"""
Contrato estandar de "tool" para el agente Vision, inspirado en el diseno
de `Tool.make` de OpenCode (packages/core/src/tool/tool.ts) pero reescrito
en Python puro (sin Effect-TS): cada tool es una funcion sincrona/async
que recibe un dict de argumentos (ya validado contra su JSON Schema) y
devuelve un string (lo que "ve" el modelo como resultado de la tool-call).

Patron: cada tool se registra como una instancia de `Tool`, con:
    name         -> identificador usado en la tool-call del LLM
    description  -> texto que el modelo lee para decidir cuando usarla
    parameters   -> JSON Schema (dict) de los argumentos que acepta
    execute      -> callable(args: dict) -> str (async o sync, ambos ok)
    dangerous    -> True si la tool ejecuta comandos o modifica el
                    filesystem (bash/write/edit) -- se audita en un log
                    aparte, aunque en esta v1 NO se pide confirmacion
                    humana (ver AGENTS.md de este proyecto: "acceso total
                    sin friccion", decision explicita del usuario).

`ToolRegistry` es el equivalente simplificado del `ToolRegistry.Service`
de OpenCode: mantiene el dict nombre->Tool, arma las `definitions` en
formato tool-calling de OpenAI (lo que se manda en `tools=[...]` al
crear el chat completion) y expone `dispatch(name, args)` para ejecutar
una tool-call real.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.tool_timings import record_duration

logger = logging.getLogger(__name__)

# Cada tool recibe (args, session) -- `session` es la SessionState de la
# conversacion actual (app/session/events.py), usada por `todowrite` y
# `question` para reflejar estado en el stream SSE del frontend. Las tools
# que no necesitan estado de sesion simplemente ignoran el 2do parametro.
ToolExecute = Callable[[dict[str, Any], Any], "str | Awaitable[str]"]

# REQ-008 b (spec adjuntar-archivos-chat): una tool sync corre en un hilo aparte
# (`asyncio.to_thread`), pero `session.push_event` usa un `asyncio.Queue` que NO
# es thread-safe. `dispatch` guarda aca el event loop del turno ANTES de salir al
# hilo (`to_thread` copia el contexto), y `emit_from_thread` agenda el push en
# ESE loop con `call_soon_threadsafe`. Asi `read` puede informar el % real de
# parseo (pagina k de N) sin corromper la cola de eventos SSE.
_TURN_LOOP: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = contextvars.ContextVar(
    "vision_turn_loop", default=None
)


# REQ-004 (spec fix-detener-y-progreso-visible): id de la tool-call que se esta ejecutando. Lo fija el bucle de tool-calling
# (`llm/loop.py`, `_dispatch_or_intercept`) alrededor de CADA dispatch: cada tool-call corre en su propia tarea de asyncio, asi que dos
# llamadas en paralelo no se pisan, y `asyncio.to_thread` copia el contexto al hilo de la tool. Lo usa `bash` para atribuir su salida
# en vivo (`tool-output`) a SU tool-call sin cambiar la firma publica `execute(args, session)` de las tools. None = llamada directa.
CURRENT_TOOL_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vision_current_tool_call_id", default=None
)

# REQ-006 (spec fix-detener-y-progreso-visible): bandera que `dispatch` arma para CADA ejecucion. Si la tool avisa que la cortaron
# (bash por timeout o Detener, ver `mark_duration_cut`), su duracion NO se registra en tool_timings: no es lo que tarda esa operacion,
# es lo que tardo hasta que la cortaron (caso real: 106 948 ms de un bash interrumpido inflaron la estimacion de todos los demas).
_DURATION_CUT: contextvars.ContextVar[list[bool] | None] = contextvars.ContextVar(
    "vision_duration_cut", default=None
)


def mark_duration_cut() -> None:
    """La tool en curso se corto (timeout, Detener): su duracion no cuenta para estimar. No-op fuera de `dispatch`."""
    holder = _DURATION_CUT.get()
    if holder is not None:
        holder[0] = True


def _stopped_by_user(session: Any) -> bool:
    """Detener REAL (no la edicion del mensaje en curso, que tambien marca stop_requested pero deja terminar a las tools)."""
    return bool(getattr(session, "stop_requested", False)) and (
        getattr(session, "edit_pending_text", None) is None
    )


def emit_from_thread(session: Any, event_type: str, data: dict[str, Any]) -> None:
    """Empuja un evento SSE desde el hilo de una tool. No-op si no hay loop del
    turno (ej. tests que llaman a la tool directo) o si la sesion no existe."""
    loop = _TURN_LOOP.get()
    if session is None or loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(session.push_event, event_type, data)
    except RuntimeError:  # el loop se cerro entre el chequeo y el agendado
        logger.debug("No se pudo emitir '%s' desde el hilo de la tool", event_type)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: ToolExecute
    dangerous: bool = False
    # Investigacion de rendimiento 2026-09: por defecto, una tool SINCRONICA
    # se corre en un worker thread (`asyncio.to_thread`, ver dispatch() mas
    # abajo) para no bloquear el event loop compartido por todas las
    # sesiones mientras hace I/O real (DB/red/disco). `thread_safe=False`
    # es la excepcion explicita para las pocas tools que tocan
    # `session.push_event()` (backed por un `asyncio.Queue`, NO thread-safe
    # si se llama desde otro thread que no sea el del event loop) --
    # todowrite/question/render_chart/record_decision son sync pero
    # rapidas (sin I/O de red/DB real), asi que quedarse en el loop no
    # cuesta nada y evita corromper la cola de eventos SSE.
    thread_safe: bool = True

    def to_openai_schema(self) -> dict[str, Any]:
        """Formato de definicion de funcion que espera la API de
        chat.completions de OpenAI/el proveedor de IA en el parametro `tools=[...]`."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registro central de tools disponibles para el agente."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def definitions(self, exclude_dangerous: bool = False) -> list[dict[str, Any]]:
        """spec modo-plan-loop REQ-002: `exclude_dangerous=True` (Modo Plan)
        saca del array `tools=[...]` que se manda al LLM las 3 unicas tools
        marcadas `dangerous=True` (bash/write/edit) -- ni siquiera puede
        INTENTARLAS, quedan ausentes del payload real de la API. Mismo
        mecanismo que `resolveTools`/`Permission.disabled` de OpenCode
        (packages/opencode/src/session/llm/request.ts), pero simplificado:
        Vision solo porta la capa de filtro duro, no el sistema de
        permisos interactivos "ask" (decision explicita, ver spec §
        Decisiones registradas)."""
        tools = self._tools.values()
        if exclude_dangerous:
            tools = [t for t in tools if not t.dangerous]
        # spec interruptores-conexiones-skills REQ-001/002: las tools de una conexion APAGADA no se le mandan al modelo (ahorra tokens y evita que busque por ahi).
        from app import switches  # import diferido: `switches` importa conexiones y estas importan tools

        available = switches.availability_filter()  # los interruptores se leen UNA vez por lista, no una por tool
        tools = [t for t in tools if available(t.name)]
        return [t.to_openai_schema() for t in tools]

    async def dispatch(self, name: str, raw_args: str, session: Any = None) -> str:
        """Ejecuta una tool-call. Nunca lanza -- cualquier excepcion se
        devuelve como texto de error (el modelo la ve como resultado de
        la tool y puede decidir reintentar o informar al usuario), igual
        que el patron `ToolFailure` de OpenCode."""
        tool = self.get(name)
        if tool is None:
            return f"Error: tool '{name}' no existe en el registro."

        # REQ-003 (defensa en profundidad): un modelo con una conversacion vieja puede pedir una tool que ya no se le ofrece. Se rechaza ANTES de ejecutar nada.
        from app import switches

        if not switches.tool_is_available(name):
            return (
                f"Error: la herramienta '{name}' esta desactivada porque su conexion esta apagada. "
                "Pedile al usuario que la active en Configuracion > Conexiones (o en el panel rapido)."
            )

        try:
            args: dict[str, Any] = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            return f"Error: argumentos invalidos (no es JSON valido): {exc}"

        if tool.dangerous:
            _audit(tool.name, args)

        # REQ-001 (spec barra-progreso-adaptativa): se mide cuanto tarda
        # REALMENTE esta tool-call, sin importar si termina bien o con
        # error -- ambos casos son informacion util para la proxima
        # estimacion (una tool que suele fallar rapido no deberia inflar
        # el presupuesto de las que funcionan lento).
        start = time.monotonic()
        _TURN_LOOP.set(asyncio.get_running_loop())  # ver emit_from_thread
        cut = [False]  # REQ-006: la tool la marca con mark_duration_cut() si la cortaron
        _DURATION_CUT.set(cut)
        cancelled = False
        try:
            # Bug de rendimiento real (investigacion 2026-09, "Vision tarda
            # mucho"): la mayoria de las tools (oracle_query, bigquery_*,
            # sharepoint_*, sap_query, graph_*, webfetch, read/glob/grep) son
            # funciones `def` SINCRONICAS que hacen I/O real de red/DB. Antes
            # se las llamaba directo (`tool.execute(...)`) y RECIEN despues
            # se chequeaba `isawaitable` -- para una tool sync, eso ya corrio
            # de forma bloqueante en el thread del event loop de asyncio ANTES
            # de llegar a ese chequeo. Como Vision sirve TODAS las sesiones
            # sobre un unico event loop, una consulta lenta de un usuario
            # colgaba el streaming SSE de cualquier otra sesion activa en ese
            # momento. Se decide ANTES de llamar: las async se awaitean
            # directo, las sync corren en un thread aparte (mismo patron que
            # ya usaba bash.py a mano, ver tools/bash.py:76-78).
            if inspect.iscoroutinefunction(tool.execute):
                result = await tool.execute(args, session)
            elif tool.thread_safe:
                result = await asyncio.to_thread(tool.execute, args, session)
            else:
                result = tool.execute(args, session)
            return str(result)
        except asyncio.CancelledError:
            cancelled = True  # REQ-006: tarea cancelada (p. ej. Vision apagandose a mitad de la tool)
            raise
        except Exception as exc:  # el modelo debe ver cualquier error
            logger.exception("Error ejecutando tool '%s'", name)
            return f"Error ejecutando '{name}': {exc}"
        finally:
            # REQ-006 (spec fix-detener-y-progreso-visible): una ejecucion CORTADA (Detener, tarea cancelada, o la propia tool avisa
            # timeout/Detener) no es una duracion real y no se registra. Una que termina normal -- aunque sea con error -- si (como antes).
            # Con la tarea cancelada tampoco se hace el `await`: no hay que seguir trabajando sobre una tarea que se esta cancelando.
            if not (cancelled or cut[0] or _stopped_by_user(session)):
                elapsed_ms = (time.monotonic() - start) * 1000
                await asyncio.to_thread(record_duration, name, elapsed_ms)


def _audit(tool_name: str, args: dict[str, Any]) -> None:
    """Registra en un archivo de auditoria cada ejecucion de una tool
    peligrosa (bash/write/edit). No bloquea la ejecucion -- es solo
    rastro para revision posterior (ver decision de diseno en AGENTS.md:
    'acceso total sin friccion' fue una eleccion consciente del usuario,
    compensada con auditoria pasiva en vez de un gate de aprobacion)."""
    settings = get_settings()
    log_path: Path = settings.audit_log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "args": args,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
