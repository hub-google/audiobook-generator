"""Server-side Workers AI cover provider, enabled only after confirming the Free plan."""
import base64
import io
import logging
import os
import re

import requests
from PIL import Image, ImageOps

MODEL = "@cf/black-forest-labs/flux-1-schnell"
MAX_PROMPT = 2048


def prepare_prompt(prompt):
    # The current long-form template exceeds the model's input limit.
    # Preserve story fields, condense only fixed composition instructions.
    prompt = " ".join(str(prompt).split())
    prompt = re.sub(
        r"FIXED THUMBNAIL COMPOSITION:.*?(?=COLOR AND MOOD:)",
        "COMPOSITION: layered cinematic poster, large unobstructed protagonist face, secondary characters, monumental symbol, dramatic rim light. Keep faces above darker lower title zone. No empty landscape. ",
        prompt, flags=re.DOTALL,
    )
    prompt = re.sub(
        r"STRICT OUTPUT:.*$",
        "One coherent scene, no text, letters, numbers, logo, watermark or frame.",
        prompt, flags=re.DOTALL,
    )
    suffix = " Compose all essential faces and symbols within the central widescreen crop. No text, logos or watermarks."
    if not prompt:
        raise ValueError("封面 Prompt 不可空白")
    if len(prompt) + len(suffix) > MAX_PROMPT:
        logging.warning("Cloudflare 封面 Prompt 超出模型限制；保留前段故事資訊及無文字構圖規則")
        prompt = prompt[:MAX_PROMPT - len(suffix)].rsplit(" ", 1)[0] or prompt[:MAX_PROMPT - len(suffix)]
    return prompt + suffix


def generate_cloudflare_cover(prompt, width=1280, height=720):
    """Make one inference request; never switch to a potentially paid provider."""
    if os.getenv("CLOUDFLARE_WORKERS_FREE_CONFIRMED", "").strip().lower() != "true":
        raise RuntimeError("尚未確認 Cloudflare Workers Free 方案；不執行生圖以避免超額收費")
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", account) or not token:
        raise RuntimeError("缺少有效的 Cloudflare Account ID 或 API Token")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"prompt": prepare_prompt(prompt), "steps": 4},
            timeout=(10, 120),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise RuntimeError("Cloudflare 生圖連線失敗") from None
    if response.status_code != 200:
        raise RuntimeError(f"Cloudflare 生圖失敗 HTTP {response.status_code}；未切換其他生圖服務")
    try:
        payload = response.json()
        if payload.get("success") is not True:
            raise ValueError("unsuccessful response")
        raw = base64.b64decode(payload["result"]["image"], validate=True)
        with Image.open(io.BytesIO(raw)) as opened:
            opened.verify()
        with Image.open(io.BytesIO(raw)) as opened:
            result = ImageOps.fit(opened.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    except (ValueError, KeyError, TypeError, OSError):
        raise RuntimeError("Cloudflare 回傳的圖片資料無效") from None
    logging.info("Cloudflare FLUX.1 Schnell 已生成 %dx%d 封面底圖", width, height)
    return result
