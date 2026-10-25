"""
Reloj de alarmas/recordatorios (tabla `schedules`, `/api/alarms`), migrado
a BACKEND (2026-09, pedido del usuario: "las alarmas no estan alertando
nada"). Diagnostico real: el motor de disparo vivia SOLO en el frontend
(`scheduleTick` en frontend/js/app.js, `setInterval` de 1s) -- necesitaba
la pestaña de Vision abierta en el navegador en el momento exacto del
horario. Los datos de `agent_runs`/`schedules` mostraban alarmas
"habilitadas" que llevaban dias sin dispararse porque la pestaña no
quedaba abierta.

Mismo esqueleto que `app/scheduler/agents.py` (mismo `AGENT_POLL_INTERVAL_
SECONDS`-like tick, mismo `is_due_now()` -- se REUSA de agents.py: `schedules`
y `agents` comparten identico vocabulario de trigger_json, ver docstring
de esa funcion -- y el mismo bug de "next_due() nunca dispara nada
recurrente" que is_due_now() corrige, encontrado 2026-09-17 probando ZCC en
vivo). La UNICA diferencia real es que un `schedule` no lanza un
turno del LLM: hace sonar un WAV nativo (`winsound`, stdlib) y muestra un
toast nativo de Windows (`win11toast`) -- ASI SI se nota con el navegador
cerrado. El CRUD (crear/editar/pausar/borrar, panel Configuracion > Agentes)
NO cambia: sigue siendo `/api/alarms` sobre la misma tabla `schedules` de
`<carpeta de datos>/vision.db` (ver master_index.py) -- este modulo solo agrega el
disparo, no toca la persistencia ni la UI.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.persistence import master_index as mi
from app.scheduler.agents import get_snooze_minutes, is_due_now, wav_path

logger = logging.getLogger(__name__)

# Igual que agents.py: 20s alcanza para "a las 8:30" con tolerancia minima
# sin martillar la DB. La pestaña (si esta abierta) sigue mostrando su
# propia lista via /api/alarms sin relacion con este tick.
ALARM_POLL_INTERVAL_SECONDS = 20


def fire_alarm(schedule: dict[str, Any]) -> None:
    """Dispara ESTA alarma de forma nativa (sin depender de ningun
    navegador abierto): alerta visual persistente + sonido en loop (ver
    app/notifier.py, pedido del usuario 2026-09-17: "que suene hasta que
    presione el boton X") + un toast silencioso de Windows (solo para que
    quede historial en el Centro de actividades), y si tiene `file_path`
    asociado lo ejecuta -- mismo `os.startfile` auditado que ya usaba
    `/api/alarms/{id}/run`. Nunca deja tirar una excepcion hacia el loop:
    cada paso esta aislado en su propio try/except, para que un toast roto
    (p.ej. sin AppUserModelID en el EXE empaquetado) no tape la alerta ni
    el archivo."""
    from win11toast import (
        notify,  # import diferido: costoso (WinRT) y solo hace falta aca
    )

    from app import notifier
    from app.tools import base as tool_base

    label = schedule.get("label") or "(sin etiqueta)"
    sound = schedule.get("sound") or "beep"

    notifier.show_alert("alarm", schedule["id"], label, wav_path(sound), get_snooze_minutes())

    try:
        # audio silenciado: el WAV de la alerta YA es el sonido elegido por
        # el usuario -- sin esto Windows superpone su propio "ding" generico.
        notify(title=f"⏰ {label}", body="ULTRON — alarma programada", audio={"silent": "true"})
    except Exception:
        logger.warning("No se pudo mostrar el toast de la alarma '%s'", label, exc_info=True)

    path = (schedule.get("file_path") or "").strip()
    if path:
        try:
            import os

            os.startfile(path)
            tool_base._audit("run_file", {"path": path, "status": "ok", "source": f"alarm:{schedule['id']}"})
        except OSError as exc:
            tool_base._audit(
                "run_file",
                {"path": path, "status": "error", "error": str(exc), "source": f"alarm:{schedule['id']}"},
            )


def _check_and_fire_due_alarms() -> None:
    from app import notifier

    now = time.time()
    schedules_by_id = {s["id"]: s for s in mi.list_schedules()}

    # "Zzz" (posponer, pedido del usuario 2026-09-17): re-dispara SIN pasar
    # por is_due_now -- ver mismo criterio en agents.py.
    for schedule_id in notifier.pop_due_snoozes("alarm", now):
        schedule = schedules_by_id.get(schedule_id)
        if schedule is None or not schedule["enabled"]:
            continue
        try:
            fire_alarm(schedule)
        except Exception:
            logger.warning(
                "Fallo al re-disparar (Zzz) la alarma '%s' (%s)", schedule["label"], schedule["id"], exc_info=True
            )
        finally:
            mi.update_schedule_last_fired(schedule["id"], now)

    for schedule in schedules_by_id.values():
        if schedule["kind"] == "pomodoro":
            continue  # el pomodoro es un timer de UI en vivo, no una alarma puntual
        if not schedule["enabled"]:
            continue
        # is_due_now (no next_due) a proposito: ver docstring de
        # _most_recent_due en agents.py, bug real 2026-09-17 (ZCC no sono a
        # las 8:30 ni quedo en el historial) -- next_due() da SIEMPRE un
        # vencimiento futuro por construccion, entonces `due <= now` nunca
        # podia ser verdadero para una alarma recurrente.
        if not is_due_now(schedule, now):
            continue
        try:
            fire_alarm(schedule)
        except Exception:
            logger.warning(
                "Fallo al disparar la alarma '%s' (%s)", schedule["label"], schedule["id"], exc_info=True
            )
        finally:
            mi.update_schedule_last_fired(schedule["id"], now)


async def run_alarms_loop() -> None:
    """Tarea de background de larga duracion, misma forma que
    `agents.run_agents_loop()` -- se lanza una sola vez desde el startup
    de FastAPI (ver app/main.py)."""
    while True:
        try:
            _check_and_fire_due_alarms()
        except Exception:
            logger.warning("Fallo el ciclo del reloj de alarmas", exc_info=True)
        await asyncio.sleep(ALARM_POLL_INTERVAL_SECONDS)
