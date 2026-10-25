"""Validacion de los horarios (`trigger`) de agentes y alarmas al CREAR o EDITAR (PQ-004 / BL-019, ADR-018).

Hasta ahora `trigger` era un dict libre: un "25:99" se guardaba y el reloj (`scheduler/agents.py`, `alarms.py`) lo leia en
silencio como 08:30, sin que el usuario se enterara. Esta validacion mira exactamente lo que el reloj entiende y lo que la
pantalla de agentes puede mandar, y devuelve un mensaje para PERSONAS (o None si esta bien).

Es una funcion pura, sin I/O: la API la usa para responder 422 y no se guarda nada si falla. NO se aplica a lo que ya estaba
guardado: el reloj sigue leyendo defensivamente ese caso (BL-007) y un agente viejo con horario malo se puede apagar o
renombrar igual (la API solo valida cuando el pedido toca `kind` o `trigger`).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Las mismas reglas que ofrece el <select id="agent-rule"> de la pantalla y que entiende el reloj.
RULES = ("daily", "weekdays", "weekly", "ndays", "nhours")
KINDS = ("once", "recurring")

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def _bad_time(value: Any) -> str | None:
    """None si `value` es una hora hh:mm valida (00:00 a 23:59)."""
    text = str(value).strip()
    m = _HHMM.fullmatch(text)
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        return None
    return f"La hora «{text}» no es válida. Usá el formato hh:mm, entre 00:00 y 23:59 (por ejemplo 08:30)."


def _bad_weekday(value: Any) -> str | None:
    """0=domingo..6=sabado, como el <select> de la pantalla. Se acepta un entero o un texto que sea un entero."""
    try:
        dow = int(value)
    except (TypeError, ValueError):
        dow = None
    if dow is not None and not isinstance(value, bool) and 0 <= dow <= 6:
        return None
    return f"El día de la semana «{value}» no es válido. Tiene que ser un número de 0 (domingo) a 6 (sábado)."


def _bad_count(value: Any, unit: str) -> str | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = None
    if n is not None and not isinstance(value, bool) and n >= 1:
        return None
    return f"La cantidad de {unit} «{value}» no es válida. Tiene que ser un número entero de 1 en adelante."


def validate_trigger(kind: str, trigger: dict[str, Any] | None) -> str | None:
    """Mensaje de error para una persona si `(kind, trigger)` no es un horario valido; None si esta bien."""
    if kind not in KINDS:
        return f"El tipo de repetición «{kind}» no es válido. Elegí «una vez» o «recurrente»."
    trigger = trigger or {}

    if kind == "once":
        iso = str(trigger.get("target_iso") or "").strip()
        if not iso:
            return "Falta la fecha y hora de la ejecución única."
        try:
            datetime.fromisoformat(iso)
        except ValueError:
            return f"La fecha y hora «{iso}» no es válida. Usá el formato AAAA-MM-DDThh:mm (por ejemplo 2030-01-31T08:30)."
        return None

    rule = trigger.get("rule")
    if rule not in RULES:
        return "Falta la regla de repetición o no es válida. Elegí Diario, Días hábiles, Un día de la semana, Cada N días o Cada N horas."
    # La hora es opcional (el reloj usa 08:30), pero si viene tiene que ser valida. En «cada N días/horas» el reloj no la usa para
    # contar, asi que solo se exige en las reglas que si la usan.
    if "time" in trigger and trigger["time"] not in (None, ""):
        if (msg := _bad_time(trigger["time"])) is not None:
            return msg
    if rule == "weekly" and "weekday" in trigger and trigger["weekday"] not in (None, ""):
        if (msg := _bad_weekday(trigger["weekday"])) is not None:
            return msg
    if rule in ("ndays", "nhours") and "n" in trigger and trigger["n"] not in (None, ""):
        return _bad_count(trigger["n"], "días" if rule == "ndays" else "horas")
    return None
