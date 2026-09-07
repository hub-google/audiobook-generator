"""Canonicalize scraped chapter text before advertisement review."""

from __future__ import annotations

import re
import unicodedata


RAW_NORMALIZER_VERSION = "raw-normalizer-v1"
_INLINE_WHITESPACE_RE = re.compile(r"[^\S\r\n]+", re.UNICODE)
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def normalize_scraped_text(value: str) -> str:
    """Apply NFKC and remove every in-line whitespace while preserving lines."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    text = _INLINE_WHITESPACE_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()

