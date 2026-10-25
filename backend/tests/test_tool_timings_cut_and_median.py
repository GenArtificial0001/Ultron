"""REQ-006 (spec fix-detener-y-progreso-visible): mejor estimacion de la barra de progreso.

Reporte real del usuario (2026-10-11): la estimacion de `bash` era la MEDIA de los ultimos 20 bash de cualquier tipo (mezcla un `echo` de
0,2 s con un script de 100 s) y ademas registraba como duracion "real" un bash cortado (106 948 ms de un comando interrumpido por un
reinicio). El contrato que se prueba aca:
  * para `bash` el presupuesto es la MEDIANA de la ventana (una sola corrida larga no infla la estimacion de todos los demas);
  * el resto de las operaciones siguen con la media de siempre;
  * una herramienta CORTADA no se registra: Detener (la sesion queda con `stop_requested` al terminar), la cancelacion de la tarea, o un
    bash que termina por timeout / Detener. Una herramienta que termina normal (incluso con error) se sigue registrando.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import tool_timings as tt
from app.tools import bash as bash_module
from app.tools.base import Tool, ToolRegistry


@pytest.fixture
def timings_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "tool_timings.json"
    monkeypatch.setattr(tt, "TIMINGS_FILE", path)
    return path


def recorded(timings_file) -> dict:
    if not timings_file.exists():
        return {}
    return json.loads(timings_file.read_text(encoding="utf-8"))


class _Session:
    def __init__(self, stop_requested=False, edit_pending_text=None):
        self.session_id = ""
        self.stop_requested = stop_requested
        self.edit_pending_text = edit_pending_text

    def push_event(self, event_type, data):  # pragma: no cover - no se usa en estos tests
        pass


def _registry_with(name, execute, **kw) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(name=name, description="d", parameters={"type": "object", "properties": {}}, execute=execute, **kw))
    return reg


# ---------------------------------------------------------------------------------------------------------------------
# Mediana para bash
# ---------------------------------------------------------------------------------------------------------------------
def test_bash_budget_is_the_median_so_one_long_run_does_not_inflate_it(timings_file):
    for ms in (200, 250, 300, 100_000):
        tt.record_duration("bash", ms)
    budget, has_history = tt.get_budget_ms("bash")
    assert has_history is True
    assert budget == pytest.approx(275.0)  # mediana; la media seria 25 187,5


def test_bash_median_with_an_odd_window(timings_file):
    for ms in (900, 100, 106_948, 300, 200):
        tt.record_duration("bash", ms)
    assert tt.get_budget_ms("bash")[0] == pytest.approx(300.0)


def test_other_operations_keep_the_mean(timings_file):
    for ms in (200, 250, 300, 100_000):
        tt.record_duration("oracle_query", ms)
    assert tt.get_budget_ms("oracle_query")[0] == pytest.approx((200 + 250 + 300 + 100_000) / 4)


# ---------------------------------------------------------------------------------------------------------------------
# Duraciones cortadas: no se registran
# ---------------------------------------------------------------------------------------------------------------------
def test_a_tool_that_ends_normally_is_still_recorded(timings_file):
    reg = _registry_with("normal", lambda args, session=None: "ok")
    assert asyncio.run(reg.dispatch("normal", "{}", _Session())) == "ok"
    assert len(recorded(timings_file).get("normal", [])) == 1


def test_a_tool_that_fails_is_still_recorded(timings_file):
    """Una falla rapida es informacion real (como hoy): no es una herramienta cortada."""

    def boom(args, session=None):
        raise RuntimeError("fallo")

    reg = _registry_with("falla", boom)
    assert asyncio.run(reg.dispatch("falla", "{}", _Session())).startswith("Error ejecutando 'falla'")
    assert len(recorded(timings_file).get("falla", [])) == 1


def test_a_tool_that_was_running_when_the_user_pressed_stop_is_not_recorded(timings_file):
    def execute(args, session=None):
        session.stop_requested = True  # el usuario aprieta Detener mientras corre
        return "a medias"

    reg = _registry_with("lenta", execute)
    asyncio.run(reg.dispatch("lenta", "{}", _Session()))
    assert "lenta" not in recorded(timings_file)


def test_editing_the_message_is_not_a_stop_and_the_duration_is_recorded(timings_file):
    """Editar el mensaje en curso tambien marca stop_requested, pero con texto pendiente: la herramienta NO se corta."""

    def execute(args, session=None):
        session.stop_requested = True
        session.edit_pending_text = "texto nuevo"
        return "termino entera"

    reg = _registry_with("editada", execute)
    asyncio.run(reg.dispatch("editada", "{}", _Session()))
    assert len(recorded(timings_file).get("editada", [])) == 1


def test_a_cancelled_tool_is_not_recorded(timings_file):
    started = asyncio.Event()

    async def forever(args, session=None):
        started.set()
        await asyncio.sleep(3600)
        return "nunca"

    reg = _registry_with("eterna", forever)

    async def scenario():
        task = asyncio.ensure_future(reg.dispatch("eterna", "{}", _Session()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert "eterna" not in recorded(timings_file)


@pytest.mark.parametrize("reason", ["timeout", "stopped"])
def test_a_bash_cut_by_timeout_or_stop_is_not_recorded(timings_file, tmp_path, monkeypatch, reason):
    monkeypatch.setattr(
        bash_module, "_run_subprocess_sync", lambda *a, **k: bash_module._RunResult(None, b"parcial", reason)
    )
    reg = ToolRegistry()
    reg.register(bash_module.bash_tool)
    monkeypatch.setattr("app.tools.base._audit", lambda *a, **k: None)
    out = asyncio.run(reg.dispatch("bash", json.dumps({"command": "x", "workdir": str(tmp_path)}), _Session()))
    assert "parcial" in out
    assert "bash" not in recorded(timings_file)


def test_a_bash_that_ends_normally_is_recorded(timings_file, tmp_path, monkeypatch):
    monkeypatch.setattr(bash_module, "_run_subprocess_sync", lambda *a, **k: bash_module._RunResult(0, b"ok", "done"))
    reg = ToolRegistry()
    reg.register(bash_module.bash_tool)
    monkeypatch.setattr("app.tools.base._audit", lambda *a, **k: None)
    asyncio.run(reg.dispatch("bash", json.dumps({"command": "x", "workdir": str(tmp_path)}), _Session()))
    assert len(recorded(timings_file).get("bash", [])) == 1


def test_a_bash_with_a_nonzero_exit_code_is_still_a_real_duration(timings_file, tmp_path, monkeypatch):
    monkeypatch.setattr(bash_module, "_run_subprocess_sync", lambda *a, **k: bash_module._RunResult(1, b"x", "done"))
    reg = ToolRegistry()
    reg.register(bash_module.bash_tool)
    monkeypatch.setattr("app.tools.base._audit", lambda *a, **k: None)
    asyncio.run(reg.dispatch("bash", json.dumps({"command": "x", "workdir": str(tmp_path)}), _Session()))
    assert len(recorded(timings_file).get("bash", [])) == 1
