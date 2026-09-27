"""Investigacion de rendimiento 2026-09 ("Vision tarda mucho en responder"):
antes, `ToolRegistry.dispatch()` llamaba una tool SINCRONICA directo en el
thread del event loop -- una consulta lenta (Oracle/SharePoint/SAP/webfetch)
de una sesion bloqueaba el streaming SSE de TODAS las demas sesiones activas
al mismo tiempo, no solo la que la pidio. Estos tests prueban, con una tool
sync falsa que duerme, que dos dispatches concurrentes corren en PARALELO
(en threads separados) en vez de en serie -- y que las 4 tools que tocan
`session.push_event()` (asyncio.Queue, no thread-safe desde otro thread)
siguen corriendo en el thread del event loop via `thread_safe=False`.

Sin pytest-asyncio (no es dependencia del proyecto): cada test es un `def`
sincronico que arranca su propio loop con `asyncio.run(...)`.
"""

from __future__ import annotations

import asyncio
import time

from app.tools.base import Tool, ToolRegistry
from app.tools.chart import render_chart_tool
from app.tools.decisions import record_decision_tool
from app.tools.question import question_tool
from app.tools.todowrite import todowrite_tool


def _slow_sync_execute(args: dict, session=None) -> str:
    time.sleep(0.3)
    return "listo"


def test_concurrent_sync_tool_calls_run_in_parallel_not_serial():
    registry = ToolRegistry()
    registry.register(
        Tool(name="slow", description="test", parameters={}, execute=_slow_sync_execute)
    )

    async def _run():
        start = time.monotonic()
        await asyncio.gather(
            registry.dispatch("slow", "{}"),
            registry.dispatch("slow", "{}"),
            registry.dispatch("slow", "{}"),
        )
        return time.monotonic() - start

    elapsed = asyncio.run(_run())
    # En serie serian ~0.9s (3 x 0.3s); en paralelo (threads separados)
    # deberia rondar ~0.3s. Margen generoso para no ser flaky en CI lento.
    assert elapsed < 0.7, f"las tool-calls sync no corrieron en paralelo (tardaron {elapsed:.2f}s)"


def test_slow_sync_tool_does_not_block_a_concurrent_fast_dispatch():
    """Prueba directa del bug reportado: mientras UNA tool lenta esta
    corriendo, OTRO dispatch (simulando otra sesion) no debe esperarla."""
    registry = ToolRegistry()
    registry.register(
        Tool(name="slow", description="test", parameters={}, execute=_slow_sync_execute)
    )
    registry.register(
        Tool(name="fast", description="test", parameters={}, execute=lambda a, s=None: "ok")
    )

    async def _run():
        slow_task = asyncio.create_task(registry.dispatch("slow", "{}"))
        await asyncio.sleep(0.05)  # deja que "slow" arranque primero

        start = time.monotonic()
        result = await registry.dispatch("fast", "{}")
        elapsed = time.monotonic() - start
        await slow_task
        return result, elapsed

    result, elapsed = asyncio.run(_run())
    assert result == "ok"
    assert elapsed < 0.2, f"el dispatch rapido esperó a la tool lenta ({elapsed:.2f}s)"


def test_thread_safe_false_tools_still_work_synchronously_on_loop():
    """todowrite/question/render_chart/record_decision tocan
    session.push_event() -- deben seguir funcionando (thread_safe=False no
    debe romper el resultado, solo evita el worker thread)."""
    registry = ToolRegistry()
    for tool in (todowrite_tool, question_tool, render_chart_tool, record_decision_tool):
        assert tool.thread_safe is False, f"{tool.name} deberia tener thread_safe=False"
        registry.register(tool)

    result = asyncio.run(
        registry.dispatch(
            "todowrite", '{"todos": [{"content": "probar", "status": "pending"}]}'
        )
    )
    assert "probar" in result


def test_default_sync_tool_is_thread_safe_true():
    assert Tool(name="x", description="d", parameters={}, execute=_slow_sync_execute).thread_safe is True
