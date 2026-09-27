"""
Historial compatible con el modelo destino (spec `salto-automatico-modelos`, REQ-005).

Gemini (via el proveedor de IA) devuelve ids de tool-call con la "firma de pensamiento"
pegada (`call_1707955200_0__thought__<firma>`: miles de caracteres con `+ / =`)
y REPITE ids entre turnos (`call_1707955200_1` en varios turnos, caso real
2026-09-24). Claude rechaza ids fuera de `^[a-zA-Z0-9_-]+$` (bug real
2026-09-10) y OpenAI los de mas de 40 caracteres. Hasta ahora eso solo se
limpiaba al RECARGAR la sesion desde disco (`db.load_messages` descarta esos
tool-calls); saltar de modelo en caliente -- o cambiarlo a mano en el
selector -- mandaba el historial crudo y el modelo nuevo lo rechazaba (400).

Ademas (medido contra el gateway real el 2026-09-25): el proveedor de IA responde 500
("'content'") si a Claude le llega un mensaje `assistant` con tool-calls y
`content` en None o ausente -- el formato que dejan GPT o Gemini cuando llaman
herramientas sin escribir texto. Con `content: ""` lo acepta. Saltar hacia Claude
desde otro modelo chocaba justo con eso.

`history_for_model` devuelve, para cualquier modelo que no sea Gemini, una COPIA
con esos ids reemplazados por uno estable (`call_<hash>`, el mismo en la llamada
y en su respuesta; los repetidos, con sufijo) y `content: ""` donde faltaba.
Nunca toca `session.messages`: si despues se vuelve a Gemini, recibe sus ids
originales con la firma.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.llm.model_fallback import is_gemini

# Lo que aceptan todos los proveedores a la vez (Claude: alfabeto;
# OpenAI: hasta 40 caracteres).
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")


def _safe_id(call_id: str) -> str:
    if _SAFE_ID_RE.match(call_id):
        return call_id
    return "call_" + hashlib.sha1(call_id.encode("utf-8")).hexdigest()[:24]


def _unique(candidate: str, seen: set[str]) -> str:
    new, n = candidate, 1
    while new in seen:
        n += 1
        new = f"{candidate[:34]}_{n}"
    seen.add(new)
    return new


def history_for_model(messages: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """Historial a enviar a `model` (id real, ya resuelto si era un alias).
    Devuelve la MISMA lista si no hacia falta cambiar nada."""
    if is_gemini(model):
        return messages
    result: list[dict[str, Any]] = []
    changed = False
    seen: set[str] = set()
    mapping: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            # Ids de ESTE mensaje: sus respuestas (role=tool) vienen a continuacion.
            mapping = {}
            calls = []
            for call in message["tool_calls"]:
                old = str((call or {}).get("id") or "")
                new = _unique(_safe_id(old), seen)
                mapping[old] = new
                calls.append(call if new == old else {**call, "id": new})
            if any(new is not old for new, old in zip(calls, message["tool_calls"])):
                message = {**message, "tool_calls": calls}
                changed = True
            if message.get("content") is None:
                message = {**message, "content": ""}
                changed = True
        elif role == "tool":
            old = str(message.get("tool_call_id") or "")
            new = mapping.get(old, _safe_id(old))
            if new != old:
                message = {**message, "tool_call_id": new}
                changed = True
        result.append(message)
    return result if changed else messages
