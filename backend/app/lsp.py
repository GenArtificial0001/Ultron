"""
Cliente LSP minimo para diagnosticar Python despues de write/edit.

La spec diagnosticos-lsp-loop porta el patron de OpenCode: el diagnostico
se agrega al resultado de la misma tool para que el modelo pueda corregir
el archivo en el turno actual. El servicio es opcional y nunca bloquea una
escritura si pylsp no esta disponible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LANGUAGE_SERVERS: dict[str, str] = {".py": "pylsp"}
MAX_DIAGNOSTICS_PER_FILE = 20
_DIAGNOSTICS_TIMEOUT = 5.0
# ULTRON (hallazgo 2026-10-25): en una PC lenta o con el antivirus revisando un entorno recien instalado, pylsp tardo 10 s en
# responder `initialize`. El cliente mandaba el archivo sin esperarlo y los 5 s de arriba se iban en el arranque: el primer
# diagnostico salia vacio siempre. Ahora espera la respuesta del `initialize` con su propio margen, aparte del de cada archivo.
_INIT_TIMEOUT = 30.0
# lsp-se-recupera-solo REQ-002: si pylsp muere se reinicia, pero acotado -- como mucho MAX_RESTARTS veces dentro de
# RESTART_WINDOW_S segundos, para que un pylsp que no logra arrancar no entre en un bucle de lanzamientos.
MAX_RESTARTS = 3
RESTART_WINDOW_S = 60.0


def _now() -> float:
    """Reloj del cliente (separado para poder simular el paso del tiempo en los tests sin tocar el de asyncio)."""
    return time.monotonic()


def _find_pylsp_command() -> list[str] | None:
    # El EXE autocontenido no trae un pylsp.exe separado: se relanza a sí
    # mismo con un modo interno que ejecuta pylsp sobre stdio.
    if getattr(sys, "frozen", False):
        return [sys.executable, "--vision-pylsp"]
    exe_dir = Path(sys.executable).parent
    candidate = exe_dir / ("pylsp.exe" if sys.platform == "win32" else "pylsp")
    if candidate.exists():
        return [str(candidate)]
    pylsp = shutil.which("pylsp")
    return [pylsp] if pylsp else None


class _LspClient:
    """Cliente JSON-RPC sobre stdio para un proceso pylsp compartido.

    BUG real corregido (deteccion QA 2026-09-14, 2 rondas -- la primera solo
    silenciaba el sintoma): en este entorno (Windows, uvicorn --reload) el
    event loop activo no soporta crear subprocesos via
    `asyncio.create_subprocess_exec` (mismo root cause ya resuelto para la
    tool `bash`, ver bash.py) -- `pylsp` esta instalado y disponible, pero
    JAMAS llegaba a arrancar. Fix real (no solo atajar la excepcion): igual
    que bash.py, se evita el mecanismo de subprocesos de asyncio por
    completo. A diferencia de bash (un comando de una sola pasada, resuelto
    con `asyncio.to_thread(subprocess.run, ...)`), pylsp es un proceso
    bidireccional de LARGA duracion con stdin/stdout abiertos todo el
    tiempo -- por eso ahora usa `subprocess.Popen` (sincrono, arrancado via
    `asyncio.to_thread` para no bloquear el loop en el arranque) con un
    THREAD dedicado leyendo stdout en loop (`_read_loop_sync`, bloqueante
    por diseño) que actualiza `_diagnostics_by_uri` directamente -- seguro
    sin locks adicionales porque el GIL hace atomica cada asignacion de
    clave de dict, y es el mismo patron que ya usaba la version async
    (un solo escritor, el resto del cliente solo lee)."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._diagnostics_by_uri: dict[str, list[dict[str, Any]]] = {}
        self._reader_thread: threading.Thread | None = None
        self._command = _find_pylsp_command()
        self._restarts: list[float] = []  # instantes (monotonic) de los reinicios recientes
        self._ever_started = False  # distingue "murio" de "todavia no arranco" (evita lanzar dos a la vez)
        self._stopped = False  # REQ-004: tras stop() no se reinicia nada
        self._init_id: int | None = None  # id del `initialize` del proceso actual
        self._initialized = threading.Event()  # lo prende el thread lector al llegar la respuesta del `initialize`

    @property
    def available(self) -> bool:
        # REQ-003: refleja la realidad. Antes era True mientras existiera el objeto Popen, aunque el proceso
        # ya hubiera muerto -- por eso nunca se enteraba de que tenia que reiniciarlo.
        return self._command is not None and self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        """REQ-002: inicia pylsp o degrada silenciosamente si falta."""
        if self._command is None:
            logger.warning("pylsp no esta instalado; write/edit siguen sin diagnosticos")
            return
        try:
            self._proc = await asyncio.to_thread(
                subprocess.Popen,
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.warning("No se pudo arrancar pylsp", exc_info=True)
            self._proc = None
            return

        self._ever_started = True
        self._initialized = threading.Event()  # uno nuevo por proceso: el de un pylsp anterior no cuenta
        self._reader_thread = threading.Thread(
            target=self._read_loop_sync, daemon=True, name="pylsp-reader"
        )
        self._reader_thread.start()
        self._request_id += 1
        self._init_id = self._request_id
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": "initialize",
                    "params": {"processId": None, "rootUri": None, "capabilities": {}},
                }
            )
            await self._send(
                {"jsonrpc": "2.0", "method": "initialized", "params": {}}
            )
        except (OSError, ValueError):
            logger.warning("No se pudo inicializar pylsp", exc_info=True)
            self._proc = None

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        stdin = self._proc.stdin
        await asyncio.to_thread(stdin.write, header + body)
        await asyncio.to_thread(stdin.flush)

    def _read_loop_sync(self) -> None:
        """Corre en `self._reader_thread` -- bloqueante por diseño (lecturas
        sincronas de `subprocess.Popen.stdout`), nunca en el loop de
        asyncio. Misma logica de framing LSP (Content-Length + JSON) que la
        version async original, solo que con `.readline()`/`.read()`
        sincronos en vez de `await stream.readline()`/`readexactly()`."""
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                content_length: int | None = None
                while True:
                    header_line = stream.readline()
                    if not header_line:
                        return
                    if header_line in (b"\r\n", b"\n"):
                        break
                    if header_line.startswith(b"Content-Length:"):
                        content_length = int(header_line.split(b":", 1)[1].strip())
                if content_length is None:
                    continue
                raw = stream.read(content_length)
                if len(raw) < content_length:
                    return  # el proceso cerro el stream a mitad del mensaje
                message = json.loads(raw)
                if self._init_id is not None and message.get("id") == self._init_id and "method" not in message:
                    self._initialized.set()
                if message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    self._diagnostics_by_uri[params.get("uri", "")] = params.get(
                        "diagnostics", []
                    )
        except (OSError, ValueError):
            return
        except Exception:
            logger.warning("Error leyendo mensajes de pylsp", exc_info=True)
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def _restart_count_in_window(self) -> int:
        horizon = _now() - RESTART_WINDOW_S
        self._restarts = [t for t in self._restarts if t > horizon]
        return len(self._restarts)

    def _close_stdin(self) -> None:
        """Cierra el pipe de escritura de un proceso que ya no esta. Sin esto el `BufferedWriter` queda abierto y,
        al recolectarse, intenta vaciarse contra un pipe roto (`OSError: Invalid argument` ignorada en silencio)."""
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.close()

    def _kill_sync(self) -> None:
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        self._close_stdin()

    async def _ensure_running(self) -> bool:
        """REQ-001/002/004/005: devuelve True si hay un pylsp vivo, reiniciandolo si hizo falta y esta permitido."""
        if self.available:
            return True
        if self._stopped or self._command is None or not self._ever_started:
            return False
        self._close_stdin()
        if self._restart_count_in_window() >= MAX_RESTARTS:
            return False
        self._restarts.append(_now())
        logger.warning("pylsp se reinicio (el proceso habia terminado)")  # REQ-006: una linea, sin traza
        await self.start()
        return self.available

    async def _wait_until_initialized(self) -> None:
        """Espera (sin bloquear el loop) a que pylsp responda el `initialize`, hasta `_INIT_TIMEOUT`. Si no llega, o el proceso
        muere mientras tanto, sigue igual que antes: el pedido del archivo decide."""
        waited = 0.0
        while not self._initialized.is_set() and waited < _INIT_TIMEOUT and self.available:
            await asyncio.sleep(0.1)
            waited += 0.1

    async def diagnose(self, path: Path) -> list[dict[str, Any]]:
        """REQ-001/002: pide diagnosticos frescos sin propagar fallos. Si pylsp murio lo reinicia (acotado) y
        entrega los diagnosticos de ESTA misma llamada, asi el agente no pierde el aviso del archivo que acaba de
        escribir; antes se quedaba mudo hasta reiniciar Vision."""
        if self._command is None or self._stopped:
            return []
        uri = path.resolve().as_uri()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []  # problema del archivo, no de pylsp: no hay nada que reiniciar
        async with self._lock:
            for attempt in range(2):
                if not await self._ensure_running():
                    return []
                await self._wait_until_initialized()
                try:
                    self._diagnostics_by_uri.pop(uri, None)
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "textDocument/didOpen",
                            "params": {
                                "textDocument": {
                                    "uri": uri,
                                    "languageId": "python",
                                    "version": 1,
                                    "text": text,
                                }
                            },
                        }
                    )
                    waited = 0.0
                    while (
                        uri not in self._diagnostics_by_uri
                        and waited < _DIAGNOSTICS_TIMEOUT
                    ):
                        await asyncio.sleep(0.1)
                        waited += 0.1
                    return self._diagnostics_by_uri.get(uri, [])
                except (OSError, ValueError, ConnectionResetError):
                    # Pipe roto: el proceso murio entre el chequeo y la escritura. Se lo remata (para que
                    # `available` sea False) y el segundo intento lo reinicia; solo el 2o fallo deja traza.
                    logger.warning("pylsp dejo de responder", exc_info=attempt == 1)
                    await asyncio.to_thread(self._kill_sync)
            return []

    async def stop(self) -> None:
        self._stopped = True
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._proc.terminate()
            self._close_stdin()
        # El thread lector es daemon=True: no hace falta unirlo, muere solo
        # apenas `stream.readline()` devuelve vacio (proceso terminado) o el
        # proceso Python principal termina.


_client = _LspClient()


async def start_lsp_client() -> None:
    """REQ-Performance: inicia la unica instancia global."""
    await _client.start()


async def stop_lsp_client() -> None:
    await _client.stop()


def _format_diagnostics(raw: list[dict[str, Any]], path: Path) -> str:
    severity_map = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}
    lines = []
    for diag in raw[:MAX_DIAGNOSTICS_PER_FILE]:
        severity = severity_map.get(diag.get("severity", 1), "ERROR")
        line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
        col = diag.get("range", {}).get("start", {}).get("character", 0) + 1
        lines.append(f"{severity} [{line}:{col}] {diag.get('message', '').strip()}")
    return f'<diagnostics file="{path}">\n' + "\n".join(lines) + "\n</diagnostics>"


async def diagnose_python_file(path: Path) -> str:
    """REQ-001/002/003: formatea errores y warnings solo para Python."""
    if path.suffix.lower() not in LANGUAGE_SERVERS:
        return ""
    try:
        raw = await _client.diagnose(path)
    except Exception:
        logger.warning("Fallo inesperado diagnosticando '%s'", path, exc_info=True)
        return ""
    relevant = [diag for diag in raw if diag.get("severity", 1) in (1, 2)]
    return _format_diagnostics(relevant, path) if relevant else ""
