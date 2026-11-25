"""Hugging Face cover generation transport and bounded retries.

The cover configuration functions remain injectable via the metadata facade;
the public download_ai_image import is backwards-compatible.
"""
import logging
import os
import requests
import time
from huggingface_hub.errors import InferenceTimeoutError


def _hf_error_is_retryable(error):
    """Return whether an HF inference failure is likely temporary."""
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    current = error
    while current is not None:
        if isinstance(current, (requests.ConnectionError, requests.Timeout, InferenceTimeoutError)):
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


def download_ai_image(prompt, width=1280, height=720, max_attempts=4, retry_base_seconds=15):
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

    from huggingface_hub import InferenceClient

    client = InferenceClient(model=model, token=hf_token, timeout=60)
    for attempt in range(1, max_attempts + 1):
        try:
            img = client.text_to_image(
                prompt,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=guidance,
            )
        except Exception as hf_err:
            status = getattr(getattr(hf_err, "response", None), "status_code", None)
            if status == 402:
                raise RuntimeError(
                    "Hugging Face 生圖失敗（HTTP 402）：目前的推論請求需要付費額度，"
                    "請檢查 Hugging Face 帳號的 Inference Providers 用量與帳單設定。"
                ) from hf_err
            if not _hf_error_is_retryable(hf_err) or attempt == max_attempts:
                detail = f"HTTP {status}: {hf_err}" if status else str(hf_err)
                raise RuntimeError(
                    f"Hugging Face 生圖失敗（已嘗試 {attempt} 次）：{detail}"
                ) from hf_err
            delay = min(retry_base_seconds * (2 ** (attempt - 1)), 120)
            logging.warning(
                "HF 推論暫時失敗（第 %d/%d 次）：%s；%d 秒後重試",
                attempt, max_attempts, hf_err, delay,
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
        raise RuntimeError("Hugging Face 沒有回傳圖片")
