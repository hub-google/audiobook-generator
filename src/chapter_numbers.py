"""Validation helpers for user-defined normalized website chapter numbers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata


_POSITIVE_DECIMAL = re.compile(r"\d+(?:\.\d+)?")


def normalize_positive_chapter_number(value):
    """Return a canonical positive decimal string without using binary floats."""
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if not _POSITIVE_DECIMAL.fullmatch(raw):
        raise ValueError("網站章節數正規化必須是大於 0 的整數或小數")
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError("網站章節數正規化必須是大於 0 的整數或小數") from error
    if not number.is_finite() or number <= 0:
        raise ValueError("網站章節數正規化必須是大於 0 的整數或小數")
    canonical = format(number.normalize(), "f")
    return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical


def normalize_chapter_number_overrides(overrides=None, *, strict=True):
    """Normalize stable positive integer UUID keys to positive decimal strings."""
    accepted = {}
    for raw_index, raw_number in (overrides or {}).items():
        key = str(raw_index).strip()
        try:
            if not key.isdigit() or int(key) < 1:
                raise ValueError("章節 UUID 必須是正整數")
            accepted[str(int(key))] = normalize_positive_chapter_number(raw_number)
        except (TypeError, ValueError):
            if strict:
                raise ValueError("章節 UUID 必須是正整數，網站章節數正規化必須是大於 0 的整數或小數")
    return accepted
