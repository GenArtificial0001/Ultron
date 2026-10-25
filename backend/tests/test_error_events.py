"""Errores entendibles de punta a punta en el backend (pedido del usuario
2026-09-24: "mejora el manejo de errores para que los mensajes lo entiendan
las personas no tecnicas").

Cubre lo que el usuario realmente ve:
- el evento SSE `error` del chat (texto simple + codigo de soporte aparte),
  con el log de diagnostico del 400 que falto el 2026-09-24;
- dos caminos que antes terminaban EN SILENCIO: una falla dentro de Vision a
  mitad del turno y una falla del login del proveedor antes de llamar al modelo;
- los textos de Modo reunion, Conexiones y Alarmas (sin volcar el error crudo).
El modelo y los conectores son FAKES: nunca hay red.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.llm import loop as loop_module
from app.session.events import SessionState

pytestmark = pytest.mark.integration

MODEL = "gemini-3.1-pro"
USER_SECRET = "PEDIDO-PRIVADO-DEL-USUARIO"
RAW = "TEXTO-CRUDO-DEL-PROVEEDOR"
_REQUEST = SimpleNamespace(method="POST", url="https://proveedor.test/v1/chat/completions")


def status_error(cls: type[openai.APIStatusError], status: int, text: str) -> openai.APIStatusError:
    response = SimpleNamespace(status_code=status, headers={}, request=_REQUEST)
    return cls(text, response=response, body=None)


def gemini_400() -> openai.BadRequestError:
    return status_error(
        openai.BadRequestError,
        400,
        f"Error code: 400 - {RAW}: Please ensure that the number of function response parts is "
        "equal to the number of function call parts of the function call turn.",
    )


# -- fakes del cliente del proveedor de IA ----------------------------------------------------

class _Chunk:
    def __init__(self, *, content=None, tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]


def _todowrite_call() -> list[_Chunk]:
    args = json.dumps({"todos": [{"content": "Paso", "status": "in_progress", "priority": "high"}]})
    call = SimpleNamespace(index=0, id=f"call_{uuid.uuid4().hex[:8]}", function=SimpleNamespace(name="todowrite", arguments=args))
    return [_Chunk(tool_calls=[call]), _Chunk(finish_reason="tool_calls")]


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        pass


class FakeClient:
    """`raises`: excepcion (o fabrica) que tira `create()`; `chunks`: respuesta
    fija cuando no tira. Cuenta llamadas."""

    def __init__(self, *, raises=None, chunks=None):
        self.calls = 0
        self._raises = raises
        self._chunks = chunks
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises() if callable(self._raises) else self._raises
        return _Stream(self._chunks() if callable(self._chunks) else self._chunks)


@pytest.fixture(autouse=True)
def _offline_loop(monkeypatch):
    """Todo lo que `run_turn` consulta aparte del cliente de chat, sin red."""
    monkeypatch.setattr(loop_module, "get_settings", lambda: SimpleNamespace(llm_model="test-model", vision_company_name=""))

    async def _no_compact(*args, **kwargs):
        return False

    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _no_compact)
    monkeypatch.setattr(main, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setattr(main, "list_models", lambda: main._MODELS_CACHE.__setitem__("by_id", {}))


def use_client(monkeypatch, client: FakeClient) -> list[bool]:
    refreshes: list[bool] = []

    def _get_client(force_refresh: bool = False, **_kwargs):
        refreshes.append(force_refresh)
        return client

    monkeypatch.setattr(loop_module, "get_client", _get_client)
    return refreshes


def run(session: SessionState, message: str = USER_SECRET, model: str = MODEL) -> list[dict]:
    async def _collect():
        return [event async for event in loop_module.run_turn(session, message, "sesion-test", model=model)]

    return asyncio.run(_collect())


def broken_history_session() -> SessionState:
    """Historial con la forma que Gemini rechaza: 2 tool-calls, 1 respuesta."""
    session = SessionState(session_id="")  # sin session_id: persistencia no-op
    session.messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": USER_SECRET},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a1", "type": "function", "function": {"name": "read", "arguments": USER_SECRET}},
                {"id": "a2", "type": "function", "function": {"name": "read", "arguments": USER_SECRET}},
            ],
        },
        {"role": "tool", "tool_call_id": "a1", "content": USER_SECRET},
    ]
    return session


# -- chat: evento `error` -----------------------------------------------------------

def test_model_rejection_reaches_the_chat_in_plain_language(monkeypatch, caplog):
    use_client(monkeypatch, FakeClient(raises=gemini_400))
    caplog.set_level(logging.INFO)

    events = run(broken_history_session())

    error = events[-1]
    assert error["type"] == "error"
    data = error["data"]
    assert data["code"] == "MODELO-HISTORIAL"
    assert MODEL in data["message"] and "volvé a abrir ULTRON" in data["message"]
    assert RAW not in data["message"] and "HTTP" not in data["message"]
    # El codigo, el HTTP y la hora van aparte, en "Detalle tecnico".
    assert data["technical"].startswith(f"MODELO-HISTORIAL · HTTP 400 · {MODEL} · ")


def test_a_400_logs_the_history_shape_without_any_content(monkeypatch, caplog):
    use_client(monkeypatch, FakeClient(raises=gemini_400))
    caplog.set_level(logging.INFO)

    events = run(broken_history_session())

    diagnostics = [r for r in caplog.records if "Diagnostico del rechazo 400" in r.getMessage()]
    assert len(diagnostics) == 1
    line = diagnostics[0].getMessage()
    assert "S U A2 T U" in line  # el user nuevo llega con a2 sin respuesta
    assert "[4] user llega con 1 tool-call(s) sin respuesta: a2" in line
    # La misma referencia (codigo + hora) que ve el usuario, para encontrarla.
    when = events[-1]["data"]["technical"].rsplit(" · ", 1)[1]
    assert f"MODELO-HISTORIAL · modelo {MODEL} · {when}" in line
    # Nunca el contenido de la conversacion en el log.
    assert all(USER_SECRET not in r.getMessage() for r in caplog.records)
    # El traceback completo sigue quedando, con la misma referencia.
    assert any(r.exc_info and "MODELO-HISTORIAL" in r.getMessage() for r in caplog.records)


def test_rate_limit_does_not_dump_history_shape(monkeypatch, caplog):
    spend = "Error code: 429 - Too much spend: model gemini-3.1-pro is limited to $50 per 600 seconds"
    use_client(monkeypatch, FakeClient(raises=status_error(openai.RateLimitError, 429, spend)))
    caplog.set_level(logging.INFO)

    events = run(SessionState(session_id=""))

    assert events[-1]["data"]["code"] == "MODELO-LIMITE"
    assert "10 minutos" in events[-1]["data"]["message"]
    assert not any("Diagnostico del rechazo" in r.getMessage() for r in caplog.records)


def test_expired_session_retries_once_with_fresh_token_then_explains(monkeypatch):
    client = FakeClient(raises=lambda: status_error(openai.AuthenticationError, 401, "Error code: 401 - expired"))
    refreshes = use_client(monkeypatch, client)

    events = run(SessionState(session_id=""))

    assert refreshes == [False, True]  # reintento con token nuevo antes de rendirse
    assert client.calls == 2
    assert events[-1]["data"]["code"] == "MODELO-SESION"
    assert "~/.proveedor" not in events[-1]["data"]["message"]


def test_login_failure_before_calling_the_model_is_explained_not_silent(monkeypatch):
    """Antes `get_client()` estaba fuera de todo try: sin VPN, la excepcion
    subia hasta la Task del turno y el chat terminaba sin ningun mensaje."""

    def _broken_login(force_refresh: bool = False, **_kwargs):
        raise ConnectionError("HTTPSConnectionPool(host='login.microsoftonline.com'): proxy")

    monkeypatch.setattr(loop_module, "get_client", _broken_login)

    events = run(SessionState(session_id=""))

    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "MODELO-SIN-CONEXION"
    assert "VPN" in events[-1]["data"]["message"]


def test_step_limit_is_explained_in_plain_language(monkeypatch):
    # El modelo sigue pidiendo tools incluso en el ultimo paso (sin tools).
    monkeypatch.setattr(loop_module, "MAX_STEPS", 2)
    use_client(monkeypatch, FakeClient(chunks=_todowrite_call))

    events = run(SessionState(session_id=""), message="planifica algo")

    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "VISION-PASOS"
    assert "2 pasos" in events[-1]["data"]["message"]
    assert "continuá" in events[-1]["data"]["message"]


# -- chat: falla DENTRO de Vision a mitad del turno --------------------------------

def _sse_events(body: str) -> list[dict]:
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


def test_internal_crash_mid_turn_reaches_the_chat_instead_of_silence(monkeypatch, caplog):
    async def _crashing_turn(*args, **kwargs):
        yield {"type": "text-delta", "data": {"text": "Arranco..."}}
        raise RuntimeError(f"bug interno {RAW}")

    monkeypatch.setattr(main, "run_turn", _crashing_turn)
    caplog.set_level(logging.INFO)
    session_id = f"test-crash-{uuid.uuid4()}"

    resp = TestClient(main.app).post("/api/chat", json={"session_id": session_id, "message": "hola"})

    events = _sse_events(resp.text)
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["data"]["code"] == "VISION-INTERNO"
    assert RAW not in errors[0]["data"]["message"]
    assert "Volvé a enviar tu mensaje" in errors[0]["data"]["message"]
    # El detalle real queda en el log con la misma referencia.
    assert any(r.exc_info and "VISION-INTERNO" in r.getMessage() for r in caplog.records)
    # Y la sesion no queda trabada como "ocupada".
    assert not main.get_session_store().get_or_create(session_id).is_busy


# -- Modo reunion -------------------------------------------------------------------

def test_meeting_summary_error_keeps_context_and_plain_reason(monkeypatch):
    async def _limited_turn(*args, **kwargs):
        yield {"type": "error", "data": {"message": "Llegaste al límite de uso del modelo x por un rato."}}

    monkeypatch.setattr(main, "run_turn", _limited_turn)
    resp = TestClient(main.app).post(
        "/api/meeting/summarize",
        json={"session_id": f"test-meet-{uuid.uuid4()}", "transcript": "hablamos de stock"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == (
        "No pude armar el resumen de la reunión. Llegaste al límite de uso del modelo x por un rato."
    )


def test_meeting_summary_internal_failure_hides_raw_text(monkeypatch):
    async def _crashing_turn(*args, **kwargs):
        raise RuntimeError(RAW)
        yield  # pragma: no cover -- generador

    monkeypatch.setattr(main, "run_turn", _crashing_turn)
    resp = TestClient(main.app).post(
        "/api/meeting/summarize",
        json={"session_id": f"test-meet-{uuid.uuid4()}", "transcript": "hablamos de stock"},
    )
    assert resp.status_code == 502
    assert RAW not in resp.json()["detail"]
    assert resp.json()["detail"].startswith("No pude armar el resumen de la reunión")


# -- Configuracion > Conexiones ------------------------------------------------------

def test_connector_error_explains_first_and_keeps_technical_detail_last():
    connector = MagicMock()
    connector.list_datasets.side_effect = RuntimeError(
        "403 Access Denied: User does not have bigquery.datasets.list permission"
    )
    with patch("app.connectors.bigquery_connector.BigQueryConnector", return_value=connector):
        resp = TestClient(main.app).get("/api/config/connections/bigquery/datasets")

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail.startswith("Tu usuario no tiene permiso para ver esto en BigQuery.")
    friendly, technical = detail.split(" Detalle técnico: ")
    assert "403" not in friendly
    assert technical.startswith("403 Access Denied")


def test_connector_test_timeout_is_plain_language():
    connector_cls = MagicMock()
    connector_cls.return_value.test_connection.side_effect = TimeoutError()
    with patch("app.connectors.oracle_connector.OracleConnector", connector_cls):
        resp = TestClient(main.app).post("/api/config/credentials/test/oracle")

    assert resp.status_code == 504
    assert resp.json()["detail"].startswith("Oracle tardó más de 15 segundos en responder.")


# -- Alarmas: "Ejecutar archivo" ----------------------------------------------------

def test_alarm_file_that_no_longer_exists_is_explained(monkeypatch):
    import os

    from app.persistence import master_index

    monkeypatch.setattr(master_index, "get_schedule", lambda sid: {"id": sid, "file_path": "C:/x/reporte.xlsx"})

    def _missing(path):
        raise FileNotFoundError(2, f"[WinError 2] {RAW}", path)

    monkeypatch.setattr(os, "startfile", _missing, raising=False)
    resp = TestClient(main.app).post("/api/alarms/alarma-1/run")

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("No encontré el archivo")
    assert RAW not in resp.json()["detail"]
