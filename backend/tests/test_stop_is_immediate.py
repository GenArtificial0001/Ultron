"""Detener TAJANTE: el turno termina enseguida aunque la herramienta en curso no sepa cortarse sola (pedido del usuario, 2026-10-05).

Bug real: «el boton Detener se queda pegado sin detener». Causa: `_run_turn_impl` ejecuta las tool-calls con `asyncio.gather` y SOLO sigue cuando
TODAS terminaron; `session.stop_requested` se mira entre pasos. Unicamente `bash` (mata el proceso) y `glob`/`grep` cooperan con el stop. Una consulta
a Oracle/SAP/SharePoint/BigQuery o un `webfetch` es una llamada bloqueante en un hilo que nadie puede interrumpir: el usuario apretaba Detener y el
boton quedaba en «Deteniendo…» hasta que la consulta terminaba por su cuenta (minutos).

Solucion verificada aca: tras pedir Detener se le da a las tools un plazo de gracia corto (`STOP_GRACE_S`) para cerrar limpio; pasado el plazo se DEJA DE
ESPERAR la tool (su hilo termina solo, no se mata) y el turno sigue con un resultado «detenido por el usuario» para cada tool que no termino, asi el
historial queda consistente. Una EDICION del mensaje no es un Detener: esa si espera a las tools (spec parada-y-cola-mensajes REQ-004)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from app.llm import loop as loop_module
from app.session.events import SessionState
from app.tools import get_registry
from app.tools.base import Tool
from test_loop_characterization import _Client, _isolate, text, tool_call  # noqa: F401  (_isolate es una fixture autouse reutilizada)

SLOW_TOOL_S = 3.0  # lo que tardaria la consulta lenta si nadie la corta
GRACE_S = 0.3  # plazo de gracia del test (en produccion: loop_module.STOP_GRACE_S)


@pytest.fixture
def slow_tool(monkeypatch):
    """Tool `slow` bloqueante que NO mira stop_requested (como oracle_query o webfetch). El callback `on_start` simula al usuario apretando Detener."""
    monkeypatch.setattr(loop_module, "STOP_GRACE_S", GRACE_S)
    state: dict[str, Any] = {"on_start": lambda session: None, "finished": threading.Event(), "seconds": SLOW_TOOL_S}

    def execute(args, session):
        state["on_start"](session)
        time.sleep(state["seconds"])
        state["finished"].set()
        return "resultado-real-de-la-consulta"

    registry = get_registry()
    registry.register(
        Tool(
            name="slow",
            description="tool lenta de prueba",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
    )
    yield state
    registry._tools.pop("slow", None)
    state["finished"].wait(SLOW_TOOL_S + 2)  # no dejar hilos vivos entre tests


def run_timed(script, session: SessionState | None = None):
    """Corre un turno y devuelve (eventos, sesion, segundos hasta el evento TERMINAL). El cierre de asyncio.run espera a los hilos, por eso no se mide el total."""
    client = _Client(script)
    loop_module.get_client = lambda force_refresh=False, **_k: client
    session = session or SessionState(session_id="")
    started = time.monotonic()
    marks: dict[str, float] = {}

    async def collect():
        out = []
        async for ev in loop_module.run_turn(session, "consulta lenta", "test-session-id"):
            out.append(ev)
            if ev["type"] in ("stopped", "done", "error"):
                marks["terminal"] = time.monotonic() - started
        return out

    events = asyncio.run(collect())
    return events, session, marks.get("terminal", time.monotonic() - started)


def test_stop_during_a_slow_tool_ends_the_turn_without_waiting_for_the_tool(slow_tool):
    slow_tool["on_start"] = lambda session: setattr(session, "stop_requested", True)

    events, _, seconds = run_timed([tool_call("slow", {}), text("no deberia llegar")])

    assert [e["type"] for e in events][-1] == "stopped"
    assert seconds < SLOW_TOOL_S - 1.0, (
        f"el Detener tardo {seconds:.1f}s: esperaba a que terminara la tool ({SLOW_TOOL_S}s)"
    )


def test_the_abandoned_tool_leaves_a_stopped_result_so_the_history_stays_consistent(slow_tool):
    slow_tool["on_start"] = lambda session: setattr(session, "stop_requested", True)

    events, session, _ = run_timed([tool_call("slow", {}, call_id="call_slow")])

    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_slow"], "cada tool-call del asistente necesita su resultado"
    assert "detenido por el usuario" in tool_msgs[0]["content"]
    assert "resultado-real-de-la-consulta" not in tool_msgs[0]["content"]
    assert any(e["type"] == "tool-result" and "detenido" in e["data"]["result"] for e in events)


def test_a_tool_that_closes_within_the_grace_period_keeps_its_real_result(slow_tool):
    slow_tool["seconds"] = GRACE_S / 3
    slow_tool["on_start"] = lambda session: setattr(session, "stop_requested", True)

    _, session, _ = run_timed([tool_call("slow", {})])

    assert next(m for m in session.messages if m.get("role") == "tool")["content"] == "resultado-real-de-la-consulta"


def test_an_edit_is_not_a_stop_the_tool_is_awaited(slow_tool):
    """Editar el mensaje con el agente trabajando conserva el progreso (REQ-004): se espera a la tool y se sigue, no se la abandona."""
    slow_tool["seconds"] = GRACE_S * 3

    def edit(session):
        session.edit_pending_text = "mensaje editado"
        session.stop_requested = True

    slow_tool["on_start"] = edit

    events, session, _ = run_timed([tool_call("slow", {}), text("respuesta tras la edicion")])

    types = [e["type"] for e in events]
    assert "message-edited" in types and types[-1] == "done" and "stopped" not in types
    assert next(m for m in session.messages if m.get("role") == "tool")["content"] == "resultado-real-de-la-consulta"


def test_without_a_stop_a_slow_tool_is_awaited_to_the_end(slow_tool):
    slow_tool["seconds"] = GRACE_S * 3

    events, session, _ = run_timed([tool_call("slow", {}), text("listo")])

    assert [e["type"] for e in events][-1] == "done"
    assert next(m for m in session.messages if m.get("role") == "tool")["content"] == "resultado-real-de-la-consulta"


def test_several_parallel_tools_only_the_unfinished_ones_are_abandoned(slow_tool):
    """Dos tool-calls en el mismo paso: la rapida conserva su resultado, la lenta queda como detenida."""
    registry = get_registry()
    registry.register(
        Tool(
            name="fast",
            description="rapida",
            parameters={"type": "object", "properties": {}},
            execute=lambda a, s: "rapida-ok",
        )
    )
    try:
        slow_tool["on_start"] = lambda session: setattr(session, "stop_requested", True)
        two = [
            *tool_call("fast", {}, call_id="c_fast")[:1],
            *tool_call("slow", {}, call_id="c_slow")[:1],
            *tool_call("slow", {}, call_id="x")[1:],
        ]
        # las dos tool-calls viajan en el MISMO chunk de respuesta del modelo
        from test_loop_characterization import _Chunk, _Choice, _TC

        chunk = _Chunk(_Choice(tool_calls=[_TC(0, "c_fast", "fast", "{}"), _TC(1, "c_slow", "slow", "{}")]))
        two = [chunk, _Chunk(_Choice(finish_reason="tool_calls"))]

        _, session, _ = run_timed([two])

        by_id = {m["tool_call_id"]: m["content"] for m in session.messages if m.get("role") == "tool"}
        assert by_id["c_fast"] == "rapida-ok"
        assert "detenido por el usuario" in by_id["c_slow"]
    finally:
        registry._tools.pop("fast", None)
