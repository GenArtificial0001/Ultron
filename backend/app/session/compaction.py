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
    from app.llm.client import call_with_token_retry  # import diferido: evita ciclo

    def _request() -> Any:
        # transcripcion-reintenta-token REQ-008: un 401 pide token nuevo y reintenta una vez (antes el resumen del
        # contexto fallaba y la conversacion seguia sin compactar hasta que se renovara el token a mano).
        return call_with_token_retry(
            lambda client: client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _SUMMARY_PROMPT.format(transcript=transcript)}],
                stream=False,
            )
        )

    response = await asyncio.to_thread(_request)
    return response.choices[0].message.content or "(resumen no disponible)"


async def compact_session(
    session_id: str,
    messages: list[dict[str, Any]],
    context_window: int,
    model: str,
) -> bool:
    """REQ-001/004: resume turnos viejos y muta messages in-place.

    REQ-014 c (spec fix-detener-y-progreso-visible, revision): mientras se espera el resumen (una llamada al modelo: 18 s medidos)
    el usuario puede borrar o editar historial viejo, porque el turno que compacta sigue corriendo. Antes la lista nueva se armaba
    con lo que habia ANTES de esperar y despues reemplazaba a la actual: lo borrado volvia a la memoria (y el modelo lo seguia
    viendo aunque SQLite ya no lo tuviera). Ahora el resumen se aplica sobre la lista ACTUAL, reconociendo cada mensaje por
    identidad (no por igualdad: dos mensajes con el mismo texto son distintos): reemplaza a los turnos viejos y conserva todo lo
    demas tal como esta ahora. Si se borro algo de lo que se resumio, el resumen ya no sirve (hablaria de lo borrado) y no se
    aplica: el turno sigue sin compactar y el proximo vuelve a intentarlo."""
    rest = [m for m in messages if m.get("role") != "system"]
    old_turns, recent_turns = _select_recent_turns(_split_into_turns(rest), context_window)
    if not old_turns:
        return False

    summary = await _summarize(_turns_to_transcript(old_turns), model)
    # `old_messages` mantiene vivos esos dicts: sus `id()` no se reutilizan mientras dure la comparacion.
    old_messages = [m for turn in old_turns for m in turn]
    current_ids = {id(m) for m in messages}
    if any(id(m) not in current_ids for m in old_messages):
        logger.info(
            "Sesion '%s': el historial cambio mientras se resumia (se borro parte de lo resumido); no se compacta",
            session_id,
        )
        return False
    old_ids = {id(m) for m in old_messages}
    summary_message = {
        "role": "user",
        "content": (f"[Resumen automatico de {len(old_turns)} turno(s) anteriores]\n\n{summary}"),
    }
    system_messages = [m for m in messages if m.get("role") == "system"]
    kept = [m for m in messages if m.get("role") != "system" and id(m) not in old_ids]
    messages.clear()
    messages.extend(system_messages + [summary_message] + kept)
    logger.info(
        "Sesion '%s' compactada: %d turnos resumidos, %d recientes preservados",
        session_id,
        len(old_turns),
        len(recent_turns),
    )
    return True


def _pct_of(messages: list[dict[str, Any]], context_window: int) -> float:
    return estimate_tokens(messages) / context_window * 100 if context_window else 0


def prepare_compaction(messages: list[dict[str, Any]], context_window: int) -> dict[str, Any]:
    """spec aviso-compactacion-automatica REQ-001/002: PODA los resultados viejos de herramientas si la conversacion llego al umbral y dice que
    paso, SIN llamar al modelo. Devuelve `{action, before_pct, after_pct}` con `action` = `none` (no llego al umbral: no se toco nada),
    `pruned` (la poda alcanzo y NO hace falta resumir) o `needs_summary` (sigue arriba del umbral: hay que resumir con el modelo).

    Caso real 2026-10-24: una sesion al 100 % quedo en 43 % sin aviso porque solo se podo y `maybe_compact` devolvia `False`. Esta funcion
    existe para que el turno pueda AVISAR antes de la espera del resumen, sin cambiar la firma de `maybe_compact` (los tests y quien lo use
    directo siguen igual). Es idempotente: la poda salta lo ya podado, asi que `maybe_compact` despues no hace nada de mas."""
    before = _pct_of(messages, context_window)
    if before < AUTO_COMPACT_THRESHOLD_PCT:
        return {"action": "none", "before_pct": round(before), "after_pct": round(before)}
    _prune_tool_outputs(messages)
    after = _pct_of(messages, context_window)
    action = "pruned" if after < AUTO_COMPACT_THRESHOLD_PCT else "needs_summary"
    return {"action": action, "before_pct": round(before), "after_pct": round(after)}


async def maybe_compact(
    session_id: str,
    messages: list[dict[str, Any]],
    context_window: int,
    model: str,
) -> bool:
    """REQ-001: poda y resume automaticamente al alcanzar el 85%. Devuelve True solo si se RESUMIO (contrato de siempre; para saber si
    ademas se podo, usar `prepare_compaction` antes). Una falla del resumen se propaga, como siempre."""
    pct = _pct_of(messages, context_window)
    if pct < AUTO_COMPACT_THRESHOLD_PCT:
        return False
    _prune_tool_outputs(messages)
    if _pct_of(messages, context_window) < AUTO_COMPACT_THRESHOLD_PCT:
        return False
    return await compact_session(session_id, messages, context_window, model)
