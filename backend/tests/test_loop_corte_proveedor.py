"""spec fix-corte-proveedor REQ-002/REQ-003: cuando el proveedor de IA corta la conexion a mitad de la respuesta.

Caso real (log de DEV, 2026-10-14): la respuesta del proveedor de IA empezo a las 11:04:43 (200 OK, en streaming) y a las 11:07:15 el otro lado
cerro la conexion sin terminar -- `httpx2.RemoteProtocolError: peer closed connection without sending complete message body (incomplete
chunked read)`, levantado MIENTRAS se leia el stream (`_stream_chat_completion` -> `_producer`). El usuario vio «MODELO-DESCONOCIDO:
Ocurrió un problema inesperado», que no decia que paso ni que no habia sido Vision.

Contrato que se prueba, con un el proveedor de IA FALSO cuyo stream se corta (sin red; la persistencia SQLite es la real, sobre `tmp_path`):
- corte ANTES de que el usuario viera texto del paso -> el paso se repite UNA vez con el mismo modelo y el turno termina bien: una sola
  respuesta, sin partes dobles ni huerfanas en SQLite, sin ejecutar dos veces una herramienta y con la constancia en el log (REQ-002/003);
- se corta otra vez -> el error MODELO-CORTE, que dice que fue el proveedor de IA y no Vision (REQ-001/002);
- ya se habia mostrado texto -> el error, sin reintento: no se repite lo que el usuario ya vio (REQ-002);
- el usuario apreto Detener -> sin reintento (REQ-003)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import httpx2
import pytest

from app.llm import loop as loop_module
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import SessionState

pytestmark = pytest.mark.integration

SID = "sesion-corte-proveedor"
PEER_CLOSED = "peer closed connection without sending complete message body (incomplete chunked read)"
RETRY_LOG = "Corte del proveedor de IA a mitad de la respuesta; reintento 1"
CORTE_TEXT = (
    "El proveedor de IA (el servicio de inteligencia artificial de la empresa) cortó la conexión en medio de la respuesta. "
    "No fue un error de Vision. Volvé a enviar tu mensaje; si pasa seguido, elegí otro modelo en el selector de arriba."
)
TODOS_ARGS = json.dumps({"todos": [{"content": "paso 1", "status": "in_progress", "priority": "high"}]})


# ---------------------------------------------------------------------------------------------------------------------
# El proveedor de IA falso: cada pedido devuelve el proximo paso del guion (una funcion que arma el stream de ESE pedido)
# ---------------------------------------------------------------------------------------------------------------------
def chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def tool_fragment(call_id: str, name: str, arguments: str):
    return chunk(tool_calls=[SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))])


def answer(*items):
    """Stream completo: los chunks dados y el cierre normal."""

    def step():
        yield from items
        yield chunk(finish_reason="stop" if not any(c.choices[0].delta.tool_calls for c in items) else "tool_calls")

    return step


def cut_after(*items, before_cut=None):
    """Stream que entrega `items` y despues se corta como el caso real (el otro lado cierra la conexion). `before_cut` corre justo
    antes del corte, en el hilo que lee el stream (asi el test aprieta Detener «en ese momento» sin sleeps)."""

    def step():
        yield from items
        if before_cut is not None:
            before_cut()
        raise httpx2.RemoteProtocolError(PEER_CLOSED)

    return step


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step()  # un generador: tiene `close()`, como el stream real del SDK


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(loop_module, "get_settings", lambda: type("S", (), {"llm_model": "test-model"})())

    async def _no_compact(*a, **k):
        return False

    import app.main as main_module
    import app.session.compaction as compaction

    monkeypatch.setattr(compaction, "maybe_compact", _no_compact)
    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setattr(main_module, "list_models", lambda: main_module._MODELS_CACHE.__setitem__("by_id", {}))


@pytest.fixture
def db_path(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261014110443")
    return db.db_path_for(str(root))


def run(monkeypatch, script, session: SessionState | None = None, user_message="contame algo"):
    client = FakeLLM(script)
    monkeypatch.setattr(loop_module, "get_client", lambda force_refresh=False, **_k: client)
    session = session or SessionState(session_id=SID)

    async def collect():
        return [ev async for ev in loop_module.run_turn(session, user_message, SID)]

    events = asyncio.run(collect())
    assert writer.flush(), "la cola de escritura de SQLite se vacio"
    return events, session, client


def types(events) -> list[str]:
    return [e["type"] for e in events]


def shown_text(events) -> str:
    return "".join(e["data"]["text"] for e in events if e["type"] == "text-delta")


def saved(db_path) -> list[tuple[str, str, dict]]:
    """(rol del mensaje, tipo de part, datos) de todo lo que quedo en SQLite, en orden."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.role, p.type, p.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? ORDER BY m.created_at, m.id, p.seq",
            (SID,),
        ).fetchall()
    return [(role, ptype, json.loads(data)) for role, ptype, data in rows]


def saved_roles(db_path) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return [r for (r,) in conn.execute("SELECT role FROM message WHERE session_id = ? ORDER BY created_at, id", (SID,))]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002/003: corte ANTES de que el usuario viera texto -> se repite el paso una vez y el turno termina bien
# ---------------------------------------------------------------------------------------------------------------------
def test_a_cut_before_any_text_is_retried_once_and_the_turn_ends_well(monkeypatch, db_path, caplog):
    # Primer intento: el marcador de tipo y una herramienta a medio escribir (sin texto visible) y se corta.
    script = [
        cut_after(chunk(content="[[TIPO: conversacional]]"), tool_fragment("call_cortada", "todowrite", '{"todos": [{"con')),
        answer(chunk(content="[[TIPO: conversacional]]Respuesta "), chunk(content="completa.")),
    ]
    with caplog.at_level(logging.WARNING, logger=loop_module.logger.name):
        events, session, client = run(monkeypatch, script)

    assert "error" not in types(events), "sin error: el usuario no ve el corte"
    assert types(events)[-1] == "done"
    assert shown_text(events) == "Respuesta completa."
    assert len(client.requests) == 2, "un solo reintento"
    first, retry = client.requests
    assert retry["model"] == first["model"] == "test-model", "el reintento es con el MISMO modelo"
    assert retry["messages"] == first["messages"], "lo del intento cortado no entra al historial"
    assistants = [m for m in session.messages if m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": "Respuesta completa."}], "una sola respuesta en memoria"
    assert RETRY_LOG in caplog.text, "queda constancia en app.log"
    # SQLite: el mensaje del usuario y UNA respuesta; nada de la herramienta cortada (ni filas vacias).
    assert saved_roles(db_path) == ["user", "assistant"]
    assert [(role, ptype) for role, ptype, _ in saved(db_path)] == [("user", "text"), ("assistant", "text")]
    assert saved(db_path)[-1][2]["text"] == "Respuesta completa."
    assert "call_cortada" not in json.dumps(saved(db_path))


def test_a_tool_call_cut_mid_arguments_is_not_run_and_the_retry_runs_it_once(monkeypatch, db_path):
    script = [
        cut_after(tool_fragment("call_cortada", "todowrite", TODOS_ARGS[:20])),
        answer(tool_fragment("call_ok", "todowrite", TODOS_ARGS)),
        answer(chunk(content="Listo.")),
    ]
    events, session, client = run(monkeypatch, script)

    assert types(events)[-1] == "done" and "error" not in types(events)
    assert len(client.requests) == 3, "corte + reintento del paso 1, y el paso 2"
    results = [e for e in events if e["type"] == "tool-result"]
    assert len(results) == 1, "la herramienta corre UNA vez (la del reintento), la cortada nunca"
    tool_parts = [data for role, ptype, data in saved(db_path) if ptype == "tool"]
    assert [p["call_id"] for p in tool_parts] == ["call_ok"], "en SQLite queda solo la tool-call del reintento"
    assert tool_parts[0]["name"] == "todowrite" and tool_parts[0].get("result")
    assert saved_roles(db_path).count("assistant") == 2, "un mensaje por paso: el cortado no deja fila huerfana"
    assert "call_cortada" not in json.dumps(session.messages)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001/002: si no se puede, el error dice que fue el proveedor de IA
# ---------------------------------------------------------------------------------------------------------------------
def test_a_second_cut_shows_that_proveedor_cut_the_answer(monkeypatch, db_path, caplog):
    with caplog.at_level(logging.WARNING, logger=loop_module.logger.name):
        events, _, client = run(monkeypatch, [cut_after(), cut_after()])

    assert len(client.requests) == 2, "un reintento y nada mas"
    assert caplog.text.count(RETRY_LOG) == 1
    error = events[-1]
    assert error["type"] == "error"
    assert error["data"]["code"] == "MODELO-CORTE"
    assert error["data"]["message"] == CORTE_TEXT
    assert error["data"]["technical"].startswith("MODELO-CORTE · test-model · ")
    assert saved_roles(db_path) == ["user"], "ningun intento cortado deja una respuesta vacia en SQLite"


def test_a_cut_after_text_was_shown_is_not_retried(monkeypatch, db_path, caplog):
    with caplog.at_level(logging.WARNING, logger=loop_module.logger.name):
        events, _, client = run(monkeypatch, [cut_after(chunk(content="Empiezo a contarte que")), answer(chunk(content="otra cosa"))])

    assert len(client.requests) == 1, "el usuario ya vio texto: repetir el paso duplicaria la respuesta"
    assert shown_text(events) == "Empiezo a contarte que"
    assert events[-1]["type"] == "error" and events[-1]["data"]["code"] == "MODELO-CORTE"
    assert RETRY_LOG not in caplog.text


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: Detener se respeta
# ---------------------------------------------------------------------------------------------------------------------
def test_after_detener_a_cut_is_not_retried(monkeypatch, db_path, caplog):
    session = SessionState(session_id=SID)

    def press_stop():
        session.stop_requested = True

    with caplog.at_level(logging.WARNING, logger=loop_module.logger.name):
        events, _, client = run(monkeypatch, [cut_after(before_cut=press_stop), answer(chunk(content="no deberia pedirse"))], session=session)

    assert len(client.requests) == 1, "con Detener no se vuelve a llamar al modelo"
    assert "done" not in types(events)
    assert events[-1]["type"] in {"stopped", "error"}
    assert RETRY_LOG not in caplog.text
