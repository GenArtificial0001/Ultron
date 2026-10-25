"""Coordinacion del inicio y cierre de sesion de Outlook y Teams (spec office-outlook-teams-mcp REQ-M02, REQ-M09, REQ-M11, REQ-M14).

Por que existe: iniciar sesion es una accion LARGA (abre el navegador y espera a que la persona entre con su cuenta de la empresa) y la pantalla tiene que poder preguntar «¿como va?» sin
quedarse bloqueada. Este modulo es el unico dueno de ese estado: arranca el login en una tarea de fondo, impide que haya dos a la vez, guarda si esta esperando, conectado o con error, y
atiende el cierre de sesion. Las rutas `/api/m365/*` y las tools `m365_*` solo hablan con el.

Reglas:
- UN solo login a la vez: un segundo toque en «Iniciar sesion» no abre otra ventana ni deja dos tareas esperando.
- El estado que sale hacia la pantalla NUNCA lleva el token ni el texto crudo de un error: un fallo se traduce a su frase (`errors.describe`) y el crudo va al log sin secretos.
- Iniciar sesion lo hace SIEMPRE la persona, desde la pantalla. Ninguna herramienta lo dispara (una herramienta sin sesion recibe `NeedsLogin`, ver `client.py`).
- Cerrar sesion borra el token, corta un login en espera y se puede repetir sin problema.

El estado vive en memoria del proceso: si Vision se reinicia, vuelve a leerse de `token_store` (hay token = conectado). Un login a medias no sobrevive al reinicio, y esta bien."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp import ClientSession

from app.m365 import auth, errors, token_store

logger = logging.getLogger(__name__)

# El `runner` hace el login de verdad (abre el navegador y espera). Los tests inyectan uno falso.
Runner = Callable[[int, "auth.LoginFlow"], Awaitable[None]]

_state: dict[str, Any] = {"phase": "idle", "message": "", "code": ""}
_task: asyncio.Task[None] | None = None
_flow = auth.LoginFlow()


def reset_for_tests() -> None:
    """Deja el servicio como recien arrancado (solo para los tests)."""
    global _task, _flow
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    _flow = auth.LoginFlow()
    _state.update({"phase": "idle", "message": "", "code": ""})


async def _real_runner(port: int, flow: auth.LoginFlow) -> None:
    """El login de verdad: el cliente OAuth del SDK abre el navegador, espera el codigo que vuelve por `/api/m365/callback` y guarda el token cifrado."""
    provider = auth.build_provider(port, flow)
    async with auth.open_transport(auth.connector_url(), provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()  # dispara el 401, el registro del cliente, el navegador y el canje del codigo


def status() -> dict[str, Any]:
    """Lo que ve la pantalla. `state`: connected | waiting | error | disconnected. Jamas el token."""
    phase = _state["phase"]
    if phase == "waiting":
        return {"state": "waiting", "connected": False, "message": "", "code": ""}
    if phase == "error":
        return {"state": "error", "connected": False, "message": _state["message"], "code": _state["code"]}
    connected = token_store.has_session()
    return {"state": "connected" if connected else "disconnected", "connected": connected, "message": "", "code": ""}


async def _run(port: int, runner: Runner) -> None:
    try:
        await runner(port, _flow)
        _state.update({"phase": "idle", "message": "", "code": ""})
    except asyncio.CancelledError:
        _state.update({"phase": "idle", "message": "", "code": ""})
        raise
    except BaseException as exc:  # noqa: BLE001 -- un fallo del login (incluidos los grupos de excepciones del SDK) se traduce a una frase, nunca llega crudo a la pantalla
        cause = _leaf(exc)
        friendly = errors.describe(cause)
        logger.warning("Microsoft 365: fallo el inicio de sesion (%s)", errors.technical_detail(cause))
        _state.update({"phase": "error", "message": friendly.message, "code": friendly.code})


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        for inner in exc.exceptions:
            if isinstance(inner, errors.M365Error):
                return inner
        exc = exc.exceptions[0]
    return exc


async def start_login(port: int, runner: Runner | None = None) -> dict[str, Any]:
    """Empieza el inicio de sesion en segundo plano y devuelve el estado. Si ya hay uno esperando, no abre otro."""
    global _task
    if _task is not None and not _task.done():
        return status()
    _state.update({"phase": "waiting", "message": "", "code": ""})
    _task = asyncio.create_task(_run(port, runner or _real_runner))
    return status()


async def wait_idle() -> None:
    """Espera a que termine el login en curso (lo usan los tests; la pantalla consulta `status()`)."""
    task = _task
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def sign_out() -> dict[str, Any]:
    """Cierra la sesion: corta un login en espera y borra el token. Repetirlo no hace nada."""
    global _task
    task = _task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- se cancelo a proposito
            pass
    _task = None
    _state.update({"phase": "idle", "message": "", "code": ""})
    token_store.sign_out()
    return status()


def deliver_callback(code: str, state: str) -> bool:
    """El navegador volvio con el codigo. Solo cuenta si hay un login en curso y el `state` es el suyo."""
    if _task is None or _task.done():
        return False
    return _flow.deliver(code=code, state=state)


def deliver_error(state: str, reason: str) -> bool:
    """El navegador volvio con un rechazo (la persona cancelo o la empresa no lo permitio). Mismo criterio que `deliver_callback`."""
    if _task is None or _task.done():
        return False
    return _flow.fail(state=state, reason=reason)
