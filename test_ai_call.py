
import json
from unittest.mock import Mock

import pytest

from app.ai import omniroute_client as client


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.data


def test_call_ai_sukses_tanpa_api_key_sungguhan(monkeypatch):
    # Konfigurasi API palsu
    monkeypatch.setattr(client.settings, "AI_API_KEY", "test-api-key")
    monkeypatch.setattr(
        client.settings,
        "AI_BASE_URL",
        "https://example.test/v1",
    )
    monkeypatch.setattr(
        client.settings,
        "AI_TIMEOUT_SECONDS",
        10,
    )

    # Konfigurasi tier palsu
    monkeypatch.setattr(
        client,
        "get_tier_config",
        lambda tier: {
            "model": "test-model",
            "max_output": 100,
        },
    )

    # Mock reservasi biaya
    reserve_mock = Mock(
        return_value={
            "request_id": "test-request-001",
            "billing_currency": "IDR",
        }
    )
    monkeypatch.setattr(
        client,
        "reserve_ai_request",
        reserve_mock,
    )

    # Respons provider palsu
    fake_response = {
        "choices": [
            {
                "message": {
                    "content": "Analisis berhasil."
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }

    urlopen_mock = Mock(
        return_value=FakeResponse(fake_response)
    )
    monkeypatch.setattr(client, "urlopen", urlopen_mock)

    # Mock pencatatan biaya aktual
    finalize_mock = Mock(return_value=150)
    monkeypatch.setattr(
        client,
        "finalize_ai_request",
        finalize_mock,
    )

    # Jalankan fungsi asli call_ai()
    result = client.call_ai(
        tier="FAST",
        messages=[
            {"role": "user", "content": "Tes SIREADER"}
        ],
        max_output_tokens=32,
    )

    # Periksa hasil
    assert result["request_id"] == "test-request-001"
    assert result["tier"] == "FAST"
    assert result["model"] == "test-model"
    assert result["content"] == "Analisis berhasil."
    assert result["cost"] == 150
    assert result["cost_rp"] == 150
    assert result["billing_currency"] == "IDR"

    # Pastikan reservasi dan pencatatan biaya dipanggil
    reserve_mock.assert_called_once()
    finalize_mock.assert_called_once()

    # Pastikan request HTTP hanya dikirim satu kali
    urlopen_mock.assert_called_once()
