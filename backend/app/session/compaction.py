"""
Pruning y compactacion de contexto de conversacion.

La spec gestion-contexto-conversacion adopta el diseno de OpenCode:
primero reduce outputs viejos de tools sin romper su estructura y, si el
historial aun supera el umbral, resume turnos viejos preservando completos
los mas recientes y el system prompt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.token_estimate import estimate_tokens

logger = logging.getLogger(__name__)

AUTO_COMPACT_THRESHOLD_PCT = 85
_RECENT_BUDGET_PCT = 0.25
_RECENT_BUDGET_MIN = 2_000
_RECENT_BUDGET_MAX = 15_000
_PRUNE_PROTECT_TOKENS = 20_000
_PRUNED_MARKER = "[contenido podado por gestion de contexto]"


def _recent_budget_tokens(context_window: int) -> int:
    raw = int(context_window * _RECENT_BUDGET_PCT)
    return max(_RECENT_BUDGET_MIN, min(_RECENT_BUDGET_MAX, raw))


def _split_into_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def _prune_tool_outputs(messages: list[dict[str, Any]]) -> bool:
    """REQ-001/003: poda contenido viejo conservando tool_call_id."""
    protected_tokens = 0
    pruned_any = False
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if content == _PRUNED_MARKER:
            continue
        message_tokens = len(str(content)) // 4
        if protected_tokens < _PRUNE_PROTECT_TOKENS:
            protected_tokens += message_tokens
            continue
        message["content"] = _PRUNED_MARKER
        pruned_any = True
    return pruned_any


def _select_recent_turns(
    turns: list[list[dict[str, Any]]], context_window: int
) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    """REQ-004: separa turnos viejos de recientes por presupuesto."""
    budget = _recent_budget_tokens(context_window)
    recent: list[list[dict[str, Any]]] = []
    used = 0
    index = len(turns)
    for turn in reversed(turns):
        turn_tokens = estimate_tokens(turn)
        if recent and used + turn_tokens > budget:
            break
        recent.insert(0, turn)
        used += turn_tokens
        index -= 1
    return turns[:index], recent


def _turns_to_transcript(turns: list[list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    for turn in turns:
        for message in turn:
            role = message.get("role")
            content = message.get("content")
            if role == "user":
                text = content if isinstance(content, str) else "(mensaje con imagenes)"
                lines.append(f"Usuario: {text}")
            elif role == "assistant" and content:
                lines.append(f"Vision: {content}")
            elif role == "tool":
                lines.append(f"[resultado de herramienta] {str(content or '')[:200]}")
    return "\n".join(lines)


_SUMMARY_PROMPT = """Resumi la siguiente porcion de una conversacion tecnica. Preserva decisiones,
archivos y rutas, resultados relevantes de herramientas e instrucciones explicitas del usuario.
Se conciso (maximo 300 palabras) y escribi prosa corrida.

Conversacion a resumir:
{transcript}"""


async def _summarize(transcript: str, model: str) -> str:
    from app.llm.client import get_client  # import diferido: evita ciclo
    from app.llm.providers import api_model

    # spec varios-proveedores: el cliente y el nombre del proveedor de `model`.
    client = get_client(model=model)

    def _request() -> Any:
        return client.chat.completions.create(
            model=api_model(model),
            messages=[
                {"role": "user", "content": _SUMMARY_PROMPT.format(transcript=transcript)}
            ],
            stream=False,
        )

    response = await asyncio.to_thread(_request)
    return response.choices[0].message.content or "(resumen no disponible)"


async def compact_session(
    session_id: str,
    messages: list[dict[str, Any]],
    context_window: int,
    model: str,
) -> bool:
    """REQ-001/004: resume turnos viejos y muta messages in-place."""
    system_messages = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    old_turns, recent_turns = _select_recent_turns(
        _split_into_turns(rest), context_window
    )
    if not old_turns:
        return False

    summary = await _summarize(_turns_to_transcript(old_turns), model)
    summary_message = {
        "role": "user",
        "content": (
            f"[Resumen automatico de {len(old_turns)} turno(s) anteriores]\n\n{summary}"
        ),
    }
    new_messages = system_messages + [summary_message]
    for turn in recent_turns:
        new_messages.extend(turn)
    messages.clear()
    messages.extend(new_messages)
    logger.info(
        "Sesion '%s' compactada: %d turnos resumidos, %d recientes preservados",
        session_id,
        len(old_turns),
        len(recent_turns),
    )
    return True


async def maybe_compact(
    session_id: str,
    messages: list[dict[str, Any]],
    context_window: int,
    model: str,
) -> bool:
    """REQ-001: poda y resume automaticamente al alcanzar el 85%."""
    pct = estimate_tokens(messages) / context_window * 100 if context_window else 0
    if pct < AUTO_COMPACT_THRESHOLD_PCT:
        return False
    _prune_tool_outputs(messages)
    pct_after = estimate_tokens(messages) / context_window * 100 if context_window else 0
    if pct_after < AUTO_COMPACT_THRESHOLD_PCT:
        return False
    return await compact_session(session_id, messages, context_window, model)
