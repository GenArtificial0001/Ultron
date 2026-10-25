"""«Ejecutar ahora» de un agente dispara el turno sin explotar con `no running event loop` (bug real 2026-10-21).

El usuario toco «Ejecutar ahora» en un agente y vio «No se pudo ejecutar el agente. Vision tuvo un problema interno»; el log decia `RuntimeError: no running event loop` en
`_launch_turn` -> `asyncio.create_task()`. Causa: `POST /api/agents/{id}/run` estaba definido con `def` (sincrono); FastAPI corre esos endpoints en un hilo de un pool, que NO tiene un
event loop de asyncio, y `fire_agent()` -> `_launch_turn()` necesita crear una tarea. La regla ya estaba escrita en AGENTS.md («un endpoint que hace `asyncio.create_task()` debe ser
`async def`») y ya se habia aplicado a `/loop`; aca faltaba. El reloj de agentes (`run_agents_loop`) no fallaba porque corre dentro del event loop.

Como se prueba: el endpoint se llama como lo hace el servidor real (FastAPI corre un `def` en un hilo SIN loop; un `async def` en el loop). Nada toca la red ni el modelo:
`_launch_turn` se reemplaza por un espia que hace lo mismo que el real a este respecto (pedir el loop en ejecucion)."""

from __future__ import annotations

import asyncio
import inspect
import threading
from types import SimpleNamespace

import pytest

from app import main
from app.persistence import master_index as mi
from app.scheduler import agents as ag
from app.session.events import get_session_store


@pytest.fixture
def spy_launch(monkeypatch):
    """Reemplaza `main._launch_turn` por algo que PIDE el loop en ejecucion, igual que el real (`asyncio.create_task`), y deja constancia de que corrio y desde donde."""
    seen = {"turns": [], "in_loop_thread": []}

    def fake_launch(session, session_id, turn):
        asyncio.get_running_loop()  # lo mismo que hace asyncio.create_task(): sin loop en este hilo, falla con RuntimeError
        seen["turns"].append(turn.text)
        seen["in_loop_thread"].append(threading.current_thread() is threading.main_thread())

    monkeypatch.setattr(main, "_launch_turn", fake_launch)
    return seen


@pytest.fixture
def an_agent(tmp_master_db):
    """Un agente de tipo skill (su prompt no depende de archivos de la carpeta de la sesion) en una sesion que no esta ocupada."""
    session_id = "sesion-del-agente"
    get_session_store().get_or_create(session_id)
    agent = mi.create_agent(
        "agente-1",
        "Reporte horario",
        session_id,
        "skill",
        [],
        "mi-skill",
        "",
        "recurring",
        {"rule": "nhours", "n": 1},
        True,
    )
    return agent


# ------------------------------------------------------------------------------------------------------------------ REQ-001: el endpoint corre en el event loop
def test_the_run_endpoint_is_async_so_it_runs_inside_the_event_loop():
    """La regla de AGENTS.md: un endpoint que termina en `asyncio.create_task()` tiene que ser `async def`."""
    assert inspect.iscoroutinefunction(main.run_agent_now_endpoint), (
        "con `def` FastAPI lo corre en un hilo sin event loop y explota"
    )


def test_calling_run_now_the_way_the_server_does_fires_the_agent(an_agent, spy_launch):
    """Reproduce el caso real: se llama al endpoint dentro de un event loop en marcha (como lo hace FastAPI con un `async def`)."""

    async def call():
        return await main.run_agent_now_endpoint(an_agent["id"])

    result = asyncio.run(call())

    assert result == {"started": True}
    assert spy_launch["turns"], "el primer turno del agente se lanzo"


def test_the_bug_itself_a_sync_call_from_a_thread_without_a_loop_fails_like_in_the_log(an_agent, spy_launch):
    """Control del propio test: lo que hacia FastAPI con el `def` (llamar `run_agent_now` en un hilo sin loop) reproduce EXACTAMENTE el error del log. Si esto dejara de fallar, los tests de arriba no probarian nada."""
    errors: list[BaseException] = []

    def in_a_pool_thread():
        try:
            ag.run_agent_now(an_agent["id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=in_a_pool_thread)
    thread.start()
    thread.join()

    assert len(errors) == 1 and isinstance(errors[0], RuntimeError) and "no running event loop" in str(errors[0])


# ------------------------------------------------------------------------------------------------------------------ REQ-002: no cambia lo que ya andaba
def test_run_now_still_marks_the_agent_as_fired_so_a_once_agent_does_not_fire_again(an_agent, spy_launch):
    async def call():
        return await main.run_agent_now_endpoint(an_agent["id"])

    asyncio.run(call())

    assert mi.get_agent(an_agent["id"])["last_fired_at"] is not None


def test_run_now_of_an_unknown_agent_answers_404_not_500(tmp_master_db, spy_launch):
    from fastapi import HTTPException

    async def call():
        return await main.run_agent_now_endpoint("no-existe")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(call())

    assert caught.value.status_code == 404
    assert spy_launch["turns"] == [], "no se lanzo nada"


def test_run_now_on_a_busy_session_queues_the_turns_instead_of_failing(an_agent, spy_launch):
    """Con la sesion ocupada los turnos se encolan (no se lanza otro): ese camino no pide el loop y tiene que seguir igual."""
    session = get_session_store().get_or_create(an_agent["session_id"])
    session.current_task = SimpleNamespace(done=lambda: False)  # la sesion «esta ocupada»

    async def call():
        return await main.run_agent_now_endpoint(an_agent["id"])

    result = asyncio.run(call())

    assert result == {"started": True}
    assert spy_launch["turns"] == [], "no se lanzo un turno nuevo: se encolo"


# ------------------------------------------------------------------------------------------------------------------ REQ-003: ningun otro endpoint tiene el mismo defecto
def test_no_sync_endpoint_of_main_reaches_asyncio_create_task():
    """Barrido estatico: ningun endpoint `def` de `main.py` llega (directa o indirectamente) a `asyncio.create_task`/`_launch_turn`, **tambien a traves de los modulos que importa**
    (`app/scheduler/agents.py`, `alarms.py`): el caso real vivia ahi, en `run_agent_now` -> `fire_agent` -> `_launch_sequence` -> `_launch_turn`, y un barrido que solo miraba `main.py` no lo veia.
    Evita que vuelva a pasar con otro endpoint."""
    import ast
    from pathlib import Path

    root = Path(main.__file__).resolve().parent
    trees = {}
    for rel in ("main.py", "scheduler/agents.py", "scheduler/alarms.py"):
        trees[rel] = ast.parse((root / rel).read_text(encoding="utf-8"))
    funcs: dict[str, ast.AST] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(node.name, node)
    spawning = {"create_task", "_launch_turn", "_maybe_continue_loop", "_start_next_queued"}

    def called(node) -> set[str]:
        out: set[str] = set()
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                f = call.func
                out.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
        return out

    # Una funcion `async def` corre en el event loop: llamar a `create_task` desde ahi es correcto, asi que NO propaga el riesgo a quien la espera.
    reaching = {name for name, node in funcs.items() if isinstance(node, ast.FunctionDef) and called(node) & spawning}
    changed = True
    while changed:
        changed = False
        for name, node in funcs.items():
            if isinstance(node, ast.FunctionDef) and name not in reaching and called(node) & reaching:
                reaching.add(name)
                changed = True

    offenders = []
    for node in ast.walk(trees["main.py"]):
        if not isinstance(node, ast.FunctionDef):  # solo los `def` sincronos: los `async def` ya corren en el loop
            continue
        for deco in node.decorator_list:
            is_route = isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in {"get", "post", "put", "delete", "patch"}
            if is_route and called(node) & (reaching | spawning):
                offenders.append(f"{node.name} (main.py:{node.lineno})")
    assert offenders == [], f"endpoints `def` que crean tareas asyncio y explotan con «no running event loop»: {offenders}"
