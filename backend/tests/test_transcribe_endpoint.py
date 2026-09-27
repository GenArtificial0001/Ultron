"""Integracion de `POST /api/transcribe` (modo chat por voz, modo reunion y
modo Cerebro comparten este endpoint): decodifica el data URI, llama al
proveedor de dictado del .env (spec vision-generico REQ-003: STT_BASE_URL,
STT_API_KEY, STT_MODEL) y devuelve `{"text": ...}`. El cliente es un FAKE:
nunca hay red ni audio real."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main

AUDIO = b"\x1aE\xdf\xa3fake-webm-bytes"
DATA_URI = "data:audio/webm;base64," + base64.b64encode(AUDIO).decode()


class FakeTranscriptions:
    def __init__(self, result="  hola vision  ", exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def create(self, **kwargs):
        kwargs["audio_bytes"] = kwargs["file"].read()  # el archivo se consume dentro de la llamada
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


@pytest.fixture
def whisper(monkeypatch):
    fake = FakeTranscriptions()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake))
    monkeypatch.setattr(main, "get_stt_client", lambda: (client, "modelo-de-voz"))
    return fake


@pytest.fixture
def api():
    return TestClient(main.app)


def post(api, **body):
    return api.post("/api/transcribe", json=body)


def test_transcribes_and_strips_whitespace(api, whisper):
    r = post(api, audio=DATA_URI)
    assert r.status_code == 200 and r.json() == {"text": "hola vision"}


def test_sends_decoded_bytes_to_the_configured_voice_model_as_text(api, whisper):
    post(api, audio=DATA_URI)
    call = whisper.calls[0]
    assert call["model"] == "modelo-de-voz" and call["response_format"] == "text"
    assert call["audio_bytes"] == AUDIO  # el prefijo "data:...;base64," se descarto
    assert call["file"].name == "audio.webm"  # la API infiere el formato por el nombre


def test_language_is_omitted_for_auto_detect(api, whisper):
    post(api, audio=DATA_URI)
    assert "language" not in whisper.calls[0]
    post(api, audio=DATA_URI, language=None)
    assert "language" not in whisper.calls[1]
    post(api, audio=DATA_URI, language="")  # cadena vacia = tambien auto-detect
    assert "language" not in whisper.calls[2]


@pytest.mark.parametrize("lang", ["es", "en", "pt"])
def test_explicit_language_is_forced(api, whisper, lang):
    """Bug real: hablando en español, Whisper a veces transcribia en ingles."""
    post(api, audio=DATA_URI, language=lang)
    assert whisper.calls[0]["language"] == lang


def test_response_object_with_text_attribute_is_supported(api, whisper):
    whisper.result = SimpleNamespace(text="  objeto con text ")
    assert post(api, audio=DATA_URI).json() == {"text": "objeto con text"}


def test_bare_base64_without_data_uri_prefix_is_silently_treated_as_empty(api, whisper):
    """Documenta el comportamiento ACTUAL: el contrato es un data URI completo. Si llega
    base64 pelado (sin `data:...,`), `partition(",")` deja el payload VACIO y Whisper
    recibe 0 bytes sin ningun error -- el cliente no se entera de que mando mal el audio."""
    r = post(api, audio=base64.b64encode(AUDIO).decode())
    assert r.status_code == 200
    assert whisper.calls[0]["audio_bytes"] == b""


def test_invalid_base64_returns_400_and_never_calls_whisper(api, whisper):
    r = post(api, audio="data:audio/webm;base64,@@@no-es-base64@@@")
    assert r.status_code == 400 and "No pude leer la grabación" in r.json()["detail"]
    assert whisper.calls == []


def test_upstream_failure_becomes_502_with_plain_language_reason(api, whisper):
    # Pedido del usuario 2026-09-24: el texto crudo del gateway ya no llega a
    # la pantalla (queda en el log); el usuario ve que paso y que puede hacer.
    whisper.exc = RuntimeError("gateway timeout")
    r = post(api, audio=DATA_URI)
    detail = r.json()["detail"]
    assert r.status_code == 502
    assert detail.startswith("No pude pasar tu audio a texto.")
    assert "escribí el mensaje" in detail
    assert "gateway timeout" not in detail


def test_missing_audio_field_is_a_validation_error(api, whisper):
    assert api.post("/api/transcribe", json={}).status_code == 422
    assert whisper.calls == []


def test_empty_audio_is_forwarded_and_whisper_decides(api, whisper):
    """Documenta el comportamiento actual: un clip vacio NO se rechaza en el
    backend (lo filtra el frontend por nivel de voz) -- llega vacio a Whisper."""
    r = post(api, audio="data:audio/webm;base64,")
    assert r.status_code == 200 and whisper.calls[0]["audio_bytes"] == b""


# ---------------------------------------------------------------------------
# spec vision-generico REQ-003: sin proveedor de dictado configurado
# ---------------------------------------------------------------------------
def test_without_voice_provider_the_user_is_told_dictation_is_not_configured(api, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "stt_base_url", "")
    monkeypatch.setattr(settings, "stt_api_key", "")
    r = post(api, audio=DATA_URI)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("El dictado por voz no está configurado")


def test_voice_provider_uses_its_own_address_key_and_model(monkeypatch):
    from app.config import get_settings
    from app.llm.client import get_stt_client

    settings = get_settings()
    monkeypatch.setattr(settings, "stt_base_url", "https://voz.example.test/v1")
    monkeypatch.setattr(settings, "stt_api_key", "clave-de-voz")
    monkeypatch.setattr(settings, "stt_model", "whisper-1")
    client, model = get_stt_client()
    assert str(client.base_url).rstrip("/") == "https://voz.example.test/v1"
    assert client.api_key == "clave-de-voz" and model == "whisper-1"
