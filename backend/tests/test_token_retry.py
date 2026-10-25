"""spec transcripcion-reintenta-token: ante un 401 (token invalidado) la transcripcion y la lectura de PDF escaneado
piden un token nuevo y reintentan UNA vez, como ya hace el chat. Sin red: el cliente del proveedor de IA es un fake."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.llm import client as llm_client

AUDIO = b"\x1aE\xdf\xa3fake-webm-bytes"
DATA_URI = "data:audio/webm;base64," + base64.b64encode(AUDIO).decode()


def status_error(cls, status: int, text: str):
    """Excepcion real del SDK con un `response` minimo (mismo patron que test_friendly_errors: sin httpx)."""
    response = SimpleNamespace(status_code=status, headers={}, request=SimpleNamespace(method="POST", url="https://x"))
    return cls(text, response=response, body=None)


def auth_error() -> openai.AuthenticationError:
    return status_error(openai.AuthenticationError, 401, "Error code: 401 - {'Error': 'Invalid Token'}")


class FakeWhisper:
    """Falla con 401 las primeras `fail_times` llamadas. Lee el audio como lo hace el SDK (consume el archivo)."""

    def __init__(self, fail_times=0, exc=None):
        self.fail_times, self.exc, self.calls = fail_times, exc, []

    def create(self, **kw):
        self.calls.append({"audio": kw["file"].read(), "language": kw.get("language", "<omitido>"), "model": kw["model"]})
        if self.exc is not None:
            raise self.exc
        if len(self.calls) <= self.fail_times:
            raise auth_error()
        return "  hola vision  "


@pytest.fixture
def api():
    return TestClient(main.app)


@pytest.fixture
def server(monkeypatch):
    """`get_stt_client` de main, espiado (ULTRON: el dictado tiene su propio proveedor, STT_*). `get_stt_client` arma SIEMPRE un
    cliente nuevo con la configuracion vigente, asi que el segundo pedido es el «cliente refrescado» del reintento."""
    state = SimpleNamespace(whisper=FakeWhisper(), refreshes=[])

    def fake_get_stt_client():
        state.refreshes.append(bool(state.refreshes))
        return SimpleNamespace(audio=SimpleNamespace(transcriptions=state.whisper)), "whisper-large"

    monkeypatch.setattr(main, "get_stt_client", fake_get_stt_client)
    return state


def post(api, **body):
    return api.post("/api/transcribe", json={"audio": DATA_URI, **body})


# REQ-001 / REQ-002
def test_a_401_refreshes_the_token_and_retries_once_with_the_whole_audio(api, server):
    server.whisper.fail_times = 1

    r = post(api)

    assert r.status_code == 200 and r.json() == {"text": "hola vision"}
    assert server.refreshes == [False, True], "el reintento debe pedir un token nuevo (force_refresh=True)"
    assert [c["audio"] for c in server.whisper.calls] == [AUDIO, AUDIO], "el reintento mando el audio incompleto"


# REQ-003
def test_if_the_401_persists_it_gives_the_friendly_error_after_exactly_two_calls(api, server):
    server.whisper.fail_times = 99

    r = post(api)

    assert r.status_code == 502
    detail = r.json()["detail"]
    # ULTRON: el dictado usa una clave fija (STT_API_KEY), asi que un 401 que persiste es una clave rechazada, no una sesion vencida.
    assert detail.startswith("No pude pasar tu audio a texto") and "clave" in detail
    assert len(server.whisper.calls) == 2, "nunca mas de 2 llamadas por grabacion"
    assert "Invalid Token" not in detail and "401" not in detail


# REQ-004
@pytest.mark.parametrize("exc", [RuntimeError("gateway timeout"), TimeoutError("lento"), ValueError("raro")])
def test_other_errors_are_not_retried(api, server, exc):
    server.whisper.exc = exc

    r = post(api)

    assert r.status_code == 502
    assert len(server.whisper.calls) == 1 and server.refreshes == [False]


def test_a_429_is_not_retried_either(api, server):
    server.whisper.exc = status_error(openai.RateLimitError, 429, "Error code: 429")

    assert post(api).status_code == 502
    assert len(server.whisper.calls) == 1


# REQ-005
def test_the_original_401_is_logged_as_a_one_line_warning(api, server, caplog):
    server.whisper.fail_times = 1

    with caplog.at_level("WARNING"):
        post(api)

    hits = [r for r in caplog.records if "401" in r.getMessage() and "token" in r.getMessage().lower()]
    assert len(hits) == 1 and hits[0].exc_info is None


# REQ-007
def test_with_a_valid_token_there_is_one_call_and_language_is_still_omitted(api, server):
    r = post(api)

    assert r.status_code == 200
    assert len(server.whisper.calls) == 1 and server.refreshes == [False]
    assert server.whisper.calls[0]["language"] == "<omitido>"


def test_an_explicit_language_survives_the_retry(api, server):
    server.whisper.fail_times = 1

    post(api, language="es")

    assert [c["language"] for c in server.whisper.calls] == ["es", "es"]


# REQ-001: el ayudante en si
def test_helper_retries_once_with_a_fresh_client(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(llm_client, "get_client", lambda force_refresh=False, **_k: seen.append(force_refresh) or f"cliente-{len(seen)}")
    attempts: list[str] = []

    def work(c):
        attempts.append(c)
        if len(attempts) == 1:
            raise auth_error()
        return "ok"

    assert llm_client.call_with_token_retry(work) == "ok"
    assert attempts == ["cliente-1", "cliente-2"] and seen == [False, True]


def test_helper_lets_a_second_401_through(monkeypatch):
    monkeypatch.setattr(llm_client, "get_client", lambda force_refresh=False, **_k: "c")
    calls = []

    def work(_c):
        calls.append(1)
        raise auth_error()

    with pytest.raises(openai.AuthenticationError):
        llm_client.call_with_token_retry(work)
    assert len(calls) == 2


# REQ-006
def test_pdf_vision_completion_retries_after_a_401(monkeypatch):
    from app import pdf_vision

    state = {"n": 0, "refreshes": []}

    class Completions:
        def create(self, **_kw):
            state["n"] += 1
            if state["n"] == 1:
                raise auth_error()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="texto de la pagina"))])

    def fake_get_client(force_refresh=False, **_kwargs):
        state["refreshes"].append(force_refresh)
        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    monkeypatch.setattr(llm_client, "get_client", fake_get_client)

    assert pdf_vision.complete_with_proveedor("modelo", [{"role": "user", "content": "x"}]) == "texto de la pagina"
    assert state["refreshes"] == [False, True]


# REQ-008
def test_context_summary_retries_after_a_401(monkeypatch):
    import asyncio

    from app.session import compaction

    state = {"n": 0, "refreshes": []}

    class Completions:
        def create(self, **_kw):
            state["n"] += 1
            if state["n"] == 1:
                raise auth_error()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="resumen ok"))])

    def fake_get_client(force_refresh=False, **_kwargs):
        state["refreshes"].append(force_refresh)
        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    monkeypatch.setattr(llm_client, "get_client", fake_get_client)

    assert asyncio.run(compaction._summarize("usuario: hola", "modelo")) == "resumen ok"
    assert state["refreshes"] == [False, True]
