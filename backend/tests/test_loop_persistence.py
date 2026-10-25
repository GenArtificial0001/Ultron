"""spec persistencia-sqlite-sesiones REQ-002: lo que `run_turn` deja en SQLite al terminar cada paso.

Hasta ahora ningun test corria el bucle CON persistencia (`test_loop_characterization.py` usa una sesion sin carpeta). Este archivo
corre turnos reales de `run_turn` con un cliente de chat FALSO (mismo patron: un guion de chunks por paso, sin red ni modelo) sobre una
carpeta de sesion en `tmp_path` registrada en el indice maestro (`tmp_master_db`), y despues lee la base con SQL crudo.

Bug que fija (encontrado al auditar la spec, 2026-10-08): una sola bandera marcaba a la vez "mensaje assistant insertado" y "part de
texto insertado". Si en un paso el modelo pedia una herramienta ANTES de escribir texto, el part de texto nunca se insertaba y el UPDATE
final caia sobre un id inexistente: ese texto no quedaba en SQLite y no volvia al recargar ni tras reiniciar Vision."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from app.llm import loop as loop_module
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import SessionState

SID = "sesion-con-persistencia"


# ---------------------------------------------------------------------------------------------------------------------
# Cliente falso (mismo contrato que el de test_loop_characterization.py)
# ---------------------------------------------------------------------------------------------------------------------
class _Fn:
    def __init__(self, name: str = "", arguments: str = ""):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, index: int, id_: str, name: str, arguments: str):
        self.index, self.id, self.function = index, id_, _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []


class _Choice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.delta, self.finish_reason = _Delta(content, tool_calls), finish_reason


class _Chunk:
    def __init__(self, choice: _Choice):
        self.choices = [choice]


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)

    def close(self):  # pragma: no cover
        pass


class _Completions:
    def __init__(self, script: list):
        self.script, self.calls = list(script), 0

    def create(self, **kwargs):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return _Stream(step)


class _Client:
    def __init__(self, script):
        self.completions = _Completions(script)
        self.chat = type("Chat", (), {"completions": self.completions})()


TODOS_ARGS = json.dumps({"todos": [{"content": "paso 1", "status": "in_progress", "priority": "high"}]})


def text_chunks(*pieces: str, finish: str = "stop"):
    return [_Chunk(_Choice(content=p)) for p in pieces] + [_Chunk(_Choice(finish_reason=finish))]


def tool_chunk(call_id: str = "call_1", name: str = "todowrite", arguments: str = TODOS_ARGS):
    return _Chunk(_Choice(tool_calls=[_TC(0, call_id, name, arguments)]))


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
    master_index.set_project_path(SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261008230000")
    return db.db_path_for(str(root))


def run(script, user_message="hola"):
    client = _Client(script)
    loop_module.get_client = lambda force_refresh=False, **_k: client
    session = SessionState(session_id=SID)

    async def collect():
        return [ev async for ev in loop_module.run_turn(session, user_message, SID)]

    events = asyncio.run(collect())
    assert writer.flush(), "la cola de escritura de SQLite se vacio"
    return events, session


def parts(db_path) -> list[tuple[str, str, dict]]:
    """(rol del mensaje, tipo de part, datos) en el orden en que quedaron guardados."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.role, p.type, p.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? ORDER BY m.created_at, m.id, p.seq",
            (SID,),
        ).fetchall()
    return [(role, ptype, json.loads(data)) for role, ptype, data in rows]


def assistant_texts(db_path) -> list[str]:
    return [d.get("text", "") for role, ptype, d in parts(db_path) if role == "assistant" and ptype == "text"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: lo que queda en la base despues de un turno
# ---------------------------------------------------------------------------------------------------------------------
def test_a_plain_answer_leaves_the_user_message_and_the_full_answer(db_path):
    events, _ = run([text_chunks("Hola, ", "soy Vision.")], user_message="quien sos")

    assert events[-1]["type"] == "done"
    saved = parts(db_path)
    assert ("user", "text", {"text": "quien sos"}) in [(r, t, {"text": d.get("text")}) for r, t, d in saved]
    assert assistant_texts(db_path) == ["Hola, soy Vision."], "el texto final completo (los deltas no se guardan sueltos)"


def test_a_tool_call_is_saved_complete_with_its_result(db_path):
    run([[tool_chunk(), _Chunk(_Choice(finish_reason="tool_calls"))], text_chunks("Listo.")])

    tools = [d for role, ptype, d in parts(db_path) if ptype == "tool"]
    assert len(tools) == 1
    assert tools[0]["call_id"] == "call_1" and tools[0]["name"] == "todowrite"
    assert json.loads(tools[0]["arguments"])["todos"][0]["content"] == "paso 1"
    assert tools[0].get("result"), "el resultado de la herramienta tambien queda guardado"
    assert assistant_texts(db_path)[-1] == "Listo."


def test_text_before_a_tool_call_is_saved(db_path):
    run([[_Chunk(_Choice(content="Dejame revisar. ")), tool_chunk(), _Chunk(_Choice(finish_reason="tool_calls"))], text_chunks("Listo.")])

    assert assistant_texts(db_path) == ["Dejame revisar. ", "Listo."]


def test_text_that_arrives_after_a_tool_call_in_the_same_step_is_saved(db_path):
    """El bug: tool-call primero y texto despues, en el MISMO paso. Antes ese texto no quedaba en SQLite."""
    step1 = [tool_chunk(), _Chunk(_Choice(content="Anoto el plan ")), _Chunk(_Choice(content="y sigo.")), _Chunk(_Choice(finish_reason="tool_calls"))]

    _, session = run([step1, text_chunks("Listo.")])

    assert "Anoto el plan y sigo." in str(session.messages), "en memoria siempre estuvo"
    assert assistant_texts(db_path) == ["Anoto el plan y sigo.", "Listo."], "y ahora tambien en la base"
    # y vuelve igual tras un reinicio (lo que lee get_or_create)
    restored = persistence.load_session_messages(SID)
    assert "Anoto el plan y sigo." in str(restored)


def test_a_tool_only_step_does_not_leave_an_empty_text_part(db_path):
    run([[tool_chunk(), _Chunk(_Choice(finish_reason="tool_calls"))], text_chunks("Listo.")])

    assert "" not in assistant_texts(db_path), "sin texto en el paso de la herramienta no se guarda un part de texto vacio"


def test_each_step_is_its_own_assistant_message(db_path):
    run([[tool_chunk(), _Chunk(_Choice(finish_reason="tool_calls"))], text_chunks("Listo.")])

    with closing(sqlite3.connect(db_path)) as conn:
        roles = [r for (r,) in conn.execute("SELECT role FROM message WHERE session_id = ? ORDER BY created_at, id", (SID,))]
    assert roles.count("user") == 1 and roles.count("assistant") == 2


def test_a_tool_only_step_keeps_its_response_type_badge(db_path):
    """Sin marcador, el tipo del paso que solo pide herramientas se infiere y se guarda en su fila (el badge sobrevive a recargar).
    Fija que separar las banderas no cambio esto: la fila del mensaje existe aunque el paso no tenga texto."""
    run([[tool_chunk(), _Chunk(_Choice(finish_reason="tool_calls"))], text_chunks("Listo.")])

    with closing(sqlite3.connect(db_path)) as conn:
        types_by_role = conn.execute(
            "SELECT role, response_type FROM message WHERE session_id = ? ORDER BY created_at, id", (SID,)
        ).fetchall()
    assistant_types = [t for role, t in types_by_role if role == "assistant"]
    assert assistant_types[0] is not None, "el tipo inferido del paso de la herramienta quedo guardado"
