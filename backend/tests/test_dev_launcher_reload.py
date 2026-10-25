"""REQ-008 (spec fix-detener-y-progreso-visible): el modo desarrollo no se reinicia por instalar paquetes.

Lo que vivio el usuario (2026-10-11, 22:49-22:57): el agente de Vision corrio `python -m pip install python-pptx pywin32`; la tool bash pone el
`.venv` de Vision primero en el PATH, pip escribio `.py` dentro de `backend/.venv` y el `uvicorn --reload` de `Iniciar-Vision.bat` (que vigilaba
TODO `backend/`, su carpeta de trabajo) reinicio Vision a mitad del turno. El proceso viejo siguio con el turno sin aceptar conexiones y Detener
quedo colgado. El filtro por defecto de uvicorn excluye `.*`, pero eso mira solo el NOMBRE del archivo (`__init__.py`), no la carpeta `.venv`.

El contrato que se prueba aca, contra la linea REAL del .bat y el uvicorn REAL instalado (no una copia de su logica):
  * el .bat le pasa a uvicorn `--reload-dir app` (relativo a `backend/`, donde se para con `cd /d "%~dp0backend"`);
  * con esos argumentos, lo que uvicorn vigila es `backend/app` y nada mas: un `.py` que pip escribe en `backend/.venv` o un script que el
    agente guarda en `backend/workspace` NO dispara el reinicio, y un cambio en `backend/app` SI (sigue siendo modo desarrollo).
"""

from __future__ import annotations

import shlex
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from uvicorn.config import Config
from uvicorn.supervisors.watchfilesreload import FileFilter, WatchFilesReload

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "Iniciar-Vision.bat"


def _uvicorn_args_from_launcher() -> list[str]:
    """Argumentos que el .bat le pasa a `python -m uvicorn` (la linea que levanta el servidor, no un comentario REM)."""
    lines = [
        line.strip()
        for line in LAUNCHER.read_text(encoding="utf-8", errors="replace").splitlines()
        if "-m uvicorn" in line and not line.strip().upper().startswith("REM")
    ]
    assert len(lines) == 1, f"se esperaba UNA linea que levante uvicorn en {LAUNCHER.name}, hay {len(lines)}: {lines}"
    after = lines[0].split("-m uvicorn", 1)[1]
    return shlex.split(after, posix=False)


def _fake_backend(tmp_path: Path) -> Path:
    """Arbol minimo con la forma de `backend/`: el codigo, el entorno de Python y la carpeta de trabajo del agente."""
    backend = tmp_path / "backend"
    for sub in ("app", ".venv/Lib/site-packages/pptx", "workspace"):
        (backend / sub).mkdir(parents=True)
    (backend / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    return backend


def _reload_config(args: list[str]) -> Config:
    """Config de uvicorn tal como la arma su CLI con estos argumentos (relativos a la carpeta actual, como en el .bat)."""
    ctx = uvicorn.main.make_context("uvicorn", list(args))
    p = ctx.params
    assert p["reload"], "el .bat de desarrollo tiene que seguir con --reload"
    return Config(
        p["app"],
        reload=True,
        reload_dirs=list(p["reload_dirs"]) or None,
        reload_includes=list(p["reload_includes"]) or None,
        reload_excludes=list(p["reload_excludes"]) or None,
        log_config=None,  # no reconfigura el logging de toda la suite
    )


def _is_watched(config: Config, path: Path) -> bool:
    """Lo mismo que decide WatchFilesReload: el archivo esta dentro de una carpeta vigilada Y pasa su filtro."""
    resolved = path.resolve()
    inside = any(d == resolved or d in resolved.parents for d in config.reload_dirs)
    return inside and FileFilter(config)(resolved)


def test_the_dev_launcher_watches_only_the_vision_code():
    args = _uvicorn_args_from_launcher()
    assert "app.main:app" in args and "--reload" in args
    assert "--reload-dir" in args, "Iniciar-Vision.bat vigila todo backend/ (incluido .venv): falta --reload-dir app"
    dirs = [args[i + 1] for i, a in enumerate(args) if a == "--reload-dir"]
    assert [d.strip('"').replace("\\", "/").rstrip("/") for d in dirs] == ["app"]


def test_with_the_launcher_args_a_package_installed_in_venv_is_not_watched(tmp_path, monkeypatch):
    backend = _fake_backend(tmp_path)
    monkeypatch.chdir(backend)  # el .bat hace `cd /d "%~dp0backend"` antes de levantar uvicorn
    config = _reload_config(_uvicorn_args_from_launcher())

    assert config.reload_dirs == [(backend / "app").resolve()]
    assert not _is_watched(config, backend / ".venv" / "Lib" / "site-packages" / "pptx" / "__init__.py")
    assert not _is_watched(config, backend / "workspace" / "transcribir.py")
    assert _is_watched(config, backend / "app" / "main.py"), (
        "un cambio en el codigo de Vision tiene que seguir reiniciando"
    )


def test_without_reload_dir_uvicorn_would_watch_the_venv_too(tmp_path, monkeypatch):
    """Control: confirma el diagnostico de la spec con el mismo uvicorn (sin la opcion, el filtro `.*` no salva a `.venv`)."""
    backend = _fake_backend(tmp_path)
    monkeypatch.chdir(backend)
    config = _reload_config(["app.main:app", "--reload"])

    assert _is_watched(config, backend / ".venv" / "Lib" / "site-packages" / "pptx" / "__init__.py")


def test_python_m_app_main_also_watches_only_the_vision_code():
    """El otro lanzador con recarga (`python -m app.main`, citado en el docstring de main.py) usa `uvicorn.run(..., reload=True)`: tiene
    que vigilar la misma carpeta que el .bat. Se lee la llamada real del bloque `__main__` sin ejecutarlo (levantaria el servidor)."""
    import ast

    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    main_blocks = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) and "__name__" in ast.unparse(node.test)
    ]
    assert len(main_blocks) == 1
    calls = [n for n in ast.walk(main_blocks[0]) if isinstance(n, ast.Call) and ast.unparse(n.func) == "uvicorn.run"]
    assert len(calls) == 1
    kwargs = {kw.arg: kw.value for kw in calls[0].keywords}
    assert ast.literal_eval(kwargs["reload"]) is True
    assert "reload_dirs" in kwargs, (
        "python -m app.main vigila toda la carpeta actual (incluido .venv): falta reload_dirs"
    )
    value = eval(
        compile(ast.Expression(kwargs["reload_dirs"]), str(main_py), "eval"), {"Path": Path, "__file__": str(main_py)}
    )  # noqa: S307 -- expresion del propio repo
    dirs = [value] if isinstance(value, str | Path) else list(value)
    assert [Path(d).resolve() for d in dirs] == [main_py.parent.resolve()]


def _restart_paths(supervisor: WatchFilesReload, write, seconds: float, *, until_first: bool) -> list[Path]:
    """Llama a `should_restart()` (lo que hace en bucle el supervisor de `uvicorn --reload`) mientras otro hilo escribe archivos cada 0,3 s.
    Escribir en bucle evita depender de cuanto tarda en arrancar el observador de archivos del sistema operativo."""
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set():
            write(n)
            n += 1
            stop.wait(0.3)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    found: list[Path] = []
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            found.extend(supervisor.should_restart() or [])
            if until_first and found:
                break
    finally:
        stop.set()
        thread.join(timeout=5)
    return found


@pytest.mark.timeout(120)
def test_the_real_reloader_ignores_pip_writing_in_venv_but_reloads_on_app_changes(tmp_path, monkeypatch):
    """El observador REAL de uvicorn (watchfiles) con los argumentos del .bat: pip escribiendo en `.venv` y el agente en `workspace` no
    reinician; tocar `app/main.py` si (control positivo: prueba que el observador estaba andando)."""
    backend = _fake_backend(tmp_path)
    monkeypatch.chdir(backend)
    supervisor = WatchFilesReload(
        _reload_config(_uvicorn_args_from_launcher()), target=lambda sockets: None, sockets=[]
    )
    try:

        def pip_and_agent(n: int) -> None:
            (backend / ".venv" / "Lib" / "site-packages" / "pptx" / f"mod{n % 5}.py").write_text(
                f"x = {n}\n", encoding="utf-8"
            )
            (backend / "workspace" / "transcribir.py").write_text(f"x = {n}\n", encoding="utf-8")

        ignored = _restart_paths(supervisor, pip_and_agent, 6, until_first=False)
        shown = sorted({p.resolve().relative_to(backend.resolve()).as_posix() for p in ignored})
        assert ignored == [], f"esto reiniciaria Vision a mitad de un turno: {shown}"

        def edit_code(n: int) -> None:
            (backend / "app" / "main.py").write_text(f"x = {n}\n", encoding="utf-8")

        reloaded = _restart_paths(supervisor, edit_code, 30, until_first=True)
        assert (backend / "app" / "main.py").resolve() in [p.resolve() for p in reloaded]
    finally:
        supervisor.should_exit.set()
        supervisor.watcher.close()  # suelta el observador del sistema operativo (si no, Windows retiene la carpeta temporal)


# ---------------------------------------------------------------------------------------------------------------------
# El reinicio por --reload se CUELGA con una conexion de streaming abierta (spec office-outlook-teams-mcp, hallazgo del 2026-10-17)
# ---------------------------------------------------------------------------------------------------------------------
# Medido con una app minima (cero codigo de Vision) que tiene un stream que nunca termina, igual que `/api/chat/stream/{id}`:
#   * con un stream abierto y SIN `--timeout-graceful-shutdown` el reinicio no ocurre: uvicorn espera para siempre a que la conexion cierre y el worker viejo
#     sigue sirviendo el codigo viejo (o no responde); con `--timeout-graceful-shutdown 3` se reinicia en ~4 s.
# Por eso los DOS lanzadores con recarga tienen que pasarle un limite de espera.
GRACEFUL_SECONDS_MAX = 10  # mas de eso y «apago Vision» se siente colgado; el chat se reengancha solo al volver


def test_the_dev_launcher_limits_how_long_uvicorn_waits_for_open_connections():
    args = _uvicorn_args_from_launcher()

    assert "--timeout-graceful-shutdown" in args, (
        "sin este limite, un chat abierto (stream) cuelga el reinicio de Vision"
    )
    seconds = int(args[args.index("--timeout-graceful-shutdown") + 1])
    assert 1 <= seconds <= GRACEFUL_SECONDS_MAX


def test_the_graceful_option_the_launcher_uses_is_one_the_installed_uvicorn_really_has():
    """Una opcion mal escrita o que la version instalada no conoce hace que uvicorn NO ARRANQUE: el .bat se romperia entero."""
    args = _uvicorn_args_from_launcher()

    ctx = uvicorn.main.make_context(
        "uvicorn", list(args)
    )  # lo mismo que corre el .bat: si la opcion no existe, lanza UsageError

    assert ctx.params["timeout_graceful_shutdown"] == int(args[args.index("--timeout-graceful-shutdown") + 1])


def test_python_m_app_main_also_limits_the_graceful_wait():
    import ast

    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    call = next(n for n in ast.walk(block) if isinstance(n, ast.Call) and ast.unparse(n.func) == "uvicorn.run")
    kwargs = {kw.arg: kw.value for kw in call.keywords}

    assert "timeout_graceful_shutdown" in kwargs, "python -m app.main tambien se cuelga con un chat abierto"
    seconds = ast.literal_eval(kwargs["timeout_graceful_shutdown"])
    assert isinstance(seconds, int) and 1 <= seconds <= GRACEFUL_SECONDS_MAX


def test_both_launchers_use_the_same_graceful_limit():
    """Dos numeros distintos para lo mismo es una trampa: el dia que alguien cambie uno se olvida del otro."""
    import ast

    bat = _uvicorn_args_from_launcher()
    from_bat = int(bat[bat.index("--timeout-graceful-shutdown") + 1])
    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    call = next(n for n in ast.walk(block) if isinstance(n, ast.Call) and ast.unparse(n.func) == "uvicorn.run")
    from_main = ast.literal_eval({kw.arg: kw.value for kw in call.keywords}["timeout_graceful_shutdown"])

    assert from_bat == from_main


# ---------------------------------------------------------------------------------------------------------------------
# El mismo hallazgo, medido con el Vision REAL y una sesion OCUPADA (no con una app minima)
# ---------------------------------------------------------------------------------------------------------------------
def _serve_real_vision_with_a_busy_session(graceful):
    """Levanta `app.main:app` (el Vision real, sin tareas de fondo) con una sesion ocupada y UNA conexion de chat abierta (`/api/chat/stream/{id}` espera eventos sin fin mientras
    hay un turno en curso). Devuelve (servidor, hilo, conexion abierta). `graceful` = `timeout_graceful_shutdown` de uvicorn."""
    import asyncio
    import socket

    from app import main
    from app.session.events import get_session_store

    sid = f"sesion-apagado-{graceful}"
    session = get_session_store().get_or_create(sid)

    async def never() -> None:
        await asyncio.sleep(3600)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(
            main.app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            lifespan="off",
            timeout_graceful_shutdown=graceful,
        )
    )
    ready = threading.Event()

    def run() -> None:
        async def go() -> None:
            session.current_task = asyncio.create_task(never())
            ready.set()
            await server.serve()
            session.current_task.cancel()

        asyncio.run(go())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10)
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    held = socket.create_connection(("127.0.0.1", port), timeout=5)
    held.sendall(f"GET /api/chat/stream/{sid} HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n".encode())
    held.settimeout(3)
    assert b"200" in held.recv(200), "el chat tiene que quedar abierto con la sesion ocupada"
    return server, thread, held


def test_the_real_vision_shuts_down_quickly_with_a_chat_open_when_the_launcher_limit_is_applied():
    from app import main  # noqa: F401  (se importa aca para que un fallo de importacion se vea como error de este test)

    args = _uvicorn_args_from_launcher()
    seconds = int(args[args.index("--timeout-graceful-shutdown") + 1])
    # Primero el rango: con un limite enorme (ej. 600) este test esperaria minutos antes de fallar. Asi falla rapido y con el motivo claro.
    assert 1 <= seconds <= GRACEFUL_SECONDS_MAX, (
        f"el limite del lanzador ({seconds} s) esta fuera de 1..{GRACEFUL_SECONDS_MAX}"
    )
    server, thread, held = _serve_real_vision_with_a_busy_session(seconds)
    try:
        started = time.monotonic()
        server.should_exit = True
        thread.join(timeout=seconds + 8)
        took = time.monotonic() - started

        assert not thread.is_alive(), (
            f"Vision no se apago en {seconds + 8} s con un chat abierto: el reinicio por --reload queda colgado"
        )
        assert took < seconds + 4, (
            f"tardo {took:.1f} s: el limite del lanzador ({seconds} s) no esta cortando la conexion"
        )
    finally:
        held.close()
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


def test_control_without_the_limit_the_real_vision_does_not_shut_down_with_a_chat_open():
    """Control: confirma el diagnostico con el mismo servidor. Sin `timeout_graceful_shutdown`, un chat abierto con un turno en curso impide el apagado (si dejara de colgarse, el
    arreglo ya no haria falta y este test lo avisa)."""
    server, thread, held = _serve_real_vision_with_a_busy_session(None)
    try:
        server.should_exit = True
        thread.join(timeout=6)

        assert thread.is_alive(), (
            "sin el limite, Vision se apago solo: el diagnostico cambio y este arreglo hay que revisarlo"
        )
    finally:
        held.close()
        server.force_exit = True
        thread.join(timeout=5)
