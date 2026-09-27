"""
Export de una sesion a Markdown (spec persistencia-sqlite-sesiones,
REQ-008) -- puerto LITERAL del algoritmo real de OpenCode en
`packages/tui/src/util/transcript.ts` (`formatTranscript`/`formatMessage`/
`formatAssistantHeader`/`formatPart`), decision explicita del usuario de
reusar ese diseno en vez de inventar un formato propio.

No se usa desde ningun comando de chat todavia -- eso pertenece a la spec
de comandos slash ("/export"), que reusara `export_session_to_markdown`
tal cual queda aca. Por ahora se invoca desde: la auto-purga a 30 dias
(REQ-007) y, potencialmente, pruebas manuales de validacion."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.persistence import db


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m/%d/%Y, %I:%M:%S %p")  # noqa: DTZ006 -- hora local de la maquina, no UTC


def format_part(part_type: str, data: dict[str, Any]) -> str:
    """Puerto literal de `formatPart` (transcript.ts:86-114)."""
    if part_type == "text":
        if data.get("synthetic"):
            return ""
        return f"{data.get('text', '')}\n\n"

    if part_type == "reasoning":
        # `options.thinking` de OpenCode no tiene equivalente en la UI de
        # Vision hoy -- se omite el reasoning del export, igual que el
        # default (`options.thinking = False`) de OpenCode.
        return ""

    if part_type == "image":
        # spec contexto-multimodal REQ-006 (reglas de negocio): un
        # placeholder de texto en vez de embeber el base64 completo en el
        # .md -- mantiene el archivo exportado legible y de tamano
        # razonable (decision explicita del usuario en la spec).
        return "*[imagen adjunta]*\n\n"

    if part_type == "tool":
        name = data.get("name", "?")
        result = f"**Tool: {name}**\n"
        arguments = data.get("arguments")
        if arguments:
            try:
                parsed_args = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
            except (json.JSONDecodeError, TypeError):
                parsed_args = arguments
            result += f"\n**Input:**\n```json\n{json.dumps(parsed_args, indent=2, ensure_ascii=False)}\n```\n"
        if "result" in data:
            result += f"\n**Output:**\n```\n{data['result']}\n```\n"
        elif data.get("error"):
            result += f"\n**Error:**\n```\n{data['error']}\n```\n"
        result += "\n"
        return result

    return ""


def format_assistant_header(model: str | None, duration_s: float | None) -> str:
    """Puerto de `formatAssistantHeader` (transcript.ts:69-84) -- Vision no
    tiene el concepto de "Agent" (subagentes) de OpenCode, asi que se
    omite esa parte y se deja Modelo + duracion."""
    parts = ["Assistant"]
    if model:
        parts.append(model)
    header = " · ".join(parts)
    if duration_s is not None:
        header += f" · {duration_s:.1f}s"
    return f"## {header}\n\n"


def format_message(
    role: str, parts: list[tuple[str, dict[str, Any]]], model: str | None = None
) -> str:
    """Puerto de `formatMessage` (transcript.ts:48-67)."""
    if role == "user":
        result = "## User\n\n"
    elif role == "assistant":
        duration = None
        result = format_assistant_header(model, duration)
    else:
        # 'tool'/'system' no tienen encabezado propio en el transcript de
        # OpenCode (los resultados de tool se renderizan como parte del
        # mensaje 'assistant' que los origino) -- se omiten aca tambien.
        return ""

    for part_type, data in parts:
        result += format_part(part_type, data)
    return result


def format_transcript(
    title: str,
    session_id: str,
    created_at: float,
    updated_at: float,
    messages: list[dict[str, Any]],
) -> str:
    """Puerto de `formatTranscript` (transcript.ts:26-46).

    `messages`: lista de {role, model, parts: [(type, data), ...]} ya
    ordenada cronologicamente (ver build_export_messages)."""
    transcript = f"# {title}\n\n"
    transcript += f"**Session ID:** {session_id}\n"
    transcript += f"**Created:** {_fmt_dt(created_at)}\n"
    transcript += f"**Updated:** {_fmt_dt(updated_at)}\n\n"
    transcript += "---\n\n"

    for msg in messages:
        transcript += format_message(msg["role"], msg["parts"], msg.get("model"))
        transcript += "---\n\n"

    return transcript


def build_export_messages(db_path: Path, session_id: str) -> list[dict[str, Any]]:
    """Arma la estructura intermedia {role, parts} directamente desde las
    tablas normalizadas (sin pasar por el formato OpenAI de
    load_messages, que colapsa tool-calls/resultados en mensajes 'tool'
    separados -- para el export interesa el 'part' crudo tal cual quedo
    guardado)."""
    import sqlite3

    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, role FROM message WHERE session_id = ? ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()
        result = []
        for msg_id, role in rows:
            part_rows = conn.execute(
                "SELECT type, data FROM part WHERE message_id = ? ORDER BY seq ASC",
                (msg_id,),
            ).fetchall()
            parts = [(ptype, json.loads(data)) for ptype, data in part_rows]
            result.append({"role": role, "parts": parts})
        return result
    finally:
        conn.close()


def export_session_to_markdown(session_id: str) -> str | None:
    """REQ-008: genera el contenido Markdown completo de una sesion. Usa
    la fachada de app/persistence/service.py para resolver la carpeta
    PROPIA de esta sesion (arquitectura carpeta-por-sesion, definitiva) --
    devuelve None si esa sesion no tiene carpeta asignada o no existe en
    disco.

    Bug real corregido: esta funcion llamaba a `get_active_db_path()` SIN
    pasarle `session_id` -- con la carpeta global vieja funcionaba porque
    habia un fallback comun a todas las sesiones, pero en la arquitectura
    carpeta-por-sesion esa llamada sin argumento siempre devuelve None,
    rompiendo /export para toda sesion sin excepcion."""
    from app.persistence.service import (
        get_active_db_path,
    )  # import diferido: evita ciclo

    db_path = get_active_db_path(session_id)
    if db_path is None:
        return None

    meta = db.get_session_meta(db_path, session_id)
    if meta is None:
        return None

    messages = build_export_messages(db_path, session_id)
    return format_transcript(
        title=meta["title"],
        session_id=session_id,
        created_at=meta["created_at"],
        updated_at=meta["updated_at"],
        messages=messages,
    )


def _filename_title_component(title: str) -> str | None:
    """Nombre de sesion listo para usar como prefijo de archivo, o `None`
    si la sesion NUNCA tuvo un nombre propio (ver caller: en ese caso se
    omite el prefijo por completo, para no colisionar con el propio
    timestamp de `session-<fecha-hora>.md` -- decision explicita del
    usuario tras notar que anteponer literal "Nueva sesión" chocaria con
    el prefijo `DEFAULT_TITLE_PREFIX` que YA incluye su propio timestamp
    distinto (`db.py`), quedando redundante/confuso).

    Limpieza de caracteres: mismo criterio ya usado en
    export_session_command_endpoint de main.py (alfanumerico/espacio/
    guion/guion-bajo, resto reemplazado por '_'), recortado a 60
    caracteres para dejar margen al resto de la nomenclatura fija."""
    if title.startswith(db.DEFAULT_TITLE_PREFIX):
        return None
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()
    return safe[:60] or None


def export_session_to_file(session_id: str) -> Path | None:
    """REQ-007/008 (spec persistencia-sqlite-sesiones) + bug real
    reportado por el usuario (2026-09-08): escribe el .md DIRECTO a
    `<carpeta-de-la-sesion>/sessions/` -- CADA export crea un archivo
    NUEVO, nunca sobrescribe uno viejo aunque se exporte la misma sesion
    dos veces (pedido explicito). Devuelve la ruta escrita (None si esa
    sesion no tiene carpeta asignada).

    Nomenclatura (pedido explicito del usuario, corregida 2026-09-08):
    `<Nombre de la sesión>_session-<fecha-hora>.md` si la sesion TIENE un
    nombre propio (manual o auto-generado por "primeras palabras del
    primer mensaje") -- antes el nombre de archivo NUNCA incluia el
    titulo, quedaba `session-<fecha-hora>.md` a secas, sin forma de
    distinguir a que sesion pertenecia un archivo viejo con solo mirar la
    carpeta. Si la sesion sigue con su titulo placeholder (nunca se le
    puso nombre), se usa solo `session-<fecha-hora>.md` SIN prefijo --
    decision explicita del usuario tras notar que anteponer literal
    "Nueva sesión" colisionaria/seria redundante con el propio
    `DEFAULT_TITLE_PREFIX` de `db.py`, que ya incluye su propio timestamp
    de creacion distinto al de este archivo."""
    from app.persistence.service import (
        SESSIONS_SUBDIR,
        get_active_db_path,
        project_root_for_session,
    )  # import diferido: evita ciclo

    project_root = project_root_for_session(session_id)
    if project_root is None:
        return None

    db_path = get_active_db_path(session_id)
    if db_path is None:
        return None
    meta = db.get_session_meta(db_path, session_id)
    if meta is None:
        return None

    messages = build_export_messages(db_path, session_id)
    content = format_transcript(
        title=meta["title"],
        session_id=session_id,
        created_at=meta["created_at"],
        updated_at=meta["updated_at"],
        messages=messages,
    )

    title_prefix = _filename_title_component(meta["title"])
    sessions_dir = project_root / SESSIONS_SUBDIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")  # noqa: DTZ005 -- hora local de la maquina, no UTC
    filename = (
        f"{title_prefix}_session-{timestamp}.md"
        if title_prefix
        else f"session-{timestamp}.md"
    )
    out_path = sessions_dir / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path
