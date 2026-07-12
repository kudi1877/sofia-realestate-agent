"""Phone normalization helpers for private enrichment fields."""

from __future__ import annotations

import re


def normalize_bulgarian_phone(value: str | None) -> str | None:
    """Normalize a Bulgarian phone to +359 digits, or return None."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00359"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "359" + digits[1:]
    elif not digits.startswith("359") and len(digits) in (8, 9):
        digits = "359" + digits
    if not digits.startswith("359") or not 11 <= len(digits) <= 12:
        return None
    return f"+{digits}"
