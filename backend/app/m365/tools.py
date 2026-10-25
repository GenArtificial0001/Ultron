"""Herramientas `m365_*` que ve el modelo para Outlook y Teams (spec office-outlook-teams-mcp REQ-M05, REQ-M06, REQ-M07, REQ-M10, REQ-M12).

Por que existe: cada herramienta del conector de la empresa (EAKC `mcp-msgraph`) se le ofrece al modelo envuelta en una `Tool` de Vision con nombre `m365_<nombre>`. Envolverlas (en vez de pasarlas
en bruto) permite lo que el contrato pide: un nombre que no depende de como se llega a Outlook (REQ-020 de la spec original), filtrarlas todas con UN interruptor por prefijo (REQ-M09), dejar
que la politica de autorizacion decida ANTES de tocar la red (REQ-M07), y entregar lo que vuelve entre marcas y sin secretos (REQ-M06).

Los parametros y las descripciones salen de `catalog.json`, que viaja DENTRO de la app (copiado del catalogo real que devolvio el conector el 2026-10-17). No se leen de `specs/`: esa carpeta no
viaja a prod ni al .exe, y en la PC de cada persona las herramientas no tendrian parametros. De cada descripcion queda el primer parrafo (hasta 400 caracteres): completas pesarian ~10.100 tokens en
CADA vuelta del modelo; asi pesan ~4.900.

Orden de cada llamada: politica (puede bloquear sin tocar la red) -> conector -> marcar el turno si trajo texto ajeno -> auditoria si escribio -> contenido entre marcas. Una tool NUNCA lanza
(contrato de `ToolRegistry`): cualquier falla vuelve como la frase para la persona (`errors.describe`)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.m365 import client, content, errors, policy
from app.tools.base import Tool

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).with_name("catalog.json")
# Las herramientas que escriben y se auditan (el resto solo lee o guarda un borrador).
_AUDITED = frozenset(
    {
        "draft_message",
        "send_drafted_message",
        "cancel_meeting",
        "book_rctr_room",
        "confirm_booking",
        "create_draft_email",
        "send_email_to_self",
        "trust_senders",
    }
)

# De donde viene el texto que se le entrega al modelo (la marca lo dice: correo, chat, evento...).
_SOURCE = {
    "get_emails": "correo", "read_email": "correo", "list_mail_folders": "correo", "create_draft_email": "correo", "send_email_to_self": "correo", "mark_email_read": "correo",
    "read_chat_messages": "chat", "read_active_chats": "chat", "list_chats": "chat", "search_messages": "chat", "draft_message": "chat", "send_drafted_message": "chat",
    "get_meetings": "evento", "check_availability": "evento", "book_rctr_room": "evento", "confirm_booking": "evento", "cancel_meeting": "evento",
    "find_person": "persona", "my_recent_contacts": "persona", "my_profile": "persona",
    "find_meetings": "reunion", "resolve_meeting_link": "reunion", "list_meeting_transcripts": "reunion", "get_meeting_attendance": "reunion",
    "list_files": "archivo", "list_recent_files": "archivo", "list_shared_files": "archivo", "search_files": "archivo", "get_file_info": "archivo", "get_file_content": "archivo",
    "list_teams_channels": "canal", "read_channel_messages": "canal", "trust_senders": "m365",
    "read_transcript": "transcripcion", "search_transcript": "transcripcion", "search_transcripts": "transcripcion", "collect_meeting_transcript_evidence": "transcripcion",
}  # fmt: skip


# Lecturas que traen CONTENIDO (un archivo entero, los mensajes de un canal, el texto de los resultados de una busqueda) y pueden tardar mas de los 15 s de siempre: medido contra
# la empresa real, `read_channel_messages` con UN mensaje tardo 21 s. Con 15 s fallaria siempre con «tardaron demasiado». Las demas herramientas conservan el plazo corto.
SLOW_TIMEOUT_SECONDS = 60.0
_SLOW_READS = frozenset({"read_channel_messages", "get_file_content", "search_files"})


def _timeout_for(name: str) -> float:
    return SLOW_TIMEOUT_SECONDS if name in _SLOW_READS else client.DEFAULT_TIMEOUT_SECONDS


def load_catalog() -> dict[str, dict[str, Any]]:
    """Las herramientas ofrecidas, con descripcion y esquema. Un archivo ausente o roto da un catalogo VACIO (el registro de tools sigue andando)."""
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning(
            "Microsoft 365: no se pudo leer el catalogo de herramientas; no se ofrece ninguna", exc_info=True
        )
        return {}
    return data if isinstance(data, dict) else {}


def _make_execute(name: str):
    """`execute(args, session)` de UNA herramienta. Async: la llamada al conector es de red y no puede bloquear el servidor."""

    async def execute(args: dict[str, Any], session: Any = None) -> str:
        full = f"{policy.PREFIX}{name}"
        try:
            blocked = policy.check(full, args, session)
            if blocked is not None:
                return blocked  # la persona tiene que autorizar primero: no se toca la red
            result = await client.call_tool(name, args, timeout=_timeout_for(name))
        except errors.M365Error as exc:
            return exc.friendly.message
        except Exception as exc:  # noqa: BLE001 -- una tool nunca lanza: lo que sea se traduce a la frase para la persona
            friendly = errors.describe(exc)
            logger.warning("Microsoft 365: fallo la herramienta %s (%s)", full, errors.technical_detail(exc))
            return friendly.message

        policy.note_read(session, full)
        if name in _AUDITED and not result.is_error:
            policy.audit(full, args, "ejecutada")
        text = content.wrap(result.text, source=_SOURCE.get(name, "m365"))
        return f"Error de la herramienta: {text}" if result.is_error else text

    return execute


def build_tools() -> list[Tool]:
    """Las `Tool` de Outlook y Teams, listas para registrar. Vacio si el catalogo no se pudo leer."""
    tools: list[Tool] = []
    for name, spec in sorted(load_catalog().items()):
        tools.append(
            Tool(
                name=f"{policy.PREFIX}{name}",
                description=str(spec.get("description") or name),
                parameters=spec.get("parameters") or {"type": "object", "properties": {}},
                execute=_make_execute(name),
            )
        )
    return tools


M365_TOOLS: list[Tool] = build_tools()
