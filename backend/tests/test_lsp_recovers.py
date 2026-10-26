"""spec lsp-se-recupera-solo: si `pylsp` se cae, el cliente lo reinicia (acotado) en vez de quedarse mudo.

Usa el pylsp REAL del venv (un proceso local, sin red): lo importante es que el proceso muera de verdad. Se saltea
si pylsp no esta instalado."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import lsp

# Arrancan el pylsp REAL varias veces (hasta 5 por test). En una PC lenta o con el antivirus revisando un entorno recien instalado,
# cada arranque tarda ~10 s y el cliente ahora lo espera (ver lsp._INIT_TIMEOUT): el limite general de 60 s por test no alcanza.
pytestmark = [
    pytest.mark.skipif(lsp._find_pylsp_command() is None, reason="pylsp no esta instalado"),
    pytest.mark.timeout(300),
]

BAD = "import os\nx = (\n"  # sintaxis rota: pylsp siempre reporta algo


@pytest.fixture
def py_file(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(BAD, encoding="utf-8")
    return f


def run(coro):
    return asyncio.run(coro)


async def started() -> lsp._LspClient:
    c = lsp._LspClient()
    await c.start()
    return c


def kill(c: lsp._LspClient) -> None:
    c._proc.kill()
    c._proc.wait()


# REQ-007
def test_a_live_pylsp_gives_diagnostics_and_never_restarts(py_file):
    async def go():
        c = await started()
        try:
            first = c._proc
            r1 = await c.diagnose(py_file)
            r2 = await c.diagnose(py_file)
            return r1, r2, c._proc is first, c.available
        finally:
            await c.stop()

    r1, r2, same, avail = run(go())
    assert r1 and r2 and same and avail


# REQ-001 + REQ-003
def test_after_pylsp_dies_the_same_call_restarts_it_and_still_returns_diagnostics(py_file, caplog):
    async def go():
        c = await started()
        try:
            await c.diagnose(py_file)
            kill(c)
            dead_available = c.available
            with caplog.at_level(logging.WARNING, logger="app.lsp"):
                result = await c.diagnose(py_file)
            return dead_available, result, c.available
        finally:
            await c.stop()

    dead_available, result, avail_after = run(go())
    assert dead_available is False, "available debe reflejar que el proceso ya murio"
    assert result, "el agente no debe perder el aviso del archivo que acaba de escribir"
    assert avail_after is True


# REQ-006
def test_each_restart_leaves_one_plain_log_line(py_file, caplog):
    async def go():
        c = await started()
        try:
            kill(c)
            with caplog.at_level(logging.WARNING, logger="app.lsp"):
                await c.diagnose(py_file)
        finally:
            await c.stop()

    run(go())
    hits = [r for r in caplog.records if "se reinicio" in r.getMessage()]
    assert len(hits) == 1 and hits[0].exc_info is None


# REQ-002
def test_restarts_are_capped_inside_the_window(py_file, monkeypatch):
    async def go():
        c = await started()
        try:
            outcomes = []
            for _ in range(lsp.MAX_RESTARTS + 2):
                kill(c)
                outcomes.append(bool(await c.diagnose(py_file)))
            return outcomes, c._restart_count_in_window()
        finally:
            await c.stop()

    outcomes, in_window = run(go())
    assert outcomes[: lsp.MAX_RESTARTS] == [True] * lsp.MAX_RESTARTS
    assert outcomes[lsp.MAX_RESTARTS :] == [False, False], "pasado el tope no se reintenta (sin bucles de arranque)"
    assert in_window == lsp.MAX_RESTARTS


def test_after_the_window_passes_it_can_restart_again(py_file, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(lsp, "_now", lambda: clock["t"])  # reloj propio del cliente: el de asyncio no se toca

    async def go():
        c = await started()
        try:
            for _ in range(lsp.MAX_RESTARTS):
                kill(c)
                await c.diagnose(py_file)
            kill(c)
            blocked = bool(await c.diagnose(py_file))
            clock["t"] += lsp.RESTART_WINDOW_S + 1
            recovered = bool(await c.diagnose(py_file))
            return blocked, recovered
        finally:
            await c.stop()

    blocked, recovered = run(go())
    assert blocked is False and recovered is True


# REQ-004
def test_nothing_restarts_after_stop(py_file):
    async def go():
        c = await started()
        await c.stop()
        c._proc.wait()
        result = await c.diagnose(py_file)
        return result, c.available, c._proc.poll() is not None

    result, available, still_dead = run(go())
    assert result == [] and available is False and still_dead


def test_ensure_running_itself_refuses_to_restart_after_stop():
    """Defensa en profundidad: aunque otro camino llame a `_ensure_running` directo, tras stop() no relanza."""
    async def go():
        c = await started()
        await c.stop()
        c._proc.wait()
        return await c._ensure_running(), c._restarts

    restarted, restarts = run(go())
    assert restarted is False and restarts == []


def test_a_broken_pipe_between_the_check_and_the_write_is_recovered_in_the_same_call(py_file, monkeypatch):
    """La carrera real del log (BrokenPipeError): el proceso parece vivo al chequear y muere al escribir.
    El primer envio falla, se remata el proceso y el segundo intento lo reinicia: la llamada igual devuelve diagnosticos."""
    async def go():
        c = await started()
        try:
            real_send, calls = c._send, {"n": 0}

            async def flaky(payload):
                if payload.get("method") == "textDocument/didOpen":
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise BrokenPipeError(32, "Broken pipe")
                return await real_send(payload)

            c._send = flaky
            return await c.diagnose(py_file), calls["n"], c.available
        finally:
            await c.stop()

    result, attempts, available = run(go())
    assert result, "tras el pipe roto la llamada debia recuperarse y devolver diagnosticos"
    assert attempts == 2 and available is True


def test_the_dead_process_pipes_are_closed_so_nothing_leaks(py_file):
    """Sin cerrar el stdin del proceso muerto, el BufferedWriter se recolecta y falla en silencio
    (`OSError: Invalid argument ignored`), que la suite trata como error (PytestUnraisableExceptionWarning)."""
    async def go():
        c = await started()
        try:
            old = c._proc
            kill(c)
            await c.diagnose(py_file)
            return old.stdin.closed, c._proc is not old
        finally:
            await c.stop()

    old_stdin_closed, replaced = run(go())
    assert replaced and old_stdin_closed


# REQ-005
def test_without_pylsp_everything_degrades_silently(py_file, monkeypatch):
    monkeypatch.setattr(lsp, "_find_pylsp_command", lambda: None)

    async def go():
        c = lsp._LspClient()
        await c.start()
        return await c.diagnose(py_file), c.available

    assert run(go()) == ([], False)


def test_a_failing_restart_never_raises(py_file, monkeypatch):
    async def go():
        c = await started()
        try:
            kill(c)

            def boom(*_a, **_k):
                raise OSError("no se puede arrancar")

            monkeypatch.setattr(lsp.subprocess, "Popen", boom)
            return await c.diagnose(py_file), c.available
        finally:
            c._command = None  # evita relanzar en stop()
            await c.stop()

    result, available = run(go())
    assert result == [] and available is False


# ULTRON (hallazgo 2026-10-25): un pylsp que tarda en arrancar ya no se come el primer diagnostico.
_SLOW_FAKE_PYLSP = r'''
import json, sys, time
inp, out = sys.stdin.buffer, sys.stdout.buffer
def read():
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if line.startswith(b"Content-Length:"):
            length = int(line.split(b":", 1)[1])
    return json.loads(inp.read(length))
def send(msg):
    body = json.dumps(msg).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()
while True:
    m = read()
    if m is None:
        break
    if m.get("method") == "initialize":
        time.sleep(6)  # mas que _DIAGNOSTICS_TIMEOUT (5 s): el caso de una PC lenta
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {}}})
    elif m.get("method") == "textDocument/didOpen":
        uri = m["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": uri, "diagnostics": [{"message": "diagnostico del pylsp lento", "severity": 1,
                         "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]}})
'''


def test_a_slow_starting_pylsp_still_gives_the_first_diagnostics(tmp_path):
    import sys as _sys

    fake = tmp_path / "pylsp_lento.py"
    fake.write_text(_SLOW_FAKE_PYLSP, encoding="utf-8")
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    async def go():
        c = lsp._LspClient()
        c._command = [_sys.executable, str(fake)]
        await c.start()
        try:
            return await c.diagnose(target)
        finally:
            await c.stop()

    diags = run(go())
    assert [d["message"] for d in diags] == ["diagnostico del pylsp lento"]


def test_pylsp_runs_as_a_module_of_the_same_python_so_moving_the_folder_never_breaks_it(monkeypatch):
    """Bug real 2026-10-26: al renombrar la carpeta del proyecto, `pylsp.exe` del entorno virtual seguia apuntando al Python
    de la ruta vieja y los diagnosticos salian vacios. Se lanza como `python -m pylsp`, sin rutas guardadas."""
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert lsp._find_pylsp_command() == [sys.executable, "-m", "pylsp"]
