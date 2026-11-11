"""Legacy import shim.

The historical module name remains for downstream imports only. All actual
requests are sent directly through Anthropic's official Messages API gateway.
"""
from app.ai import provider_gateway as _gateway
from app.ai.provider_gateway import (
    AIRequestError,
    AIUsageMissingError,
    build_anthropic_payload,
    call_ai,
    extract_response_text,
)

urlopen = _gateway.urlopen


def normalize_usage(usage):
    """Compatibility normalizer for historical usage records.

    New Anthropic requests use only the official Messages usage schema. The
    OpenAI-compatible branch exists only so old persisted fixtures/tools can
    still parse historical records; this shim never sends OpenAI-compatible
    HTTP requests.
    """
    if not isinstance(usage, dict):
        raise AIUsageMissingError("Provider did not return a usage object.")
    if "input_tokens" in usage:
        return _gateway.normalize_usage(usage)
    if "prompt_tokens" not in usage or "completion_tokens" not in usage:
        raise AIUsageMissingError("Legacy usage is missing token counts.")
    details = usage.get("prompt_tokens_details") or {}
    cache_read = int(details.get("cached_tokens") or 0)
    cache_write = int(
        details.get("cache_creation_input_tokens")
        or details.get("cache_write_tokens")
        or 0
    )
    output_details = usage.get("completion_tokens_details") or {}
    result = {
        "input_tokens": max(0, int(usage["prompt_tokens"]) - cache_read - cache_write),
        "output_tokens": int(usage["completion_tokens"]),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "thinking_tokens": int(output_details.get("reasoning_tokens") or 0),
        "cache_ttl_known": False,
    }
    if any(value < 0 for key, value in result.items() if key != "cache_ttl_known"):
        raise AIUsageMissingError("Legacy usage contains a negative token count.")
    return result


__all__ = [
    "AIRequestError",
    "AIUsageMissingError",
    "build_anthropic_payload",
    "call_ai",
    "extract_response_text",
    "normalize_usage",
    "urlopen",
]
