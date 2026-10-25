"""REQ-004 (spec fix-detener-y-progreso-visible): salida EN VIVO del bash.

Reporte real del usuario (2026-10-11 ~23:06): «no sabia como estaba avanzando entonces no se si esta en un bucle infinito». Un script de
transcripcion corria varios minutos y la pantalla solo decia «Ejecutando comando… 91%». Ahora, mientras corre, la tool lee la salida con
un hilo lector y, si llego salida nueva, avisa a lo sumo cada ~1 s con las ultimas lineas (~8, maximo ~1500 caracteres). Lo que NO puede
cambiar (contrato que se prueba aca contra la implementacion anterior, copiada como ORACULO de referencia):
  * el resultado final que ve el modelo (exit code, salida completa, stderr mezclado, decodificacion, truncado);
  * Detener sigue cortando el comando (y sus hijos) en ~1 s; el timeout tambien.

Comandos REALES e inocuos (un python que imprime lineas con pausas) en un `tmp_path`, igual que `test_tool_bash.py`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.tools import base as tool_base
from app.tools import bash
from app.tools.base import ToolRegistry

pytestmark = pytest.mark.skipif(
    " " in sys.executable, reason="rutas con espacios: cmd /c necesitaria otro quoteo"
)

SHELL = ("cmd.exe", "/c") if os.name == "nt" else ("/bin/bash", "-c")


def _script(tmp_path, name, body) -> str:
    if " " in str(tmp_path):
        pytest.skip("rutas con espacios: cmd /c necesitaria otro quoteo")
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f"{sys.executable} -u {path}"


def _run_sync(command, cwd, timeout=60, should_stop=lambda: False, on_output=None):
    return bash._run_subprocess_sync(
        SHELL[0], SHELL[1], command, str(cwd), timeout, should_stop, on_output=on_output
    )


PRINT_SLOWLY = """import time
for i in range({n}):
    print(f"linea {{i}}", flush=True)
    time.sleep({pause})
"""


# ---------------------------------------------------------------------------------------------------------------------
# El callback de salida en vivo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_live_callback_sees_the_lines_while_the_command_is_still_running(tmp_path):
    command = _script(tmp_path, "lento.py", PRINT_SLOWLY.format(n=8, pause=0.45))
    calls: list[tuple[float, str]] = []
    started = time.monotonic()

    result = _run_sync(command, tmp_path, on_output=lambda tail: calls.append((time.monotonic(), tail)))
    ended = time.monotonic()

    assert result.reason == "done" and result.returncode == 0
    assert len(calls) >= 2, calls
    first_at, first_tail = calls[0]
    assert first_at - started < 2.0, "la primera salida tiene que verse enseguida, no al final"
    assert ended - first_at > 1.0, "el aviso llego cuando el comando todavia corria"
    assert "linea 0" in first_tail
    assert "linea" in calls[-1][1] and calls[-1][1] != first_tail  # la salida avanza


def test_the_live_callback_is_throttled_to_about_once_per_second(tmp_path):
    command = _script(tmp_path, "rapido.py", PRINT_SLOWLY.format(n=40, pause=0.1))
    stamps: list[float] = []
    _run_sync(command, tmp_path, on_output=lambda tail: stamps.append(time.monotonic()))
    assert len(stamps) >= 2
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert min(gaps) >= 0.85, gaps  # «a lo sumo cada ~1 s»


def test_without_new_output_there_is_no_new_notice(tmp_path):
    command = _script(tmp_path, "una.py", 'import time\nprint("una sola", flush=True)\ntime.sleep(3)\n')
    calls: list[str] = []
    _run_sync(command, tmp_path, on_output=calls.append)
    assert calls == ["una sola"]


def test_the_tail_keeps_only_the_last_lines(tmp_path):
    body = 'import time\nfor i in range(50):\n    print(f"linea {i}")\nimport sys; sys.stdout.flush()\ntime.sleep(1.5)\n'
    command = _script(tmp_path, "muchas.py", body)
    calls: list[str] = []
    _run_sync(command, tmp_path, on_output=calls.append)
    assert calls, "el comando quedo vivo 1,5 s despues de imprimir: tenia que avisar"
    tail = calls[-1]
    lines = tail.splitlines()
    assert 1 <= len(lines) <= 8, lines
    assert lines[-1] == "linea 49"
    assert "linea 0\n" not in tail + "\n" and "linea 10" not in tail


def test_the_tail_is_capped_in_characters(tmp_path):
    body = 'import time, sys\nsys.stdout.write("x" * 5000 + "\\n"); sys.stdout.flush()\ntime.sleep(1.5)\n'
    command = _script(tmp_path, "ancha.py", body)
    calls: list[str] = []
    _run_sync(command, tmp_path, on_output=calls.append)
    assert calls and 0 < len(calls[-1]) <= 1500


def test_a_progress_line_rewritten_with_carriage_return_shows_its_last_state(tmp_path):
    body = (
        'import time, sys\nfor p in (10, 50, 100):\n    sys.stdout.write(f"\\rprogreso {p}%"); sys.stdout.flush(); time.sleep(0.05)\n'
        'sys.stdout.write("\\n"); sys.stdout.flush(); time.sleep(1.5)\n'
    )
    command = _script(tmp_path, "cr.py", body)
    calls: list[str] = []
    _run_sync(command, tmp_path, on_output=calls.append)
    assert calls and calls[-1].splitlines()[-1] == "progreso 100%"


def test_a_failing_callback_never_breaks_the_command(tmp_path):
    command = _script(tmp_path, "ok.py", PRINT_SLOWLY.format(n=3, pause=0.5))

    def broken(tail):
        raise RuntimeError("el aviso fallo")

    result = _run_sync(command, tmp_path, on_output=broken)
    assert result.reason == "done" and b"linea 2" in result.output


# ---------------------------------------------------------------------------------------------------------------------
# Detener y timeout: la misma semantica de antes
# ---------------------------------------------------------------------------------------------------------------------
def test_stop_still_cuts_the_command_in_about_a_second(tmp_path):
    command = _script(tmp_path, "eterno.py", PRINT_SLOWLY.format(n=10_000, pause=0.1))
    pressed: dict[str, float] = {}
    first_output = threading.Event()

    def on_output(tail):
        first_output.set()

    def press_stop():
        first_output.wait(20)
        pressed["at"] = time.monotonic()

    presser = threading.Thread(target=press_stop)
    presser.start()
    result = _run_sync(command, tmp_path, should_stop=lambda: "at" in pressed, on_output=on_output)
    returned = time.monotonic()
    presser.join()

    assert result.reason == "stopped"
    assert returned - pressed["at"] < 2.5, "Detener tiene que cortar en ~1 s (poll de 0,25 s + cerrar el arbol)"
    assert b"linea 0" in result.output


def test_timeout_still_cuts_the_command(tmp_path):
    command = _script(tmp_path, "eterno2.py", PRINT_SLOWLY.format(n=10_000, pause=0.1))
    started = time.monotonic()
    result = _run_sync(command, tmp_path, timeout=2, on_output=lambda tail: None)
    assert result.reason == "timeout"
    assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------------------------------------------------
# Resultado final identico: la implementacion ANTERIOR (communicate cada 0,25 s) es el oraculo de referencia
# ---------------------------------------------------------------------------------------------------------------------
def _reference_run(shell_exe, shell_flag, command, cwd, timeout, should_stop=lambda: False, **_ignored):
    """Copia textual del `_run_subprocess_sync` de la v0.27.12 (antes del hilo lector). Solo se usa para comparar resultados."""
    import platform

    if platform.system() == "Windows":
        args = f'{shell_exe} /s {shell_flag} "{command}"'
        popen_kwargs: dict = {}
    else:
        args = [shell_exe, shell_flag, command]
        popen_kwargs = {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=bash._subprocess_env(), **popen_kwargs
        )
    except Exception as exc:  # noqa: BLE001
        return bash._RunResult(None, b"", "error", str(exc))
    deadline = time.monotonic() + timeout
    while True:
        try:
            output, _ = proc.communicate(timeout=0.25)
            return bash._RunResult(proc.returncode, output or b"", "done")
        except subprocess.TimeoutExpired:
            if should_stop():
                reason = "stopped"
            elif time.monotonic() >= deadline:
                reason = "timeout"
            else:
                continue
        bash._kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = b""
        return bash._RunResult(proc.returncode, output or b"", reason)


def _commands(tmp_path) -> list[str]:
    (tmp_path / "bytes.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'ok \\xff\\xfe fin\\r\\nsegunda\\n')\n", encoding="utf-8"
    )
    (tmp_path / "grande.py").write_text(
        "import sys\nfor i in range(20000):\n    sys.stdout.write(f'fila {i} ' + 'y' * 20 + '\\n')\n", encoding="utf-8"
    )
    (tmp_path / "mezcla.py").write_text(
        "import sys, time\nprint('a', flush=True)\nsys.stderr.write('error!\\n'); sys.stderr.flush()\n"
        "time.sleep(0.6)\nprint('b')\nsys.exit(3)\n",
        encoding="utf-8",
    )
    py = sys.executable
    return [
        "echo hola-vision",
        "exit 3",
        f"{py} {tmp_path / 'bytes.py'}",
        f"{py} {tmp_path / 'grande.py'}",
        f"{py} {tmp_path / 'mezcla.py'}",
    ]


def test_the_final_result_is_identical_to_the_previous_implementation(tmp_path):
    if " " in str(tmp_path):
        pytest.skip("rutas con espacios")
    for command in _commands(tmp_path):
        expected = _reference_run(SHELL[0], SHELL[1], command, str(tmp_path), 60)
        got = _run_sync(command, tmp_path, on_output=lambda tail: None)
        assert (got.returncode, got.output, got.reason, got.error) == (
            expected.returncode,
            expected.output,
            expected.reason,
            expected.error,
        ), command


def test_the_text_the_model_sees_is_identical_to_the_previous_implementation(tmp_path, monkeypatch):
    if " " in str(tmp_path):
        pytest.skip("rutas con espacios")
    for command in _commands(tmp_path):
        args = {"command": command, "workdir": str(tmp_path)}
        new_text = asyncio.run(bash._bash_execute(args, None))
        with monkeypatch.context() as mp:
            mp.setattr(bash, "_run_subprocess_sync", _reference_run)
            old_text = asyncio.run(bash._bash_execute(args, None))
        assert new_text == old_text, command


# ---------------------------------------------------------------------------------------------------------------------
# De la tool a la sesion: evento `tool-output` con el id de la tool-call
# ---------------------------------------------------------------------------------------------------------------------
class _RecordingSession:
    def __init__(self):
        self.session_id = ""
        self.stop_requested = False
        self.edit_pending_text = None
        self.events: list[tuple[str, dict, int]] = []

    def push_event(self, event_type, data):
        self.events.append((event_type, data, threading.get_ident()))


def test_the_bash_tool_pushes_tool_output_with_the_id_of_its_tool_call(tmp_path, monkeypatch):
    command = _script(tmp_path, "vivo.py", PRINT_SLOWLY.format(n=5, pause=0.5))
    monkeypatch.setattr(tool_base, "_audit", lambda *a, **k: None)
    reg = ToolRegistry()
    reg.register(bash.bash_tool)
    session = _RecordingSession()

    async def scenario():
        loop_thread = threading.get_ident()
        # el loop de tool-calling fija el id de la tool-call alrededor de cada dispatch (ContextVar, ver llm/loop.py)
        tool_base.CURRENT_TOOL_CALL_ID.set("call_vivo")
        out = await reg.dispatch("bash", json.dumps({"command": command, "workdir": str(tmp_path)}), session)
        await asyncio.sleep(0)  # deja correr los avisos agendados con call_soon_threadsafe
        return out, loop_thread

    out, loop_thread = asyncio.run(scenario())

    live = [(data, thread) for kind, data, thread in session.events if kind == "tool-output"]
    assert live, session.events
    assert all(data["id"] == "call_vivo" and data["name"] == "bash" for data, _ in live)
    assert all(thread == loop_thread for _, thread in live), "push_event solo desde el hilo del event loop (asyncio.Queue)"
    assert "linea 0" in live[0][0]["tail"]
    assert out.startswith("[exit code: 0]") and "linea 4" in out


def test_without_a_tool_call_id_nothing_is_pushed(tmp_path, monkeypatch):
    """Llamada directa (tests, comandos internos): sin id de tool-call no hay a quien atribuir la salida."""
    command = _script(tmp_path, "vivo2.py", PRINT_SLOWLY.format(n=3, pause=0.5))
    session = _RecordingSession()
    out = asyncio.run(bash._bash_execute({"command": command, "workdir": str(tmp_path)}, session))
    assert out.startswith("[exit code: 0]")
    assert not [e for e in session.events if e[0] == "tool-output"]


def test_the_session_object_without_push_event_is_tolerated(tmp_path):
    command = _script(tmp_path, "vivo3.py", PRINT_SLOWLY.format(n=2, pause=0.6))
    out = asyncio.run(bash._bash_execute({"command": command, "workdir": str(tmp_path)}, SimpleNamespace(session_id="")))
    assert out.startswith("[exit code: 0]")
