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
        return [t.to_openai_schema() for t in tools]

    async def dispatch(self, name: str, raw_args: str, session: Any = None) -> str:
        """Ejecuta una tool-call. Nunca lanza -- cualquier excepcion se
        devuelve como texto de error (el modelo la ve como resultado de
        la tool y puede decidir reintentar o informar al usuario), igual
        que el patron `ToolFailure` de OpenCode."""
        tool = self.get(name)
        if tool is None:
            return f"Error: tool '{name}' no existe en el registro."

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
        except Exception as exc:  # el modelo debe ver cualquier error
            logger.exception("Error ejecutando tool '%s'", name)
            return f"Error ejecutando '{name}': {exc}"
        finally:
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
