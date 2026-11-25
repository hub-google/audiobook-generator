"""Hugging Face cover generation transport and bounded retries.

The cover configuration functions remain injectable via the metadata facade;
the public download_ai_image import is backwards-compatible.
"""
import io
import logging
import os
import requests
import time
from PIL import Image


def _hf_error_is_retryable(error):
    """Return whether an HF inference failure is likely temporary."""
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    current = error
    while current is not None:
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return int(status) in retryable_statuses
        current = current.__cause__ or current.__context__

    message = str(error).lower()
    return any(marker in message for marker in (
        "name resolution", "failed to resolve", "temporary failure in name resolution",
        "connection reset", "connection aborted", "timed out", "timeout",
        "http 408", "http 425", "http 429", "http 500", "http 502",
        "http 503", "http 504", "service unavailable", "rate limit",
    ))


def download_ai_image(prompt, width=1280, height=720, max_attempts=5, retry_base_seconds=15):
    # Defer the import to prevent cycles and honor application/test config injection.
    from .metadata_gen import _cover_config, _validate_cover_config
    cover_config = _cover_config()
    model = str(cover_config.get("hf_model") or "black-forest-labs/FLUX.1-schnell")
    width, height, steps, guidance = _validate_cover_config(cover_config)
    logging.info(f"🖼️ 連線 Hugging Face AI 繪圖伺服器 ({model}) 生成封面底圖 ({width}x{height})...")
    
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token or not hf_token.startswith("hf_"):
        raise ValueError("❌ [CRITICAL] 缺少有效 HF_TOKEN！無法啟動 Hugging Face 生圖，流程直接終止。")

    if max_attempts < 1:
        raise ValueError("max_attempts 必須至少為 1")

    last_error = None
    for attempt in range(1, max_attempts + 1):
        img = None
        try:
            from huggingface_hub import InferenceClient
            client = InferenceClient(model=model, token=hf_token)
            img = client.text_to_image(
                prompt,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=guidance,
            )
        except Exception as hf_err:
            logging.warning("⚠️ HF SDK 呼叫失敗；同輪改用 REST API: %s", hf_err)
            api_url = f"https://api-inference.huggingface.co/models/{model}"
            headers = {"Authorization": f"Bearer {hf_token}"}
            try:
                res = requests.post(
                    api_url,
                    headers=headers,
                    json={
                        "inputs": prompt,
                        "parameters": {
                            "width": width,
                            "height": height,
                            "num_inference_steps": steps,
                            "guidance_scale": guidance,
                        },
                    },
                    timeout=60,
                )
                if res.status_code == 200:
                    img = Image.open(io.BytesIO(res.content))
                else:
                    raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
            except Exception as rest_err:
                last_error = rest_err
                if not _hf_error_is_retryable(rest_err) or attempt == max_attempts:
                    break
                delay = min(retry_base_seconds * (2 ** (attempt - 1)), 120)
                logging.warning(
                    "⚠️ HF 暫時性連線錯誤（第 %d/%d 次）：%s；%d 秒後重試",
                    attempt, max_attempts, rest_err, delay,
                )
                time.sleep(delay)
                continue

        if img:
            img = img.convert("RGB")
            if img.size != (width, height):
                raise RuntimeError(
                    f"Hugging Face returned {img.size[0]}x{img.size[1]}; "
                    f"expected {width}x{height}. Refusing to stretch the image."
                )
            logging.info("✅ 成功從 Hugging Face %s 生成底圖！", model)
            return img
        last_error = RuntimeError("Hugging Face 沒有回傳圖片")
        break

    raise RuntimeError(
        f"❌ [CRITICAL] Hugging Face 生圖失敗（已嘗試 {max_attempts} 次）: "
        f"{last_error}！流程直接終止，嚴禁降級使用低品質備用圖。"
    ) from last_error



