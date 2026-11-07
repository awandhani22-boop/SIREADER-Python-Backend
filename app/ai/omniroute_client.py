
import json
import logging
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app.config import settings
from app.ai.cost_guard import (
    reserve_ai_request,
    finalize_ai_request,
    mark_ai_request_failed,
    get_tier_config,
    record_budget_alerts,
)

logger = logging.getLogger(__name__)


class AIRequestError(Exception):
    pass


class AIUsageMissingError(AIRequestError):
    pass


def normalize_usage(usage):
    """
    Membaca format usage OpenAI-compatible maupun Anthropic.

    input_tokens yang dikembalikan tidak termasuk cache.
    thinking_tokens adalah metadata: token itu SUDAH termasuk output_tokens.
    """

    if not isinstance(usage, dict):
        raise AIUsageMissingError(
            "Provider tidak mengembalikan data usage."
        )

    details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = (
        usage.get("output_tokens_details")
        or usage.get("completion_tokens_details")
        or {}
    )

    cache_read = (
        usage.get("cache_read_input_tokens")
        or usage.get("cache_read_tokens")
        or details.get("cached_tokens")
        or input_details.get("cached_tokens")
        or 0
    )

    # Rincian TTL cache Anthropic: cache_creation.ephemeral_5m/1h_input_tokens.
    creation = usage.get("cache_creation") or {}
    if not isinstance(creation, dict):
        creation = {}

    cache_write_5m = (
        creation.get("ephemeral_5m_input_tokens")
        or usage.get("cache_write_5m_tokens")
        or usage.get("cache_creation_5m_input_tokens")
        or 0
    )
    cache_write_1h = (
        creation.get("ephemeral_1h_input_tokens")
        or usage.get("cache_write_1h_tokens")
        or usage.get("cache_creation_1h_input_tokens")
        or 0
    )

    cache_write_total = (
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or details.get("cache_creation_input_tokens")
        or details.get("cache_write_tokens")
        or input_details.get("cache_creation_input_tokens")
        or 0
    )

    cache_read = int(cache_read)
    cache_write_5m = int(cache_write_5m)
    cache_write_1h = int(cache_write_1h)
    cache_write = int(cache_write_total)

    cache_ttl_known = (cache_write_5m + cache_write_1h) > 0
    if cache_ttl_known and not cache_write:
        cache_write = cache_write_5m + cache_write_1h

    # Format Anthropic: input_tokens terpisah dari cache.
    if "input_tokens" in usage:
        input_tokens = int(usage["input_tokens"])

    # Format OpenAI: prompt_tokens dapat mencakup cache.
    elif "prompt_tokens" in usage:
        input_tokens = max(
            0,
            int(usage["prompt_tokens"])
            - cache_read
            - cache_write,
        )

    else:
        raise AIUsageMissingError(
            "Field input_tokens/prompt_tokens tidak ditemukan."
        )

    output_tokens = usage.get(
        "output_tokens",
        usage.get("completion_tokens"),
    )

    if output_tokens is None:
        raise AIUsageMissingError(
            "Field output_tokens/completion_tokens tidak ditemukan."
        )

    # Thinking token hanya metadata; TIDAK ditambahkan ke output.
    thinking_tokens = int(
        output_details.get("reasoning_tokens")
        or details.get("reasoning_tokens")
        or usage.get("thinking_tokens")
        or 0
    )

    result = {
        "input_tokens": input_tokens,
        "output_tokens": int(output_tokens),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "thinking_tokens": thinking_tokens,
        "cache_ttl_known": cache_ttl_known,
    }

    for name, value in result.items():
        if name == "cache_ttl_known":
            continue
        if value < 0:
            raise AIUsageMissingError(
                f"Nilai usage tidak valid: {name}"
            )

    return result


def extract_response_text(data):
    """Mengambil teks jawaban dari respons chat completion."""

    choices = data.get("choices")

    if not isinstance(choices, list) or not choices:
        raise AIRequestError(
            "Respons AI tidak memiliki choices."
        )

    message = choices[0].get("message", {})
    content = message.get("content")

    if isinstance(content, str):
        return content

    # Mendukung format content berupa daftar bagian teks.
    if isinstance(content, list):
        parts = []

        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
            ):
                parts.append(item.get("text", ""))

        if parts:
            return "\n".join(parts)

    raise AIRequestError(
        "Respons AI tidak memiliki konten teks."
    )


def call_ai(
    tier,
    messages,
    max_output_tokens=None,
    extra_input_tokens=0,
    analysis_log_id=None,
):
    """
    Memanggil satu model OmniRoute.

    Semua request wajib melalui Cost Guard.

    Tidak ada retry otomatis.
    Tidak ada model fallback otomatis.
    """

    tier = tier.upper()
    config = get_tier_config(tier)

    if not settings.AI_API_KEY:
        raise AIRequestError(
            "AI_API_KEY belum dikonfigurasi."
        )

    if not settings.AI_BASE_URL:
        raise AIRequestError(
            "AI_BASE_URL belum dikonfigurasi."
        )

    if not isinstance(messages, list) or not messages:
        raise ValueError(
            "messages harus berupa list dan tidak boleh kosong."
        )

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Format message tidak valid.")

        if not message.get("role") or "content" not in message:
            raise ValueError(
                "Setiap message memerlukan role dan content."
            )

    output_limit = (
        max_output_tokens
        if max_output_tokens is not None
        else config["max_output"]
    )

    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or output_limit <= 0
        or output_limit > config["max_output"]
    ):
        raise ValueError(
            "max_output_tokens melewati batas konfigurasi tier."
        )

    # Serialisasi dilakukan sebelum reservasi.
    prompt_text = json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    payload = {
        "model": config["model"],
        "messages": messages,
        "max_tokens": output_limit,
        "stream": False,
    }

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    endpoint = (
        settings.AI_BASE_URL.rstrip("/")
        + "/chat/completions"
    )

    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.AI_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    # Reservasi harus berhasil sebelum request dikirim.
    reservation = reserve_ai_request(
        tier=tier,
        prompt_text=prompt_text,
        max_output_tokens=output_limit,
        extra_input_tokens=extra_input_tokens,
        analysis_log_id=analysis_log_id,
    )

    request_id = reservation["request_id"]

    try:
        with urlopen(
            request,
            timeout=settings.AI_TIMEOUT_SECONDS,
        ) as response:
            raw_response = response.read()

        data = json.loads(
            raw_response.decode("utf-8")
        )

    except HTTPError as error:
        # Request telah mencapai server.
        # Biaya belum dapat dipastikan, pertahankan reservasi.
        mark_ai_request_failed(
            request_id,
            f"HTTP error {error.code}",
            definitely_not_charged=False,
        )

        raise AIRequestError(
            f"OmniRoute HTTP error: {error.code}"
        ) from error

    except (URLError, TimeoutError, OSError) as error:
        # Bisa jadi request sudah diproses provider.
        # Jangan melepas reservasi secara otomatis.
        mark_ai_request_failed(
            request_id,
            str(error),
            definitely_not_charged=False,
        )

        raise AIRequestError(
            "Status request tidak diketahui. "
            "Reservasi biaya dipertahankan."
        ) from error

    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        mark_ai_request_failed(
            request_id,
            str(error),
            definitely_not_charged=False,
        )

        raise AIRequestError(
            "Respons OmniRoute tidak dapat dibaca."
        ) from error

    # Setelah respons diterima, usage wajib tersedia.
    try:
        usage = normalize_usage(data.get("usage"))

    except (AIUsageMissingError, TypeError, ValueError) as error:
        mark_ai_request_failed(
            request_id,
            str(error),
            definitely_not_charged=False,
        )
        raise

    # Catat biaya aktual berdasarkan usage provider.
    cost = finalize_ai_request(
        request_id=request_id,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cache_write_tokens=usage["cache_write_tokens"],
        cache_write_5m_tokens=usage.get("cache_write_5m_tokens"),
        cache_write_1h_tokens=usage.get("cache_write_1h_tokens"),
        thinking_tokens=usage.get("thinking_tokens", 0),
        cache_ttl_known=usage.get("cache_ttl_known"),
    )

    billing_currency = reservation.get("billing_currency", "IDR")

    # Alert budget hanya untuk jalur USD; kegagalan alert tidak boleh
    # membatalkan request yang sudah selesai dan sudah tercatat.
    if billing_currency == "USD":
        try:
            record_budget_alerts("USD")
        except Exception:  # noqa: BLE001 - best effort, jangan gagalkan call
            logger.warning("Gagal mencatat alert budget USD.", exc_info=True)

    # Ambil teks setelah biaya berhasil tercatat.
    content = extract_response_text(data)

    return {
        "request_id": request_id,
        "model": config["model"],
        "tier": tier,
        "content": content,
        "usage": usage,
        "billing_currency": billing_currency,
        "cost": cost,
        "cost_rp": cost if billing_currency == "IDR" else None,
        "cost_usd": cost if billing_currency == "USD" else None,
        "finish_reason": data["choices"][0].get(
            "finish_reason"
        ),
    }
