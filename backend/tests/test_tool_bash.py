"""Unit de la tool `bash`. Se ejecutan comandos REALES pero inocuos (`echo`,
`exit N`, un python que late en un archivo) en un `tmp_path`: es la unica forma
de validar el contrato de subprocess (exit code, cwd, decodificacion) y que
Detener / el timeout cierren el arbol de procesos completo. Los casos de
salida grande o invalida se simulan en el borde `_run_subprocess_sync`."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.tools import bash


def run(args, session=None) -> str:
    return asyncio.run(bash._bash_execute(args, session))


def test_missing_command():
    assert run({}).startswith("Error: falta el argumento 'command'")


def test_echo_returns_header_with_exit_code_and_cwd(tmp_path):
    out = run({"command": "echo hola-vision", "workdir": str(tmp_path)})
    assert out.startswith("[exit code: 0]") and str(tmp_path) in out.splitlines()[0]
    assert "hola-vision" in out


def test_nonzero_exit_code_is_reported(tmp_path):
    out = run({"command": "exit 3", "workdir": str(tmp_path)})
    assert out.startswith("[exit code: 3]")


def test_stderr_is_merged_into_output(tmp_path):
    cmd = "echo a 1>&2" if os.name == "nt" else "echo a >&2"
    assert "a" in run({"command": cmd, "workdir": str(tmp_path)})


def test_workdir_is_created_if_missing(tmp_path):
    target = tmp_path / "nuevo" / "sub"
    run({"command": "echo x", "workdir": str(target)})
    assert target.is_dir()


def test_default_cwd_comes_from_session_project(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "_base_dir_for_session", lambda s: tmp_path / "proyecto-sesion")
    out = run({"command": "echo x"}, session=SimpleNamespace(session_id="s1"))
    assert "proyecto-sesion" in out.splitlines()[0]


def fake_run(result):
    return lambda *a, **k: result


def test_timeout_message(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "_run_subprocess_sync", fake_run(bash._RunResult(None, b"parcial", "timeout")))
    out = run({"command": "sleep 99", "workdir": str(tmp_path), "timeout": 7})
    assert "timeout de 7s" in out and "parcial" in out


def test_launch_error_message(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise FileNotFoundError("cmd.exe no esta")

    monkeypatch.setattr(bash.subprocess, "Popen", broken)
    assert run({"command": "x", "workdir": str(tmp_path)}) == "Error lanzando el proceso: cmd.exe no esta"


def test_output_over_limit_is_truncated_keeping_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "MAX_OUTPUT_BYTES", 100)
    payload = ("A" * 80 + "Z" * 80).encode()
    monkeypatch.setattr(bash, "_run_subprocess_sync", fake_run(bash._RunResult(0, payload, "done")))
    out = run({"command": "x", "workdir": str(tmp_path)})
    assert "[salida truncada]" in out.splitlines()[0]
    assert "... (salida truncada) ..." in out and out.rstrip().endswith("Z")
    assert "A" * 40 in out


def test_invalid_utf8_is_replaced_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "_run_subprocess_sync", fake_run(bash._RunResult(0, b"ok \xff\xfe fin", "done")))
    assert "fin" in run({"command": "x", "workdir": str(tmp_path)})


def test_subprocess_env_prepends_venv_bin_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "_VENV_BIN_DIR", tmp_path)
    assert bash._subprocess_env()["PATH"].startswith(str(tmp_path) + os.pathsep)


def test_subprocess_env_untouched_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(bash, "_VENV_BIN_DIR", tmp_path / "no-existe")
    assert bash._subprocess_env()["PATH"] == os.environ.get("PATH", "")


def test_tool_metadata_is_dangerous_and_requires_command():
    assert bash.bash_tool.dangerous is True
    assert bash.bash_tool.parameters["required"] == ["command"]


@pytest.mark.parametrize("bad", ["abc", None])
def test_invalid_timeout_type_raises_value_error_caught_by_dispatch(bad, tmp_path):
    # `int(...)` sobre un valor no numerico lanza: dispatch() lo convierte en texto de error.
    with pytest.raises((ValueError, TypeError)):
        run({"command": "echo x", "workdir": str(tmp_path), "timeout": bad})


# ---------------------------------------------------------------------------
# Detener y timeout con procesos REALES (bug real 2026-09-25: "esta volviendo a
# demorar el boton detener" mientras corria `python find_sks_l...` en bash).
# El comando lanza un python NIETO (cmd.exe -> python.exe) que late en un
# archivo: si sigue latiendo despues del corte, el arbol no se cerro.
# ---------------------------------------------------------------------------
HEARTBEAT = """import os, sys, time, pathlib
beat = pathlib.Path(sys.argv[1])
i = 0
while True:
    i += 1
    beat.write_text(f"{os.getpid()} {i}")
    time.sleep(0.1)
"""


@pytest.fixture
def heartbeat(tmp_path):
    if " " in sys.executable or " " in str(tmp_path):
        pytest.skip("rutas con espacios: cmd /c necesitaria otro quoteo")
    script = tmp_path / "late.py"
    script.write_text(HEARTBEAT, encoding="utf-8")
    beat = tmp_path / "beat.txt"
    yield f"{sys.executable} {script} {beat}", beat
    # Higiene: si un test fallo y el nieto quedo vivo, se cierra igual.
    if beat.exists():
        pid = beat.read_text().split()[0]
        os.system(f"taskkill /F /PID {pid} >nul 2>&1" if os.name == "nt" else f"kill -9 {pid} 2>/dev/null")


def wait_for_beats(beat, count: int = 3, limit: float = 20.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if beat.exists() and int(beat.read_text().split()[1]) >= count:
            return
        time.sleep(0.05)
    pytest.fail("el proceso de prueba no arranco")


def still_beating(beat) -> bool:
    before = beat.read_text()
    time.sleep(0.8)
    return beat.read_text() != before


def test_stop_cuts_a_running_command_and_its_children_at_once(tmp_path, heartbeat):
    command, beat = heartbeat
    session = SimpleNamespace(session_id="", stop_requested=False, edit_pending_text=None)

    def press_stop():
        wait_for_beats(beat)
        session.stop_requested = True

    presser = threading.Thread(target=press_stop)
    presser.start()
    started = time.monotonic()
    out = run({"command": command, "workdir": str(tmp_path), "timeout": 120}, session)
    presser.join()

    assert out.startswith("[detenido por el usuario]")
    assert time.monotonic() - started < 25  # antes: hasta el timeout (120 s)
    assert not still_beating(beat)  # el python nieto tambien se cerro


def test_timeout_closes_the_whole_process_tree(tmp_path, heartbeat):
    command, beat = heartbeat
    started = time.monotonic()
    out = run({"command": command, "workdir": str(tmp_path), "timeout": 3})
    assert out.startswith("Error: el comando excedio el timeout de 3s")
    assert time.monotonic() - started < 20
    wait_for_beats(beat, count=1, limit=1)
    assert not still_beating(beat)


def test_editing_the_message_does_not_kill_the_running_command(tmp_path):
    # Editar el mensaje en curso tambien marca stop_requested, pero con
    # edit_pending_text: el turno sigue con el mismo progreso, el comando termina.
    session = SimpleNamespace(session_id="", stop_requested=True, edit_pending_text="nuevo texto")
    script = tmp_path / "corto.py"
    script.write_text("import time\ntime.sleep(1)\nprint('termino')\n", encoding="utf-8")
    out = run({"command": f"{sys.executable} {script}", "workdir": str(tmp_path)}, session)
    assert out.startswith("[exit code: 0]") and "termino" in out


def test_large_output_is_read_while_waiting(tmp_path, monkeypatch):
    # Sin leer la salida mientras se espera, un proceso que escribe mucho se
    # traba con el buffer lleno y nunca termina.
    monkeypatch.setattr(bash, "MAX_OUTPUT_BYTES", 1000)
    script = tmp_path / "grande.py"
    script.write_text("import sys\nsys.stdout.write('x' * 3_000_000)\n", encoding="utf-8")
    started = time.monotonic()
    out = run({"command": f"{sys.executable} {script}", "workdir": str(tmp_path), "timeout": 60})
    assert out.startswith("[exit code: 0]") and "[salida truncada]" in out.splitlines()[0]
    assert time.monotonic() - started < 30


# ---------------------------------------------------------------------------
# Comillas dobles en Windows (bug real 2026-09-25): el comando se pasaba como
# lista y Python "escapaba" las comillas con barras que cmd.exe no entiende.
# ---------------------------------------------------------------------------
windows_only = pytest.mark.skipif(os.name != "nt", reason="el quoteo de cmd.exe es propio de Windows")


@windows_only
def test_quoted_text_reaches_cmd_untouched(tmp_path):
    out = run({"command": 'echo "hola mundo"', "workdir": str(tmp_path)})
    assert '"hola mundo"' in out and '\\"' not in out


@windows_only
def test_python_dash_c_with_quotes_works(tmp_path):
    out = run({"command": f'"{sys.executable}" -c "print(40 + 2)"', "workdir": str(tmp_path)})
    assert out.startswith("[exit code: 0]") and "42" in out


@windows_only
def test_ampersand_inside_quotes_is_text_not_a_second_command(tmp_path):
    out = run({"command": 'echo "a & b"', "workdir": str(tmp_path)})
    assert '"a & b"' in out


@windows_only
def test_paths_with_spaces_in_quotes_work(tmp_path):
    folder = tmp_path / "OneDrive - Empresa" / "carpeta con espacios"
    folder.mkdir(parents=True)
    script = folder / "saludo.py"
    script.write_text("print('ruta con espacios ok')\n", encoding="utf-8")
    out = run({"command": f'"{sys.executable}" "{script}"', "workdir": str(tmp_path)})
    assert out.startswith("[exit code: 0]") and "ruta con espacios ok" in out
