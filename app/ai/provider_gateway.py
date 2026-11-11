"""Shared server-side Anthropic Messages API gateway for all SIREADER AI work.

All billable model calls must pass through Cost Guard. No SDK retry is used:
a post-dispatch network failure remains UNKNOWN and is never replayed automatically.
"""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import settings
from app.ai.cost_guard import (
    reserve_ai_request,
    finalize_ai_request,
    mark_ai_request_failed,
    get_tier_config,
    record_budget_alerts,
)

logger = logging.getLogger(__name__)


class AIRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        definitely_not_charged: bool = False,
        code: str = "AI_REQUEST_FAILED",
    ):
        super().__init__(message)
        self.definitely_not_charged = definitely_not_charged
        self.code = code


class AIUsageMissingError(AIRequestError):
    pass


def _api_key() -> str | None:
    """Select credentials strictly by provider; never cross-send proxy and upstream keys."""
    provider = str(settings.AI_PROVIDER or "").strip().lower()
    if provider == "omniroute":
        return settings.AI_API_KEY
    if provider == "anthropic":
        return settings.ANTHROPIC_API_KEY
    return None


def _messages_endpoint() -> str:
    base = str(settings.AI_BASE_URL or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    provider = str(settings.AI_PROVIDER or "anthropic").strip().lower()

    # Direct Anthropic traffic is pinned to the official HTTPS hostname.
    if provider != "omniroute" and parsed.scheme == "https" and parsed.hostname == "api.anthropic.com":
        return

    # OmniRoute is a trusted local gateway on its verified default loopback
    # port. Do not permit arbitrary LAN hosts, remote HTTPS proxies, credentials
    # in the URL, or a different local port to receive the provider key.
    if provider == "omniroute":
        try:
            local_omniroute = (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port == 20128
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            local_omniroute = False
        if local_omniroute:
            return

    # Explicit test-only override remains limited to loopback HTTP.
    if (
        settings.AI_ALLOW_INSECURE_TEST_ENDPOINT
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port is not None
    ):
        return
    raise AIRequestError(
        "Anthropic mode requires https://api.anthropic.com; OmniRoute mode "
        "requires the verified local loopback endpoint http://127.0.0.1:20128.",
        definitely_not_charged=True,
        code="ANTHROPIC_ENDPOINT_NOT_OFFICIAL",
    )


def count_input_tokens(model: str, payload: dict, *, api_key: str) -> int:
    """Use Anthropic's free model-specific token-count API before paid inference."""
    if payload.get("model") != model:
        raise ValueError("Token-count model must match the selected inference model.")
    count_payload = {
        key: value for key, value in payload.items()
        if key != "max_tokens"
    }
    endpoint = _messages_endpoint().rstrip("/") + "/count_tokens"
    _validate_endpoint(endpoint)
    request = Request(
        endpoint,
        data=json.dumps(count_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": settings.ANTHROPIC_API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.AI_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise AIRequestError(
            f"Anthropic token-count preflight failed with HTTP {error.code}; paid inference was not sent.",
            definitely_not_charged=True,
            code="TOKEN_COUNT_FAILED",
        ) from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AIRequestError(
            f"Anthropic token-count preflight failed ({type(error).__name__}); paid inference was not sent.",
            definitely_not_charged=True,
            code="TOKEN_COUNT_FAILED",
        ) from error

    count = data.get("input_tokens") if isinstance(data, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise AIRequestError(
            "Anthropic token-count response did not include a valid input_tokens integer.",
            definitely_not_charged=True,
            code="TOKEN_COUNT_FAILED",
        )
    return count


def normalize_usage(usage):
    """Normalize the official Anthropic Messages usage object.

    input_tokens excludes cache reads/writes. Thinking tokens are metadata and
    are already included in output_tokens, so they must never be added twice.
    """
    if not isinstance(usage, dict):
        raise AIUsageMissingError("Anthropic response does not include usage.")

    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    creation = usage.get("cache_creation") or {}
    if not isinstance(creation, dict):
        creation = {}
    cache_write_5m = int(
        creation.get("ephemeral_5m_input_tokens")
        or usage.get("cache_write_5m_tokens")
        or 0
    )
    cache_write_1h = int(
        creation.get("ephemeral_1h_input_tokens")
        or usage.get("cache_write_1h_tokens")
        or 0
    )
    cache_write_total = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or (cache_write_5m + cache_write_1h)
    )
    if cache_write_total and not (cache_write_5m + cache_write_1h):
        # TTL detail unavailable: Cost Guard conservatively prices it as 1-hour write.
        cache_ttl_known = False
    else:
        cache_ttl_known = (cache_write_5m + cache_write_1h) > 0

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        raise AIUsageMissingError("Anthropic usage is missing input_tokens or output_tokens.")

    output_details = usage.get("output_tokens_details") or {}
    thinking_tokens = int(
        output_details.get("reasoning_tokens")
        or usage.get("thinking_tokens")
        or 0
    )
    result = {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write_total,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "thinking_tokens": thinking_tokens,
        "cache_ttl_known": cache_ttl_known,
    }
    for name, value in result.items():
        if name == "cache_ttl_known":
            continue
        if isinstance(value, bool) or value < 0:
            raise AIUsageMissingError(f"Invalid Anthropic usage value: {name}.")
    return result


def build_anthropic_payload(model: str, messages: list[dict], max_output_tokens: int) -> dict:
    """Translate the internal role/content envelope into Messages API fields."""
    system_parts: list[str] = []
    conversation: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("Only string message content is supported by this gateway.")
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            conversation.append({"role": role, "content": content})
        else:
            raise ValueError(f"Unsupported Anthropic message role: {role!r}")
    if not conversation:
        raise ValueError("Anthropic request needs at least one user/assistant message.")

    payload = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": conversation,
    }
    system_text = "\n\n".join(system_parts).strip()
    if system_text:
        if settings.AI_PROMPT_CACHING_ENABLED:
            payload["system"] = [{
                "type": "text",
                "text": system_text,
                "cache_control": {
                    "type": "ephemeral",
                    "ttl": settings.AI_PROMPT_CACHE_TTL,
                },
            }]
        else:
            payload["system"] = system_text
    return payload


def extract_response_text(data: dict) -> str:
    """Join text blocks from Anthropic's content array."""
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise AIRequestError("Anthropic response does not include content blocks.")
    parts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    content = "\n".join(part for part in parts if part)
    if not content:
        raise AIRequestError("Anthropic response does not contain text output.")
    return content


def call_ai(
    tier,
    messages,
    max_output_tokens=None,
    extra_input_tokens=0,
    analysis_log_id=None,
    request_id=None,
    max_cost_rp=None,
    max_cost_usd=None,
    on_reserved=None,
):
    """Send one Anthropic Messages-protocol request through the configured provider and Cost Guard."""
    tier = str(tier).upper()
    config = get_tier_config(tier)
    api_key = _api_key()
    if not api_key:
        raise AIRequestError(
            "AI provider credential is not configured on the backend.",
            definitely_not_charged=True,
        )
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list.")

    output_limit = config["max_output"] if max_output_tokens is None else max_output_tokens
    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or output_limit <= 0
        or output_limit > config["max_output"]
    ):
        raise ValueError("max_output_tokens exceeds the configured tier ceiling.")

    payload = build_anthropic_payload(config["model"], messages, output_limit)
    prompt_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    endpoint = _messages_endpoint()
    _validate_endpoint(endpoint)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": settings.ANTHROPIC_API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )

    # Anthropic token count is a free preflight. Do it before reserving and
    # dispatching the billable Messages request so the hard cost guard uses the
    # model-specific prompt count instead of UTF-8 byte length.
    estimated_input_tokens = count_input_tokens(
        config["model"], payload, api_key=api_key
    )
    reservation = reserve_ai_request(
        tier=tier,
        prompt_text=prompt_text,
        max_output_tokens=output_limit,
        extra_input_tokens=extra_input_tokens,
        request_id=request_id,
        model=config["model"],
        analysis_log_id=analysis_log_id,
        max_cost_rp=max_cost_rp,
        max_cost_usd=max_cost_usd,
        estimated_input_tokens=estimated_input_tokens,
    )
    request_id = reservation["request_id"]

    if on_reserved is not None:
        try:
            on_reserved(request_id)
        except Exception as error:
            try:
                mark_ai_request_failed(
                    request_id,
                    f"Pre-request state persistence failed: {type(error).__name__}",
                    definitely_not_charged=True,
                )
            except Exception:
                logger.exception(
                    "AI_PRE_REQUEST_RESERVATION_RELEASE_FAILED request_id=%s",
                    request_id,
                )
            raise AIRequestError(
                "Request was not sent because its pre-request guard rejected the reservation.",
                definitely_not_charged=True,
                code=getattr(error, "code", "PRE_REQUEST_STATE_PERSIST_FAILED"),
            ) from error

    try:
        with urlopen(request, timeout=settings.AI_TIMEOUT_SECONDS) as response:
            raw_response = response.read()
    except HTTPError as error:
        # Once dispatch is attempted, retain the reservation for reconciliation.
        mark_ai_request_failed(request_id, f"Anthropic HTTP error {error.code}", False)
        raise AIRequestError(f"Anthropic HTTP error: {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        mark_ai_request_failed(request_id, type(error).__name__, False)
        raise AIRequestError(
            "Anthropic request outcome is unknown; reservation retained for reconciliation."
        ) from error

    try:
        data = json.loads(raw_response.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        mark_ai_request_failed(request_id, "Invalid Anthropic response JSON", False)
        raise AIRequestError("Anthropic response could not be decoded.") from error

    try:
        usage = normalize_usage(data.get("usage"))
    except (AIUsageMissingError, TypeError, ValueError) as error:
        mark_ai_request_failed(request_id, "Anthropic usage missing or invalid", False)
        raise AIUsageMissingError(str(error)) from error

    cost = finalize_ai_request(
        request_id=request_id,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cache_write_tokens=usage["cache_write_tokens"],
        cache_write_5m_tokens=usage["cache_write_5m_tokens"],
        cache_write_1h_tokens=usage["cache_write_1h_tokens"],
        thinking_tokens=usage["thinking_tokens"],
        cache_ttl_known=usage["cache_ttl_known"],
    )
    billing_currency = reservation.get("billing_currency", "USD")
    if billing_currency == "USD":
        try:
            record_budget_alerts("USD")
        except Exception:
            logger.warning("Could not record Anthropic budget alert.", exc_info=True)

    return {
        "request_id": request_id,
        "model": config["model"],
        "tier": tier,
        "content": extract_response_text(data),
        "usage": usage,
        "billing_currency": billing_currency,
        "cost": cost,
        "cost_rp": cost if billing_currency == "IDR" else None,
        "cost_usd": cost if billing_currency == "USD" else None,
        "finish_reason": data.get("stop_reason"),
        "stop_sequence": data.get("stop_sequence"),
        "provider": settings.AI_PROVIDER,
        "preflight_input_tokens": estimated_input_tokens,
    }
