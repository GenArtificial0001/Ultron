"""`GET /api/models` cuando el proveedor de IA no responde (spec proxy-autoheal REQ-006).

Incidente real (2026-10-18): el Vision instalado no cargaba los modelos y el aviso del roster llevaba pegado el texto crudo de la falla (`ProxyError`, `HTTPSConnectionPool`,
la IP del proxy muerto). Ahora el aviso es una frase para una persona, el crudo queda en el log, y el selector sigue mostrando al menos el modelo por defecto."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main

RAW = (
    "HTTPSConnectionPool(host='login.microsoftonline.com', port=443): Max retries exceeded (Caused by ProxyError('Unable to connect to proxy', "
    "ConnectTimeoutError('Connection to 19.12.64.237 timed out.'))) C:\\Users\\interno\\ruta"
)


def _dead_openai(exc: Exception):
    """El cliente del proveedor (ULTRON: `GET /models` de cada proveedor con clave) falla siempre con `exc`."""

    class DeadOpenAI:
        def __init__(self, *_a, **_k):
            self.models = SimpleNamespace(list=self._boom)

        def _boom(self):
            raise exc

    return DeadOpenAI


@pytest.fixture
def api(monkeypatch):
    from app import config
    from app.llm import providers as llm_providers

    settings = config.get_settings()
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", "clave-de-prueba")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_models", "")
    monkeypatch.setattr(main, "settings", settings)
    llm_providers.clear_caches()
    monkeypatch.setitem(main._MODELS_CACHE, "data", None)
    monkeypatch.setitem(main._MODELS_CACHE, "fetched_at", 0.0)
    monkeypatch.setattr(llm_providers, "OpenAI", _dead_openai(ConnectionError(RAW)))
    yield TestClient(main.app, base_url="http://127.0.0.1:8030", client=("127.0.0.1", 50000))
    llm_providers.clear_caches()


def test_the_warning_is_a_plain_sentence_without_the_raw_failure(api):
    body = api.get("/api/models").json()

    warning = body["warning"]
    for raw_piece in (
        "ProxyError",
        "HTTPSConnectionPool",
        "19.12.64.237",
        "login.microsoftonline",
        "C:\\Users",
        "Max retries",
    ):
        assert raw_piece not in warning
    assert "Configuración > Red" in warning and warning.endswith(".")


def test_the_selector_still_gets_the_default_model(api):
    body = api.get("/api/models").json()

    assert [m["id"] for m in body["models"]] == [main.default_model_id()]
    assert body["current_default"] == main.default_model_id()


def test_the_raw_failure_goes_to_the_log_not_to_the_screen(api, caplog):
    with caplog.at_level(logging.WARNING):
        api.get("/api/models")

    assert "19.12.64.237" in caplog.text


def test_the_warning_for_a_plain_failure_is_also_plain(monkeypatch, api):
    from app.llm import providers as llm_providers

    monkeypatch.setattr(llm_providers, "OpenAI", _dead_openai(RuntimeError("algo interno C:\\x\\y")))

    warning = api.get("/api/models").json()["warning"]

    assert "C:\\x" not in warning and "algo interno" not in warning
