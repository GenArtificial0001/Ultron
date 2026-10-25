"""REQ-002 / REQ-003 / REQ-004 / REQ-006 (spec fix-detener-y-progreso-visible): lo que el bucle de tool-calling le avisa a la pantalla.

Diagnostico real (turno de 23:06:53 a 23:13:05, 34 herramientas): el 74 % del tiempo fue el modelo pensando y ESCRIBIENDO archivos grandes
como argumento de una herramienta (un `write` de 15 KB tardo ~69 s), y la pantalla no lo mostraba:
  * «Pensando…» solo se emitia en el PRIMER paso: la espera al modelo entre pasos se veia como el comando anterior (REQ-002);
  * mientras el modelo escribia los argumentos no llegaba ningun evento: el `tool-call` sale recien al final del stream (REQ-003);
  * la salida del comando no se veia (REQ-004): el bucle tiene que decirle a la tool cual es el id de su tool-call;
  * un «pensar» cortado por Detener se registraba como duracion real (REQ-006).

Cliente de chat FALSO con un guion por paso (mismo patron que `test_loop_characterization.py`); un paso puede ser una funcion (corre al
pedir ese paso) o una lista de chunks donde un numero es una PAUSA en segundos antes del siguiente chunk (el stream corre en un hilo, ver
`_stream_chat_completion`). Sin red, sin modelo real, sin persistencia (session_id vacio)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import pytest

from app.llm import loop as loop_module
from app.session.events import SessionState


class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, index, id_, name, arguments):
        self.index, self.id, self.function = index, id_, _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []


class _Choice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.delta, self.finish_reason = _Delta(content, tool_calls), finish_reason


class _Chunk:
    def __init__(self, choice):
        self.choices = [choice]


class _Stream:
    def __init__(self, items):
        self._items = list(items)

    def __iter__(self):
        for item in self._items:
            if isinstance(item, (int, float)):
                time.sleep(item)
                continue
            yield item

    def close(self):  # pragma: no cover
        pass


def text(s):
    return [_Chunk(_Choice(content=s)), _Chunk(_Choice(finish_reason="stop"))]


def tool_call(name, arguments, call_id="call_1"):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return [_Chunk(_Choice(tool_calls=[_TC(0, call_id, name, args)])), _Chunk(_Choice(finish_reason="tool_calls"))]


def streamed_tool_call(name, arguments: str, pieces: int, pause: float = 0.0, call_id="call_1", index=0):
    """El modelo escribe los argumentos de a pedazos: primero el id+nombre, despues fragmentos del JSON (como el proveedor de IA)."""
    size = max(1, len(arguments) // pieces + 1)
    out: list[Any] = [_Chunk(_Choice(tool_calls=[_TC(index, call_id, name, None)]))]
    for i in range(0, len(arguments), size):
        if pause:
            out.append(pause)
        out.append(_Chunk(_Choice(tool_calls=[_TC(index, None, None, arguments[i : i + size])])))
    out.append(_Chunk(_Choice(finish_reason="tool_calls")))
    return out


class _Completions:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def create(self, **kwargs):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return _Stream(step() if callable(step) else step)


class _Client:
    def __init__(self, script):
        self.completions = _Completions(script)
        self.chat = type("Chat", (), {"completions": self.completions})()


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
    monkeypatch.setattr(loop_module, "get_budget_ms", lambda op: (1234.0, True))


@pytest.fixture
def thinking_records(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(loop_module, "record_duration", lambda op, ms: calls.append((op, ms)))
    return calls


def run(script, user_message="hola", session=None, **kwargs):
    """Devuelve (eventos, sesion, tiempos de llegada de cada evento, eventos fuera de banda de la sesion)."""
    client = _Client(script)
    loop_module.get_client = lambda force_refresh=False, **_k: client
    session = session or SessionState(session_id="")

    async def collect():
        q = session.subscribe()
        out, stamps = [], []
        async for ev in loop_module.run_turn(session, user_message, "test-session-id", **kwargs):
            out.append(ev)
            stamps.append(time.monotonic())
        await asyncio.sleep(0)
        pushed = []
        while not q.empty():
            pushed.append(q.get_nowait())
        session.unsubscribe(q)
        return out, stamps, pushed

    events, stamps, pushed = asyncio.run(collect())
    return events, session, stamps, pushed


def of_type(events, kind):
    return [e for e in events if e["type"] == kind]


TODO = {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: «Pensando…» en TODOS los pasos
# ---------------------------------------------------------------------------------------------------------------------
class TestThinkingOnEveryStep:
    def test_every_step_announces_that_the_model_is_thinking(self, thinking_records):
        events, *_ = run([tool_call("todowrite", TODO, "call_a"), tool_call("todowrite", TODO, "call_b"), text("Listo.")])

        thinking = [e["data"] for e in of_type(events, "progress-estimate") if e["data"].get("operation") == "thinking"]
        assert [t["label"] for t in thinking] == [
            "Pensando…",
            "Pensando el próximo paso… (paso 2)",
            "Pensando el próximo paso… (paso 3)",
        ]
        assert all(t["budget_ms"] == 1234.0 and t["has_history"] is True for t in thinking)  # mismo presupuesto "thinking"

    def test_the_next_step_thinking_comes_right_after_the_tool_result(self, thinking_records):
        events, *_ = run([tool_call("todowrite", TODO), text("Listo.")])

        kinds = [(e["type"], e["data"].get("operation")) for e in events]
        after_result = kinds[kinds.index(("tool-result", None)) + 1 :]
        assert after_result[0] == ("progress-estimate", "thinking")

    def test_the_thinking_duration_is_recorded_once_per_turn_as_before(self, thinking_records):
        run([tool_call("todowrite", TODO, "call_a"), tool_call("todowrite", TODO, "call_b"), text("Listo.")])

        assert [op for op, _ in thinking_records if op == "thinking"] == ["thinking"]

    def test_a_thinking_cut_by_stop_is_not_recorded(self, thinking_records):
        """REQ-006: un «pensar» cortado por Detener no es una duracion real."""
        session = SessionState(session_id="")

        def stop_while_streaming():
            session.stop_requested = True
            return text("respuesta que no se termina")

        events, *_ = run([stop_while_streaming], session=session)

        assert events[-1]["type"] == "stopped"
        assert [op for op, _ in thinking_records if op == "thinking"] == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: «tool-writing» mientras llegan los argumentos
# ---------------------------------------------------------------------------------------------------------------------
def _write_args(path: str, kb: int = 20) -> str:
    return json.dumps({"path": path, "content": "x = 1\n" * (kb * 1024 // 6)})


class TestToolWriting:
    def test_one_notice_as_soon_as_the_name_is_known_before_the_tool_call(self, tmp_path):
        args = _write_args(str(tmp_path / "transcribir.txt"))
        events, *_ = run([streamed_tool_call("write", args, pieces=8), text("ok")])

        kinds = [e["type"] for e in events]
        writing = of_type(events, "tool-writing")
        assert writing, kinds
        assert kinds.index("tool-writing") < kinds.index("tool-call")
        first = writing[0]["data"]
        assert first["id"] == "call_1" and first["name"] == "write" and first["chars"] == 0

    def test_notices_are_throttled_to_about_every_half_second_per_call(self, tmp_path):
        args = _write_args(str(tmp_path / "transcribir.txt"))
        events, _, stamps, _ = run([streamed_tool_call("write", args, pieces=12, pause=0.15), text("ok")])

        at = [s for e, s in zip(events, stamps, strict=True) if e["type"] == "tool-writing"]
        assert 2 <= len(at) <= 6, len(at)  # ~1,8 s de argumentos: el primero + ~3 mas, nunca uno por fragmento (12)
        gaps = [b - a for a, b in zip(at, at[1:], strict=False)]
        assert min(gaps) >= 0.4, gaps

    def test_fast_arguments_do_not_flood_the_screen(self, tmp_path):
        args = _write_args(str(tmp_path / "rapido.txt"))
        events, *_ = run([streamed_tool_call("write", args, pieces=50), text("ok")])

        assert len(of_type(events, "tool-writing")) == 1  # todo llego en milisegundos: solo el aviso del nombre

    def test_the_size_grows_and_never_exceeds_the_arguments(self, tmp_path):
        args = _write_args(str(tmp_path / "transcribir.txt"), kb=30)
        events, *_ = run([streamed_tool_call("write", args, pieces=10, pause=0.2), text("ok")])

        sizes = [e["data"]["chars"] for e in of_type(events, "tool-writing")]
        assert sizes == sorted(sizes) and len(set(sizes)) >= 2 and sizes[-1] <= len(args)

    def test_the_path_is_the_basename_once_it_is_complete(self, tmp_path):
        args = json.dumps({"path": "C:\\Proyectos\\algo\\transcribir.py", "content": "y" * 30_000})
        events, *_ = run([streamed_tool_call("write", args, pieces=10, pause=0.15), text("ok")])

        paths = [e["data"]["path"] for e in of_type(events, "tool-writing")]
        assert paths[0] is None  # todavia no llego nada del JSON
        assert paths[-1] == "transcribir.py"
        assert set(paths) <= {None, "transcribir.py"}

    def test_file_path_also_works_with_forward_slashes(self, tmp_path):
        args = json.dumps({"file_path": "/home/u/proyecto/notas.md", "content": "z" * 30_000})
        events, *_ = run([streamed_tool_call("edit", args, pieces=10, pause=0.15), text("ok")])

        assert of_type(events, "tool-writing")[-1]["data"]["path"] == "notas.md"

    def test_a_path_inside_the_content_is_never_taken_as_the_file(self, tmp_path):
        content = 'config = {"path": "falso.py"}\n' * 600
        args = '{"content": ' + json.dumps(content) + ', "path": "real.txt"}'
        events, *_ = run([streamed_tool_call("write", args, pieces=10, pause=0.15), text("ok")])

        paths = [e["data"]["path"] for e in of_type(events, "tool-writing")]
        assert "falso.py" not in paths

    def test_without_a_path_argument_the_path_is_null(self):
        args = json.dumps({"command": "echo " + "a" * 5000})
        events, *_ = run([streamed_tool_call("bash", args, pieces=10, pause=0.15), text("ok")])

        writing = of_type(events, "tool-writing")
        assert writing and all(e["data"]["path"] is None for e in writing)
        assert all(e["data"]["name"] == "bash" for e in writing)

    def test_the_tool_call_and_its_arguments_are_unchanged(self, tmp_path):
        """No se rompe lo que ya funcionaba: el `tool-call` final trae los argumentos completos y la tool corre con ellos."""
        target = tmp_path / "salida.txt"
        args = json.dumps({"path": str(target), "content": "hola\n" * 3000})
        events, session, *_ = run([streamed_tool_call("write", args, pieces=9, pause=0.05), text("ok")])

        call = of_type(events, "tool-call")[0]["data"]
        assert call["arguments"] == args and call["name"] == "write"
        assert target.read_text(encoding="utf-8") == "hola\n" * 3000
        assert events[-1]["type"] == "done"

    def test_two_tool_calls_in_the_same_step_get_their_own_first_notice(self, tmp_path):
        a = streamed_tool_call("write", _write_args(str(tmp_path / "a.txt"), 2), pieces=2, call_id="call_a", index=0)
        b = streamed_tool_call("write", _write_args(str(tmp_path / "b.txt"), 2), pieces=2, call_id="call_b", index=1)
        step = a[:-1] + b  # un solo stream con las dos llamadas, un solo finish
        events, *_ = run([step, text("ok")])

        ids = [e["data"]["id"] for e in of_type(events, "tool-writing")]
        assert "call_a" in ids and "call_b" in ids


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: el bucle le pasa a la tool el id de su tool-call (salida en vivo del bash)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(" " in sys.executable, reason="rutas con espacios: cmd /c necesitaria otro quoteo")
def test_the_live_output_of_bash_reaches_the_session_with_the_id_of_the_tool_call(tmp_path, monkeypatch):
    if " " in str(tmp_path):
        pytest.skip("rutas con espacios")
    from app.tools import base as tool_base

    monkeypatch.setattr(tool_base, "_audit", lambda *a, **k: None)
    script = tmp_path / "vivo.py"
    script.write_text("import time\nfor i in range(5):\n    print(f'linea {i}', flush=True)\n    time.sleep(0.5)\n", encoding="utf-8")
    command = f"{sys.executable} -u {script}"
    events, _, _, pushed = run([tool_call("bash", {"command": command, "workdir": str(tmp_path)}, "call_bash"), text("ok")])

    live = [p["data"] for p in pushed if p["type"] == "tool-output"]
    assert live, [p["type"] for p in pushed]
    assert all(d["id"] == "call_bash" and d["name"] == "bash" for d in live)
    assert any("linea" in d["tail"] for d in live)
    assert of_type(events, "tool-result")[0]["data"]["result"].startswith("[exit code: 0]")


def test_parallel_tool_calls_do_not_mix_their_ids(tmp_path, monkeypatch):
    """Cada tool-call corre en su propia tarea: el id que ve una no es el de la otra."""
    from app.tools import base as tool_base
    from app.tools import get_registry
    from app.tools.base import Tool

    seen: dict[str, str | None] = {}

    async def probe(args, session=None):
        await asyncio.sleep(0.05)
        seen[args["tag"]] = tool_base.CURRENT_TOOL_CALL_ID.get()
        return "ok"

    registry = get_registry()
    registry.register(Tool(name="probe", description="d", parameters={"type": "object", "properties": {}}, execute=probe))
    try:
        step = [
            _Chunk(_Choice(tool_calls=[_TC(0, "call_x", "probe", json.dumps({"tag": "x"}))])),
            _Chunk(_Choice(tool_calls=[_TC(1, "call_y", "probe", json.dumps({"tag": "y"}))])),
            _Chunk(_Choice(finish_reason="tool_calls")),
        ]
        run([step, text("ok")])
    finally:
        registry._tools.pop("probe", None)

    assert seen == {"x": "call_x", "y": "call_y"}
