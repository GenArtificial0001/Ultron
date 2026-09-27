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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LANGUAGE_SERVERS: dict[str, str] = {".py": "pylsp"}
MAX_DIAGNOSTICS_PER_FILE = 20
_DIAGNOSTICS_TIMEOUT = 5.0


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

    @property
    def available(self) -> bool:
        return self._command is not None and self._proc is not None

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

        self._reader_thread = threading.Thread(
            target=self._read_loop_sync, daemon=True, name="pylsp-reader"
        )
        self._reader_thread.start()
        self._request_id += 1
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
                if message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    self._diagnostics_by_uri[params.get("uri", "")] = params.get(
                        "diagnostics", []
                    )
        except (OSError, ValueError):
            return
        except Exception:
            logger.warning("Error leyendo mensajes de pylsp", exc_info=True)

    async def diagnose(self, path: Path) -> list[dict[str, Any]]:
        """REQ-001/002: pide diagnosticos frescos sin propagar fallos."""
        if not self.available:
            return []
        uri = path.resolve().as_uri()
        async with self._lock:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
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
                logger.warning("pylsp dejo de responder", exc_info=True)
                return []

    async def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._proc.terminate()
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
