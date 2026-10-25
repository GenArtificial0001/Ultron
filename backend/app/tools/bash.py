"""
Tool `bash`: ejecuta un comando de shell real en el sistema del usuario.

Replica el rol de packages/core/src/tool/bash.ts de OpenCode pero SIN el
sistema de permisos "ask" -- el usuario definio explicitamente "acceso
total sin friccion" para esta v1 (ver AGENTS.md del proyecto). Cada
ejecucion queda igual auditada en logs/audit.log (ver app/tools/base.py,
`_audit`), pero no bloquea esperando confirmacion humana.

Directorio por defecto (arquitectura carpeta-por-sesion, definitiva): si
no se especifica `workdir`, corre en la carpeta de PROYECTO de la sesion
actual (ver app/tools/filesystem.py:_base_dir_for_session, mismo criterio
reusado aca) -- NUNCA en `backend/workspace/` (ese era el sandbox
generico viejo, mezclaba trabajo real de sesiones distintas con el propio
codigo fuente de Vision). Se puede pasar cualquier otro `workdir`
absoluto si hace falta operar fuera de esa carpeta."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import BASE_DIR
from app.tools.base import CURRENT_TOOL_CALL_ID, Tool, emit_from_thread, mark_duration_cut
from app.tools.filesystem import _base_dir_for_session

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 1_000_000
DEFAULT_TIMEOUT = 120
# Cada cuanto se mira si el usuario apreto Detener mientras corre el comando.
_POLL_SECONDS = 0.25
# Cuanto se espera, despues de cerrar el arbol de procesos, para juntar la
# salida que haya quedado.
_DRAIN_SECONDS = 5

# REQ-004 (spec fix-detener-y-progreso-visible): salida EN VIVO. Reporte real del usuario (2026-10-11): un script de transcripcion corria
# minutos y la pantalla solo decia «Ejecutando comando… 91%» -- no habia forma de distinguir «trabajando» de «colgado». Mientras corre el
# comando se avisa, a lo sumo cada `_LIVE_OUTPUT_INTERVAL_S` y solo si llego salida nueva, con las ultimas lineas (las que veria una
# consola). Acotado a proposito: es una señal de vida para una persona, no un segundo canal de la salida completa (esa sigue yendo
# entera al modelo, sin cambios).
_LIVE_OUTPUT_INTERVAL_S = 1.0
_LIVE_TAIL_LINES = 8
_LIVE_TAIL_MAX_CHARS = 1500
# Solo se guarda (y decodifica) el FINAL de la salida para armar las ultimas lineas: no se copia el buffer completo cada segundo.
_LIVE_TAIL_SCAN_BYTES = 8192
_READ_CHUNK_BYTES = 65_536

# REQ real (usuario 2026-09-16, "quede mal con esta limitacion"): sin esto,
# `python` dentro de un comando de `bash` resolvia al Python que sea que
# este PRIMERO en el PATH del proceso -- en esta maquina, por pura
# casualidad, un Python global que YA tenia pandas/numpy/scipy/matplotlib
# instalados. En la maquina de otra persona (o si ese Python global no
# tiene nada de eso, o ni existe), `requirements.txt` NO protege nada,
# porque ese archivo solo llena el `.venv` PROPIO del backend (el que
# `Iniciar-Vision.bat` crea/instala automaticamente la primera vez) -- un
# `.venv` que `bash` nunca miraba. Se antepone la carpeta Scripts/bin de
# ESE `.venv` al PATH del subproceso para que `python`/`pip` dentro de
# `bash` sean SIEMPRE el mismo entorno versionado por requirements.txt, en
# cualquier maquina donde se levante Vision con el .bat de siempre.
_VENV_BIN_DIR = BASE_DIR / ".venv" / ("Scripts" if platform.system() == "Windows" else "bin")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if _VENV_BIN_DIR.is_dir():
        env["PATH"] = f"{_VENV_BIN_DIR}{os.pathsep}{env.get('PATH', '')}"
    return env


@dataclass
class _RunResult:
    returncode: int | None
    output: bytes
    reason: str  # "done" | "timeout" | "stopped" | "error"
    error: str | None = None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Cierra el proceso Y todo lo que lanzo. En Windows, matar solo cmd.exe
    deja vivo al python.exe (u otro) que corria adentro -- sigue trabajando y
    con la salida abierta, asi que la espera no termina: `taskkill /T` cierra
    el arbol completo."""
    if proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _live_tail(tail_bytes: bytes, cut_at_start: bool) -> str:
    """REQ-004: las ultimas lineas de la salida, como las veria una consola (maximo `_LIVE_TAIL_LINES` y `_LIVE_TAIL_MAX_CHARS`).

    `cut_at_start`: el pedazo no empieza al principio de la salida, asi que su primera linea esta cortada y se descarta (salvo que sea la
    unica). Una linea reescrita con retorno de carro (barras de avance de pip/tqdm: «\\r 50%», «\\r 100%») muestra su ultimo estado.
    Se decodifica igual que el resultado final (UTF-8 con reemplazo): nunca lanza."""
    text = tail_bytes.decode(errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if cut_at_start and len(lines) > 1:
        lines = lines[1:]
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    tail = "\n".join(lines[-_LIVE_TAIL_LINES:])
    if len(tail) > _LIVE_TAIL_MAX_CHARS:
        tail = tail[-_LIVE_TAIL_MAX_CHARS:]
    return tail


class _OutputReader(threading.Thread):
    """REQ-004: hilo LECTOR de la salida del comando. Hace lo mismo que el hilo interno de `communicate()` en Windows (leer el pipe hasta
    EOF y acumular todo), pero ademas guarda el final de la salida para la vista en vivo. Daemon: si el comando deja un nieto con la
    salida abierta, este hilo no traba el cierre de Vision (igual que los hilos de `communicate`)."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True, name="vision-bash-lector")
        self._stream = stream
        self._chunks: list[bytes] = []
        self._tail = b""
        self._lock = threading.Lock()
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                data = self._stream.read1(_READ_CHUNK_BYTES)
                if not data:
                    return
                with self._lock:
                    self._chunks.append(data)
                    self.total += len(data)
                    self._tail = (self._tail + data)[-_LIVE_TAIL_SCAN_BYTES:]
        except (OSError, ValueError):  # pipe cerrado a la fuerza: lo leido hasta aca queda
            return

    def tail_text(self) -> str:
        with self._lock:
            tail, total = self._tail, self.total
        return _live_tail(tail, cut_at_start=total > len(tail))

    def output(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)


def _run_subprocess_sync(
    shell_exe: str,
    shell_flag: str,
    command: str,
    cwd: str,
    timeout: int,
    should_stop: Callable[[], bool] = lambda: False,
    on_output: Callable[[str], None] | None = None,
) -> _RunResult:
    """Corre el comando de forma SINCRONICA, pensada para un thread aparte via
    `asyncio.to_thread` (ver mas abajo).

    BUG real corregido (deteccion QA 2026-09-14): la version anterior usaba
    `asyncio.create_subprocess_exec`, que en Windows requiere que el loop de
    asyncio activo sea `ProactorEventLoop` -- si el loop es `SelectorEventLoop`
    (lo que pasaba en este entorno bajo `uvicorn --reload`: el WatchFiles
    reloader crea su loop ANTES de que `app.main` llegue a importarse, asi
    que fijar la policy ahi llegaba tarde), tira `NotImplementedError` con
    mensaje vacio y la tool queda completamente inutilizable. `subprocess`
    (sincrono, estandar de la libreria) no pasa por el mecanismo de
    subprocesos de asyncio en absoluto -- funciona igual sin importar que
    tipo de event loop este corriendo.

    BUG real (usuario 2026-09-25, "esta volviendo a demorar el boton
    detener"): con `subprocess.run` el thread quedaba bloqueado hasta que el
    comando terminara o venciera su timeout (120 s por defecto, o lo que
    pidiera el modelo) -- Detener recien actuaba despues, entre pasos del
    loop. Ahora se espera de a `_POLL_SECONDS` y, si el usuario apreto
    Detener (`should_stop`) o se vencio el timeout, se cierra el arbol de
    procesos completo y se devuelve lo que se alcanzo a juntar.

    REQ-004 (spec fix-detener-y-progreso-visible): la salida la lee un hilo lector propio (`_OutputReader`) en vez de `communicate`, para
    poder mirarla MIENTRAS corre y avisar por `on_output` (ultimas lineas, a lo sumo cada `_LIVE_OUTPUT_INTERVAL_S`, solo si hay salida
    nueva). La semantica es la MISMA de antes, punto por punto (`tests/test_bash_live_output.py` la compara contra la implementacion
    anterior): termina bien cuando la salida llego a su fin Y el proceso salio; Detener y el timeout se miran cada `_POLL_SECONDS`; al
    cortar se cierra el arbol y se espera hasta `_DRAIN_SECONDS` por el resto de la salida (si no llega, salida vacia, como hacia
    `communicate` vencido). Un `on_output` que falla nunca rompe el comando."""
    if platform.system() == "Windows":
        # Bug real (2026-09-25): con la lista [cmd.exe, /c, comando], Python
        # "escapa" las comillas del comando con barras (\") que cmd.exe no
        # entiende -- `echo "hola mundo"` imprimia \"hola mundo\" y
        # `python -c "print(1)"` o cualquier ruta con espacios entre comillas
        # (ej. las carpetas de OneDrive) se rompian. `cmd /s /c "<comando>"`
        # como linea de comando LITERAL: /s hace que cmd quite solo las
        # comillas de afuera y ejecute el comando tal cual lo escribio el modelo.
        args: str | list[str] = f'{shell_exe} /s {shell_flag} "{command}"'
        popen_kwargs: dict = {}
    else:
        args = [shell_exe, shell_flag, command]
        popen_kwargs = {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_subprocess_env(),
            **popen_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- se devuelve como texto a la tool
        return _RunResult(None, b"", "error", str(exc))
    reader = _OutputReader(proc.stdout)
    reader.start()
    deadline = time.monotonic() + timeout
    live = {"last_at": None, "seen_total": 0}

    def _maybe_emit_live() -> None:
        # REQ-004: solo con salida NUEVA y no mas seguido que el intervalo (el primer aviso sale apenas aparece la salida).
        if on_output is None or reader.total <= live["seen_total"]:
            return
        now = time.monotonic()
        if live["last_at"] is not None and now - live["last_at"] < _LIVE_OUTPUT_INTERVAL_S:
            return
        live["last_at"], live["seen_total"] = now, reader.total
        tail = reader.tail_text()
        if not tail:
            return
        try:
            on_output(tail)
        except Exception:  # noqa: BLE001 -- la vista en vivo es mejor esfuerzo: nunca corta el comando
            logger.debug("No se pudo avisar la salida en vivo del bash", exc_info=True)

    while True:
        # Mismo criterio que `communicate(timeout=_POLL_SECONDS)`: termina cuando la salida llego a su fin Y el proceso salio.
        reader.join(timeout=_POLL_SECONDS)
        if not reader.is_alive():
            try:
                proc.wait(timeout=_POLL_SECONDS)
                _close_stdout(proc)
                return _RunResult(proc.returncode, reader.output(), "done")
            except subprocess.TimeoutExpired:
                pass
        _maybe_emit_live()
        if should_stop():
            reason = "stopped"
        elif time.monotonic() >= deadline:
            reason = "timeout"
        else:
            continue
        break
    _kill_tree(proc)
    # Drenaje: como `communicate(timeout=_DRAIN_SECONDS)`, hasta ese plazo para el fin de la salida y del proceso; si no, salida vacia.
    drain_deadline = time.monotonic() + _DRAIN_SECONDS
    reader.join(timeout=_DRAIN_SECONDS)
    output = b""
    if not reader.is_alive():
        try:
            proc.wait(timeout=max(0.0, drain_deadline - time.monotonic()))
            output = reader.output()
            _close_stdout(proc)
        except subprocess.TimeoutExpired:
            pass
    return _RunResult(proc.returncode, output, reason)


def _close_stdout(proc: subprocess.Popen) -> None:
    """Cierra el pipe de salida (lo hacia `communicate`). Solo se llama con el lector ya terminado: cerrar un pipe que otro hilo esta
    leyendo puede quedar esperando a esa lectura."""
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except OSError:
        pass


def _limit_output(output: str) -> tuple[str, bool]:
    if len(output.encode("utf-8", errors="replace")) <= MAX_OUTPUT_BYTES:
        return output, False
    return (
        output[: MAX_OUTPUT_BYTES // 2]
        + "\n... (salida truncada) ...\n"
        + output[-MAX_OUTPUT_BYTES // 2 :],
        True,
    )


async def _bash_execute(args: dict, session=None) -> str:
    command = args.get("command", "")
    workdir = args.get("workdir", "")
    timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
    if not command:
        return "Error: falta el argumento 'command'."

    cwd = Path(workdir) if workdir else _base_dir_for_session(session)
    cwd.mkdir(parents=True, exist_ok=True)

    shell_exe = "cmd.exe" if platform.system() == "Windows" else "/bin/bash"
    shell_flag = "/c" if platform.system() == "Windows" else "-c"

    # Detener (stop real, no una edicion del mensaje en curso: la edicion
    # espera a que el comando termine y sigue con el mismo progreso, ver
    # parada-y-cola-mensajes REQ-004) corta el comando al instante.
    def should_stop() -> bool:
        return bool(getattr(session, "stop_requested", False)) and (
            getattr(session, "edit_pending_text", None) is None
        )

    # REQ-004 (spec fix-detener-y-progreso-visible): la salida en vivo va a la tarjeta de progreso de ESTA tool-call (el bucle fija su
    # id en CURRENT_TOOL_CALL_ID). Se avisa desde el hilo del comando con `emit_from_thread` (push_event no es thread-safe). Sin id de
    # tool-call (llamada directa, tests) no hay a quien atribuirla y no se avisa nada.
    call_id = CURRENT_TOOL_CALL_ID.get()
    on_output: Callable[[str], None] | None = None
    if call_id and session is not None and hasattr(session, "push_event"):

        def on_output(tail: str) -> None:
            emit_from_thread(session, "tool-output", {"id": call_id, "name": "bash", "tail": tail})

    result = await asyncio.to_thread(
        _run_subprocess_sync,
        shell_exe,
        shell_flag,
        command,
        str(cwd),
        timeout,
        should_stop,
        on_output=on_output,
    )
    # REQ-006: un comando cortado (timeout o Detener) no tardo "eso": no cuenta para estimar la proxima barra de bash.
    if result.reason in ("timeout", "stopped"):
        mark_duration_cut()
    if result.error is not None:
        return f"Error lanzando el proceso: {result.error}"

    output, truncated = _limit_output(result.output.decode(errors="replace"))
    if result.reason == "timeout":
        return (
            f"Error: el comando excedio el timeout de {timeout}s y fue terminado "
            f"(junto con los procesos que habia lanzado).\n{output}"
        ).rstrip()
    if result.reason == "stopped":
        return (
            "[detenido por el usuario] El comando se cancelo al presionar Detener: se "
            f"cerro el proceso y todo lo que habia lanzado.\n{output}"
        ).rstrip()

    header = f"[exit code: {result.returncode}] [cwd: {cwd}]"
    if truncated:
        header += " [salida truncada]"
    return f"{header}\n{output}"


bash_tool = Tool(
    name="bash",
    description=(
        "Ejecuta un comando de shell en el sistema del usuario (cmd.exe en Windows). "
        "Corre con la autoridad completa del usuario -- usar con cuidado. Si no se "
        "especifica 'workdir', corre en el workspace por defecto del agente. El "
        "Python del sistema (invocable como `python`) tiene instalado pandas, "
        "numpy, openpyxl, scipy, sympy, matplotlib, seaborn, plotly, streamlit, "
        "bokeh y dash -- para leer/analizar/graficar un .xlsx, .csv u otro archivo "
        "de datos binario que la tool `read` (solo texto plano) no puede parsear, "
        "escribi un script Python (con `write`) y corre con `python script.py` en "
        "vez de pedirle al usuario que lo convierta a CSV a mano."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Comando a ejecutar"},
            "workdir": {
                "type": "string",
                "description": "Directorio de trabajo (opcional)",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout en segundos",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    },
    execute=_bash_execute,
    dangerous=True,
)
