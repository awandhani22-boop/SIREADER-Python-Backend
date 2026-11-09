"""Deep recursive credential scrubbing, pattern masking, and canonical JSON hashing.

Phase 5.2.2 Human Output Review defense-in-depth sanitization.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[REDACTED]"
MAX_REVIEW_PAYLOAD_BYTES = 64 * 1024

SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "key",
    "authorization",
    "cookie",
    "credential",
    "connection_string",
)

# Regex patterns for values embedded in free text
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.=]+")
DATABASE_URL_PATTERN = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):\/\/[^\s@]+@[^\s]+")
API_KEY_PATTERN = re.compile(r"\b(?:AIza[0-9A-Za-z_\-]{30,45}|sk-[a-zA-Z0-9_\-]{20,}|ghp_[a-zA-Z0-9_\-]{20,})\b")


def mask_string(val: str) -> str:
    """Mask known secret patterns inside arbitrary strings."""
    if not isinstance(val, str):
        return val
    masked = BEARER_PATTERN.sub(REDACTED, val)
    masked = DATABASE_URL_PATTERN.sub(REDACTED, masked)
    masked = API_KEY_PATTERN.sub(REDACTED, masked)
    return masked


def sanitize_review_payload(data: Any) -> Any:
    """Recursively scrub credentials and secret patterns from nested structures."""
    if isinstance(data, dict):
        scrubbed = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in SENSITIVE_KEY_SUBSTRINGS):
                scrubbed[k] = REDACTED
            else:
                scrubbed[k] = sanitize_review_payload(v)
        return scrubbed
    elif isinstance(data, list):
        return [sanitize_review_payload(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(sanitize_review_payload(item) for item in data)
    elif isinstance(data, str):
        return mask_string(data)
    else:
        return data


def canonical_json_bytes(data: Any) -> bytes:
    """Serialize data into deterministic, canonical UTF-8 JSON bytes."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(data: Any) -> str:
    """Calculate 64-char lowercase hexadecimal SHA-256 over canonical JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest().lower()


def validate_review_payload_size(data: Any, max_bytes: int = MAX_REVIEW_PAYLOAD_BYTES) -> None:
    """Ensure sanitized payload does not exceed the 64 KiB boundary."""
    raw = canonical_json_bytes(data)
    if len(raw) > max_bytes:
        raise ValueError(f"Review payload size ({len(raw)} bytes) exceeds maximum limit of {max_bytes} bytes")
