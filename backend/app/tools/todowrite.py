"""
Tool `todowrite`: el agente arma/actualiza una lista de tareas visible en
la UI (sidebar del frontend), replicando el rol de la tool TodoWrite de
OpenCode -- investigado su codigo real (packages/schema/src/session-todo.ts,
packages/opencode/src/tool/todowrite.txt) antes de extenderlo: OpenCode usa
una lista PLANA (content/status/priority, sin jerarquia). Este proyecto
amplia eso a proposito (pedido explicito del usuario, "esto va a ayudar a
organizar hasta a la IA misma"): cada tarea principal puede tener
`subtasks` (MISMO shape, 2 niveles fijos -- una sub-tarea NUNCA tiene sus
propias subtasks, ver validacion mas abajo) para desglosar trabajo con
partes identificables (ejercicios de una actividad, secciones de un
dashboard, pasos de una instalacion) sin aplanarlos como items hermanos
sueltos que pierden la relacion "pertenece a".

El estado vive en `SessionState.todos` (en memoria, sin persistencia
propia -- se persiste aparte en SQLite, ver mas abajo) y cada actualizacion
se empuja como evento SSE `type: "todo"` para que el frontend refresque el
panel sin polling.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import Tool

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


def _clean_todo_item(item: dict, *, allow_subtasks: bool) -> dict:
    """Normaliza un item de todo (o de subtask, cuando allow_subtasks=False
    -- evita que el LLM anide un 3er nivel, que no tiene soporte visual ni
    de negocio: la spec es EXPLICITAMENTE de 2 niveles fijos)."""
    status = item.get("status", "pending")
    if status not in VALID_STATUSES:
        status = "pending"
    cleaned = {
        "content": str(item.get("content", "")),
        "status": status,
        "priority": item.get("priority", "medium"),
    }
    if allow_subtasks:
        raw_subtasks = item.get("subtasks", [])
        cleaned["subtasks"] = (
            [_clean_todo_item(s, allow_subtasks=False) for s in raw_subtasks]
            if isinstance(raw_subtasks, list)
            else []
        )
    return cleaned


def _todowrite_execute(args: dict, session: Any = None) -> str:
    todos = args.get("todos", [])
    if not isinstance(todos, list):
        return "Error: 'todos' debe ser una lista de objetos {content, status, priority, subtasks?}."

    cleaned: list[dict] = [
        _clean_todo_item(item, allow_subtasks=True) for item in todos
    ]

    if session is not None:
        session.todos = cleaned
        session.push_event("todo", {"todos": cleaned})
        # persistencia-sqlite-sesiones REQ-002: el checklist tambien se
        # persiste (tabla `todo`) para que sobreviva a un reinicio del
        # backend igual que el resto de la sesion -- reemplazo completo
        # (DELETE + INSERT) porque todowrite siempre manda la lista
        # entera, no deltas.
        if session.session_id:
            from app.persistence import (
                service as persistence,
            )  # import diferido: evita ciclo

            persistence.record_todos(session.session_id, cleaned)

    total_subtasks = sum(len(t.get("subtasks", [])) for t in cleaned)
    resumen = (
        ", ".join(f"{t['content']} [{t['status']}]" for t in cleaned) or "(lista vacia)"
    )
    subtasks_note = f", {total_subtasks} sub-tareas" if total_subtasks else ""
    return (
        f"Lista de tareas actualizada ({len(cleaned)} items{subtasks_note}): {resumen}"
    )


todowrite_tool = Tool(
    name="todowrite",
    description=(
        "Crea o actualiza la lista de tareas visible para el usuario. Usar SIEMPRE que el "
        "pedido tenga 2 o mas pasos reales -- ayuda tambien a organizar tu propio trabajo, "
        "no solo a informar al usuario. Cuando una tarea principal tiene partes identificables "
        "(ejercicios de una actividad, secciones de un dashboard, pasos de una instalacion), "
        "desglosalas como 'subtasks' de esa tarea en vez de crearlas como items sueltos al "
        "mismo nivel -- asi se ve la relacion 'pertenece a'. Maximo 2 niveles: una subtask NUNCA "
        "tiene sus propias subtasks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "subtasks": {
                            "type": "array",
                            "description": (
                                "Opcional. Sub-pasos de ESTA tarea principal (ej. cada "
                                "ejercicio de una actividad, cada seccion de un dashboard). "
                                "Mismo shape que un todo, pero SIN su propio campo 'subtasks' "
                                "(maximo 2 niveles)."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": sorted(VALID_STATUSES),
                                    },
                                    "priority": {
                                        "type": "string",
                                        "enum": ["high", "medium", "low"],
                                    },
                                },
                                "required": ["content", "status", "priority"],
                            },
                        },
                    },
                    "required": ["content", "status", "priority"],
                },
            },
        },
        "required": ["todos"],
    },
    execute=_todowrite_execute,
    # Toca session.push_event() (asyncio.Queue) -- no es thread-safe corrida
    # desde un worker thread, y de por si es sincronica pero rapida (sin
    # I/O de red/DB), asi que se queda en el event loop (ver Tool.thread_safe
    # en app/tools/base.py).
    thread_safe=False,
)
