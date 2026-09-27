"""
Diagnostico ESTRUCTURAL del historial que se le manda al modelo.

Por que existe (caso real 2026-09-24, sesion 8619176d, gemini-3.1-pro): el proveedor de IA
respondio 400 "the number of function response parts is equal to the number
of function call parts" y la sesion quedo trabada. El log solo guardaba el
texto del error, no COMO estaba armado el historial en ese momento, y el
historial que lo provoco vivia solo en memoria: al reconstruirlo desde SQLite
Gemini lo aceptaba (200), asi que el disparador exacto no se pudo confirmar.

`describe_history_shape` resume la forma del historial SIN contenido (nunca
texto del usuario, argumentos ni resultados de tools): una secuencia de
roles con la cantidad de tool-calls por mensaje, mas la lista de anomalias
que los proveedores rechazan -- respuestas de tools que no coinciden con las
llamadas, ids duplicados o invalidos, mensajes `system` a mitad de la
conversacion. El loop lo registra solo cuando el modelo responde 400.
"""

from __future__ import annotations

import re
from typing import Any

# Mismo criterio que `_VALID_TOOL_CALL_ID_RE` de app/persistence/db.py (Claude
# rechaza ids de tool_use fuera de este alfabeto; los de Gemini traen la firma
# de pensamiento con `+`, `/` o `=`).
_VALID_TOOL_CALL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_ANOMALIES = 20


def _short_id(call_id: str) -> str:
    """Los ids de Gemini llegan a miles de caracteres (firma de pensamiento):
    nunca se registran completos."""
    if len(call_id) <= 24:
        return call_id
    return f"{call_id[:24]}…({len(call_id)} caracteres)"


def _has_image(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "image_url" for block in content
    )


def _compress(tokens: list[str]) -> str:
    """"T T T" -> "T×3": un historial largo entra en una sola linea de log."""
    out: list[str] = []
    for token in tokens:
        if out and out[-1].split("×")[0] == token:
            base, _, count = out[-1].partition("×")
            out[-1] = f"{base}×{int(count or 1) + 1}"
        else:
            out.append(token)
    return " ".join(out)


def describe_history_shape(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Devuelve (secuencia, anomalias).

    Secuencia: S=system, U=user (U+img con imagenes), A=assistant sin tools,
    A3=assistant que pidio 3 tools, T=respuesta de tool. Anomalias: frases
    cortas con el indice del mensaje, listas para el log."""
    tokens: list[str] = []
    anomalies: list[str] = []
    first_seen: dict[str, int] = {}
    pending: dict[str, int] = {}  # tool_call_id -> indice del assistant que lo pidio

    def flag(text: str) -> None:
        if len(anomalies) < _MAX_ANOMALIES:
            anomalies.append(text)

    def close_pending(at: int, who: str) -> None:
        if pending:
            ids = ", ".join(_short_id(cid) for cid in list(pending)[:3])
            flag(f"[{at}] {who} llega con {len(pending)} tool-call(s) sin respuesta: {ids}")
            pending.clear()

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            tokens.append("S")
            if index > 0:
                flag(f"[{index}] mensaje system a mitad de la conversacion")
        elif role == "user":
            close_pending(index, "user")
            tokens.append("U+img" if _has_image(message.get("content")) else "U")
        elif role == "assistant":
            close_pending(index, "assistant")
            calls = message.get("tool_calls") or []
            tokens.append(f"A{len(calls)}" if calls else "A")
            for call in calls:
                call_id = str((call or {}).get("id") or "")
                if not call_id:
                    flag(f"[{index}] tool-call sin id")
                    continue
                if not _VALID_TOOL_CALL_ID_RE.match(call_id):
                    flag(f"[{index}] id con caracteres no aceptados por todos los modelos: {_short_id(call_id)}")
                if call_id in first_seen:
                    flag(f"[{index}] id repetido (ya usado en [{first_seen[call_id]}]): {_short_id(call_id)}")
                else:
                    first_seen[call_id] = index
                pending[call_id] = index
        elif role == "tool":
            tokens.append("T")
            call_id = str(message.get("tool_call_id") or "")
            if call_id in pending:
                del pending[call_id]
            else:
                flag(f"[{index}] respuesta de tool sin tool-call pendiente: {_short_id(call_id) or '(sin id)'}")
        else:
            tokens.append("?")
            flag(f"[{index}] rol desconocido: {role!r}")
    close_pending(len(messages), "fin del historial")
    return _compress(tokens), anomalies
