"""
Reloj de agentes programados (spec agentes-programados, pedido del usuario
2026-09): un "agente" automatiza uno de 3 tipos de cosas
(`automation_type`) a un horario dado, en la sesion que el usuario eligio
al crearlo -- una lista de comandos propios en secuencia ('command', ver
`app/commands.py`), una skill ('skill'), o un archivo ejecutable
('file').

Este reloj corre en el BACKEND -- dispara aunque no haya ningun navegador
abierto, con tal de que el proceso backend siga vivo (`schedules`/Alarmas
tiene su propio reloj gemelo en app/scheduler/alarms.py, mismo criterio).
Mismo esqueleto de tarea de fondo que `app/persistence/purge.py:
run_purge_loop()`, con una diferencia clave: ACA no se puede delegar el
trabajo a un worker thread (`asyncio.to_thread`) porque `fire_agent()`
termina llamando `_launch_turn()` (app/main.py), que hace
`asyncio.create_task()` -- eso requiere correr en el thread del event loop,
no en un thread aparte.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.commands import list_custom_commands
from app.config import RESOURCE_ROOT
from app.friendly_errors import describe_os_error
from app.persistence import master_index as mi
from app.persistence.service import project_root_for_session
from app.session.events import (
    TURN_STATUS_CANCELLED,
    TURN_STATUS_DONE,
    UserTurn,
    get_session_store,
)

logger = logging.getLogger(__name__)

# Chequeo cada 20s: suficiente para disparar "a las 8" con una tolerancia
# minima, sin martillar la DB local con lecturas cada 1s como hace el
# checker del frontend (que si necesita esa granularidad para sonidos en
# vivo -- un agente no).
AGENT_POLL_INTERVAL_SECONDS = 20

# -- alerta visual persistente (pedido del usuario 2026-09-17) --------------
# Compartido con alarms.py: mismos nombres de WAV (frontend/sounds/*.wav) y
# misma clave de config global de "posponer" (aplica igual a agentes que a
# alarmas, un solo numero en Configuracion en vez de uno por item).
SOUND_NAMES = {"beep", "double", "pulse", "sirena", "success"}
SNOOZE_SETTING_KEY = "alert_snooze_minutes"
DEFAULT_SNOOZE_MINUTES = 10

# REQ-001 (spec agentes-huerfanos-y-gestion-skills, pedido del usuario
# 2026-09-19): "sesion de reserva" para agentes que quedan sin sesion
# destino (ver REQ-002/003 -- que pasa cuando se borra la sesion que un
# agente usaba). Es una sesion NORMAL, creada y elegida por el usuario
# (Vision nunca la crea sola ni le asigna carpeta) -- esta clave solo
# guarda CUAL session_id fue elegido como reserva. Mismo patron que
# SNOOZE_SETTING_KEY: un unico valor global en `app_settings`, no por item.
AGENT_FALLBACK_SESSION_KEY = "agent_fallback_session_id"


def wav_path(sound: str) -> str:
    name = sound if sound in SOUND_NAMES else "beep"
    return str(RESOURCE_ROOT / "frontend" / "sounds" / f"{name}.wav")


def get_snooze_minutes() -> int:
    raw = mi.get_app_setting(SNOOZE_SETTING_KEY)
    try:
        n = int(raw) if raw else DEFAULT_SNOOZE_MINUTES
    except (TypeError, ValueError):
        n = DEFAULT_SNOOZE_MINUTES
    return max(1, min(60, n))


def get_fallback_session_id() -> str:
    """REQ-001: session_id configurado como reserva para agentes huerfanos,
    o "" si el usuario todavia no eligio ninguna (nunca None -- mas facil
    de comparar contra un session_id vacio/inexistente en el resto del
    modulo)."""
    return mi.get_app_setting(AGENT_FALLBACK_SESSION_KEY) or ""


# ---------------------------------------------------------------------------
# Port a Python de nextDueMs/_timeOfDayMs/_nextWeekdayFrom
# (frontend/js/app.js ~L5267-5329) -- MISMA semantica de reglas
# (daily/weekdays/weekly/ndays/nhours/once) y MISMA forma de trigger_json,
# para que un agente y una alarma "hablen" del mismo vocabulario de
# horarios. Corre en hora LOCAL de la maquina (igual que `new Date()` en el
# navegador) -- datetime naive + .timestamp() alcanza porque backend y
# frontend viven en la misma maquina Windows.
# ---------------------------------------------------------------------------
def _time_of_day_ts(base: datetime, hm: str) -> float:
    try:
        hh_s, mm_s = (hm or "08:30").split(":")
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError):
        hh, mm = 8, 30
    # BL-007 (ADR-006, bug #2): una hora fuera de rango ("25:99", "24:00", "08:60") hacia que `replace()` lanzara
    # ValueError FUERA de este try; como `is_due_now` se llama sin proteccion en `_check_and_fire_due_agents`, un solo
    # agente mal escrito abortaba el chequeo de TODOS los demas cada 20 s. Mismo criterio que para el texto no
    # parseable: se cae en 08:30 en vez de romper.
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        hh, mm = 8, 30
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def _weekday_of(trigger: dict[str, Any]) -> int:
    """Dia de la semana del trigger en convencion JS (0=domingo..6=sabado), como el <select> del frontend.

    BL-007 (ADR-006, bug #1): antes se leia con `int(trigger.get("weekday") or 1)`, y como `0 or 1 == 1` un agente semanal
    del DOMINGO (la UI manda el NUMERO 0) se leia como LUNES y nunca disparaba el domingo. Ahora solo la ausencia (`None`,
    "") o un valor ilegible caen en lunes -- igual que antes -- y 0 es domingo. Fuera de 0..6 tambien cae en lunes."""
    raw = trigger.get("weekday")
    if raw is None or raw == "":
        return 1
    try:
        dow = int(raw)
    except (TypeError, ValueError):
        return 1
    return dow if 0 <= dow <= 6 else 1


def _next_weekday_from(base: datetime, target_dow: int, hm: str) -> float | None:
    """target_dow en convencion JS Date.getDay() (0=domingo..6=sabado), igual
    que el <select id="alarm-weekday"> de index.html -- Python's
    date.weekday() es Lunes=0, por eso se convierte abajo."""
    now = time.time()
    for add in range(8):
        day = base + timedelta(days=add)
        js_dow = (day.weekday() + 1) % 7
        if js_dow == target_dow:
            ts = _time_of_day_ts(day, hm)
            if ts > now:
                return ts
    return None


def next_due(agent: dict[str, Any]) -> float | None:
    """Proximo timestamp (epoch, local) en que este agente deberia
    disparar, o None si no se puede calcular (regla desconocida / 'once'
    con fecha invalida)."""
    trigger = agent.get("trigger") or {}
    now = time.time()

    if agent["kind"] == "once":
        iso = trigger.get("target_iso") or ""
        try:
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None

    hm = trigger.get("time") or "08:30"
    rule = trigger.get("rule")
    today = datetime.now()  # noqa: DTZ005 -- hora local de la maquina (ver comentario del modulo)

    if rule == "daily":
        ts = _time_of_day_ts(today, hm)
        while ts <= now:
            ts += 86400
        return ts

    if rule == "weekdays":
        for add in range(7):
            day = today + timedelta(days=add)
            if day.weekday() <= 4:  # lunes(0)..viernes(4)
                ts = _time_of_day_ts(day, hm)
                if ts > now:
                    return ts
        return None

    if rule == "weekly":
        return _next_weekday_from(today, _weekday_of(trigger), hm)

    if rule in ("ndays", "nhours"):
        try:
            n = max(1, int(trigger.get("n") or 1))
        except (TypeError, ValueError):
            n = 1
        step = (n * 86400) if rule == "ndays" else (n * 3600)
        base = agent.get("last_fired_at") or agent.get("created_at") or now
        ts = base + step
        while ts <= now:
            ts += step
        return ts

    return None


def _most_recent_due(agent: dict[str, Any], now: float) -> float | None:
    """Bug real (2026-09, encontrado al probar la alarma ZCC en vivo: no
    sono a las 8:30 NI QUEDO EN EL HISTORIAL): `next_due()` calcula el
    PROXIMO vencimiento ESTRICTAMENTE futuro (`while ts <= now: ts +=
    intervalo`) -- eso es correcto para mostrar "proximo disparo" en la UI,
    pero es la funcion EQUIVOCADA para decidir "hay que disparar ya": por
    construccion, el valor que devuelve SIEMPRE es mayor a `now`, entonces
    la comparacion `due <= now` que hacia el chequeo periodico (aca y en
    alarms.py) NUNCA podia ser verdadera para 'daily'/'weekdays'/'weekly'/
    'ndays'/'nhours' -- ninguna automatizacion recurrente podia disparar
    sola, solo las 'once' (que no se re-calculan hacia adelante).
    Esta funcion hace lo inverso: el vencimiento de HOY, si ya paso (<= now).
    El chequeo periodico dispara si ese vencimiento es mas nuevo que el
    ultimo `last_fired_at` -- asi importa poco cuanto tiempo pasa entre
    polls (20s aca): el disparo de HOY sigue siendo detectable en CUALQUIER
    poll posterior a las 8:30, no solo en el poll exacto.

    A proposito NO mira dias anteriores (nada de "recuperar" un vencimiento
    de ayer): probado en vivo (2026-09-17) con un `range(8)` hacia atras --
    una alarma "Diario a las 23:59" consultada a las 8am volvia a encontrar
    el 23:59 de AYER como "vencimiento reciente" y disparaba de inmediato,
    horas antes de hora. Si el proceso estuvo caido justo en el momento
    exacto del disparo, esa ocurrencia puntual se pierde -- aceptable (el
    objetivo es que no falle el chequeo de HOY, no reconstruir un historial
    perfecto de disparos)."""
    trigger = agent.get("trigger") or {}

    if agent["kind"] == "once":
        # A diferencia de next_due (que devuelve el target SIEMPRE, haya
        # pasado o no -- sirve para mostrar "proximo disparo" en la UI aca
        # se necesita None si TODAVIA no llega, para que is_due_now no lo
        # dispare antes de hora.
        target = next_due(agent)
        return target if target is not None and target <= now else None

    hm = trigger.get("time") or "08:30"
    rule = trigger.get("rule")
    today = datetime.fromtimestamp(now)  # noqa: DTZ006 -- hora local de la maquina

    if rule == "daily":
        ts = _time_of_day_ts(today, hm)
        return ts if ts <= now else None

    if rule == "weekdays":
        if today.weekday() <= 4:
            ts = _time_of_day_ts(today, hm)
            if ts <= now:
                return ts
        return None

    if rule == "weekly":
        target_dow = _weekday_of(trigger)
        if (today.weekday() + 1) % 7 == target_dow:
            ts = _time_of_day_ts(today, hm)
            if ts <= now:
                return ts
        return None

    if rule in ("ndays", "nhours"):
        try:
            n = max(1, int(trigger.get("n") or 1))
        except (TypeError, ValueError):
            n = 1
        step = (n * 86400) if rule == "ndays" else (n * 3600)
        base = agent.get("last_fired_at") or agent.get("created_at") or now
        return base + step if now >= base + step else None

    return None


def is_due_now(agent: dict[str, Any], now: float) -> bool:
    """Reemplaza el chequeo roto `due is None or due > now` (ver docstring
    de `_most_recent_due`): dispara si hubo un vencimiento <= now que
    todavia no se registro en `last_fired_at`.

    Guarda ademas contra `created_at`: sin esto, un agente/alarma recien
    creado a las 14:00 con regla "Diario a las 08:30" se dispararia
    SOLO en el proximo poll (tratando el 08:30 de HOY, ya pasado, como
    "vencimiento perdido") -- practicamente al toque de crearlo, en vez de
    esperar al 08:30 de MAÑANA como espera el usuario. Un vencimiento de
    ANTES de que el agente existiera no cuenta."""
    last_due = _most_recent_due(agent, now)
    if last_due is None:
        return False
    created_at = agent.get("created_at") or 0
    if last_due < created_at:
        return False
    last_fired = agent.get("last_fired_at")
    return last_fired is None or last_fired < last_due


# ---------------------------------------------------------------------------
# REQ-005 (spec agentes-huerfanos-y-gestion-skills): salud del agente --
# detecta en VIVO (al listar, no solo al disparar) si la sesion destino, el
# comando, o la skill de un agente ya no existen. Antes de esta feature,
# `_resolve_prompts` (mas abajo) ya detectaba comandos faltantes pero SOLO
# en el momento del DISPARO -- nunca al listar, y NUNCA para skills (gap
# real encontrado en la investigacion previa a la spec). Prioridad fija
# quand multiples campos fallan a la vez (decision explicita registrada en
# spec.md, seccion Reglas de negocio): session_id > commands/skill_name.
# ---------------------------------------------------------------------------
def compute_agent_broken_status(agent: dict[str, Any]) -> dict[str, str] | None:
    """Devuelve `{"field": ..., "detail": ...}` si este agente tiene una
    configuracion invalida, o None si esta sano. `field` es uno de
    "session_id" | "commands" | "skill_name" -- usado por el frontend para
    resaltar el control especifico en el formulario de edicion."""
    from app.persistence import master_index as mi  # import diferido: evita ciclo
    from app.tools.skill import (  # import diferido: evita ciclo (tools importa scheduler indirecto)
        discover_local_skills,
        discover_skills,
    )

    session_id = agent.get("session_id") or ""
    if not session_id or not mi.session_index_exists(session_id):
        return {
            "field": "session_id",
            "detail": "La sesión destino de este agente ya no existe.",
        }

    automation_type = agent.get("automation_type") or "command"
    if automation_type == "command":
        commands = agent.get("commands") or []
        if commands:
            _, missing = _resolve_prompts(session_id, commands)
            if missing:
                faltantes = ", ".join(f"/{n}" for n in missing)
                return {
                    "field": "commands",
                    "detail": f"Ya no existe(n): {faltantes}.",
                }
    elif automation_type == "skill":
        skill_name = (agent.get("skill_name") or "").strip()
        if skill_name:
            all_names = {s["name"] for s in discover_skills()}
            all_names |= {s["name"] for s in discover_local_skills(session_id)}
            if skill_name not in all_names:
                return {
                    "field": "skill_name",
                    "detail": f"La skill '{skill_name}' ya no existe.",
                }

    return None


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------
def _resolve_prompts(
    session_id: str, command_names: list[str]
) -> tuple[list[str], list[str]]:
    """Resuelve el prompt de cada comando de la lista, en el mismo orden --
    reusa `list_custom_commands` tal cual (misma fuente de verdad que la
    pestaña Comandos), en vez de leer los .md por su cuenta. Devuelve
    (prompts, faltantes): `faltantes` son los nombres que ya no existen en
    la carpeta de esta sesion -- se reportan todos juntos para no disparar
    la secuencia a medias."""
    project_path = project_root_for_session(session_id)
    available = {
        c["name"]: c["prompt"]
        for c in list_custom_commands(str(project_path) if project_path else None)
    }
    prompts: list[str] = []
    missing: list[str] = []
    for name in command_names:
        if name in available:
            prompts.append(available[name])
        else:
            missing.append(name)
    return prompts, missing


def fire_agent(agent: dict[str, Any]) -> None:
    """Dispara este agente segun su `automation_type`:
      - 'command': arranca (o encola) la SECUENCIA de turnos de comandos,
        en el orden elegido por el usuario (REQ 2026-09: "varios comandos
        que se ejecuten de manera secuencial en el orden que yo elija").
      - 'skill': arranca UN turno sintetico "/skill <nombre> " -- mismo
        texto que manda el panel de accesos rapidos al clickear una skill
        (ver insertSlashSkill en app.js), asi que se comporta identico a
        que el usuario la hubiera invocado a mano.
      - 'file': ejecuta el archivo DIRECTO (os.startfile, auditado), sin
        pasar por el LLM -- mismo mecanismo que ya usaba schedules.file_path.
    Registra una fila en `agent_runs` SIEMPRE, para que el historial nunca
    deje un disparo sin rastro. Ademas siempre muestra la alerta visual
    nativa (pedido del usuario 2026-09-17) -- los agentes no tienen un
    campo `sound` propio (a diferencia de las alarmas), asi que usan
    siempre "beep"."""
    from app import notifier  # noqa: PLC0415 -- import diferido: evita ciclo con main

    notifier.show_alert(
        "agent",
        agent["id"],
        agent.get("label") or "(sin etiqueta)",
        wav_path("beep"),
        get_snooze_minutes(),
    )
    automation_type = agent.get("automation_type") or "command"
    if automation_type == "file":
        _fire_file_agent(agent)
    elif automation_type == "skill":
        _fire_skill_agent(agent)
    else:
        _fire_command_agent(agent)


def _fire_file_agent(agent: dict[str, Any]) -> None:
    """automation_type='file': sincronico, nunca pasa por 'running' --
    termina apenas `os.startfile` retorna (o falla)."""
    import os

    from app.tools import base as tool_base  # import diferido: evita ciclo

    agent_id = agent["id"]
    session_id = agent["session_id"]
    run_id = uuid.uuid4().hex
    path = (agent.get("file_path") or "").strip()
    mi.create_agent_run(run_id, agent_id, session_id, [], [])
    if not path:
        mi.finish_agent_run(
            run_id, "error", "Este agente no tiene un archivo configurado."
        )
        return
    try:
        # os.startfile: abre/ejecuta con la app por defecto de Windows
        # (equivale a doble clic). Windows unicamente -- Vision es local en win32.
        os.startfile(path)
        tool_base._audit(
            "run_file", {"path": path, "status": "ok", "source": f"agent:{agent_id}"}
        )
        mi.finish_agent_run(run_id, "done")
    except OSError as exc:
        tool_base._audit(
            "run_file",
            {
                "path": path,
                "status": "error",
                "error": str(exc),
                "source": f"agent:{agent_id}",
            },
        )
        # El historial de ejecuciones lo lee cualquier persona: texto simple
        # (errores entendibles, 2026-09-24); el crudo queda en el audit de arriba.
        mi.finish_agent_run(run_id, "error", describe_os_error(exc).message)


def _fire_command_agent(agent: dict[str, Any]) -> None:
    session_id = agent["session_id"]
    agent_id = agent["id"]
    commands = agent.get("commands") or []
    run_id = uuid.uuid4().hex

    if not commands:
        mi.create_agent_run(run_id, agent_id, session_id, [], [])
        mi.finish_agent_run(
            run_id, "error", "Este agente no tiene ningún comando configurado."
        )
        return

    prompts, missing = _resolve_prompts(session_id, commands)
    if missing:
        mi.create_agent_run(run_id, agent_id, session_id, commands, [])
        faltantes = ", ".join(f"/{n}" for n in missing)
        mi.finish_agent_run(
            run_id,
            "error",
            f"No se disparó ningún comando: ya no existe(n) {faltantes} en la carpeta de esta sesión.",
        )
        return

    _launch_sequence(agent_id, session_id, run_id, commands, prompts)


def _fire_skill_agent(agent: dict[str, Any]) -> None:
    """automation_type='skill': secuencia de UN solo turno -- reusa el
    mismo lanzador que 'command' (una secuencia de largo 1 es un caso
    particular, no amerita codigo aparte)."""
    session_id = agent["session_id"]
    agent_id = agent["id"]
    skill_name = (agent.get("skill_name") or "").strip()
    run_id = uuid.uuid4().hex
    if not skill_name:
        mi.create_agent_run(run_id, agent_id, session_id, [], [])
        mi.finish_agent_run(
            run_id, "error", "Este agente no tiene ninguna skill configurada."
        )
        return
    _launch_sequence(
        agent_id, session_id, run_id, [f"skill:{skill_name}"], [f"/skill {skill_name} "]
    )


def _launch_sequence(
    agent_id: str,
    session_id: str,
    run_id: str,
    commands_label: list[str],
    prompts: list[str],
) -> None:
    """Arranca (o encola, si la sesion destino ya esta ocupada) una
    SECUENCIA de turnos sinteticos, uno por prompt, en orden -- reusa el
    mismo mecanismo de cola que ya existe para mensajes humanos
    (`session.enqueue_turn` + el dequeue automatico dentro de
    `_launch_turn`, ver POST /api/chat en main.py): se lanza el primer
    turno y se encolan los demas atras -- cada uno arranca solo cuando el
    anterior termina, sin loop propio nuevo. `commands_label` es lo que
    queda guardado en `agent_runs.commands_json` para mostrar en el
    historial (nombres de comando, o "skill:<nombre>")."""
    from app.main import (
        _launch_turn,  # import diferido: evita ciclo (main importa este modulo)
    )

    store = get_session_store()
    session = store.get_or_create(session_id)
    turns = [UserTurn(id=f"agent-{uuid.uuid4().hex}", text=p) for p in prompts]

    mi.create_agent_run(
        run_id, agent_id, session_id, commands_label, [t.id for t in turns]
    )
    if session.is_busy:
        for t in turns:
            session.enqueue_turn(t)
    else:
        first, *rest = turns
        _launch_turn(session, session_id, first)
        for t in rest:
            session.enqueue_turn(t)


def run_agent_now(agent_id: str) -> bool:
    """Disparo MANUAL ("Ejecutar ahora", pedido del usuario para el panel
    lateral de la sesion), fuera del horario programado. Mismo camino que
    un disparo real del reloj (fire_agent + marca last_fired_at) para que
    un 'once' no se vuelva a disparar mas tarde por el horario configurado.
    Devuelve False si el agente no existe."""
    agent = mi.get_agent(agent_id)
    if agent is None:
        return False
    fire_agent(agent)
    mi.update_agent_last_fired(agent_id, time.time())
    return True


def stop_agent(agent_id: str) -> bool:
    """Corta la secuencia ENTERA de la ejecucion en curso de este agente --
    no alcanza con el stop cooperativo generico (POST /api/chat/{id}/stop),
    que solo corta el turno ACTIVO: sin esto, apenas ese turno terminara de
    cortarse, `_launch_turn` arrancaria solo el siguiente paso YA ENCOLADO
    de la MISMA secuencia (el dequeue automatico no distingue "el usuario
    quiere frenar todo" de "segui con la cola normal"). Pedido explicito
    del usuario: nunca debe quedar un agente corriendo sin poder pararlo
    del todo. Devuelve False si no hay ninguna ejecucion 'running' de este
    agente."""
    running = [r for r in mi.list_running_agent_runs() if r["agent_id"] == agent_id]
    if not running:
        return False

    store = get_session_store()
    stopped_any = False
    for run in running:
        session = store.get(run["session_id"])
        if session is None:
            continue
        # 1) turno activo (si es uno de los de esta secuencia): stop
        #    cooperativo, mismo mecanismo que /api/chat/{id}/stop -- NO
        #    mata la tarea a la fuerza, deja que la tool en curso termine.
        if session.active_turn_id in run["turn_ids"]:
            session.stop_requested = True
            session.edit_pending_text = None
            stopped_any = True
        # 2) pasos restantes de ESTA secuencia que todavia esten en cola
        #    (no arrancaron): se sacan de la cola (quedan 'cancelled') --
        #    si no, el siguiente paso arrancaria solo apenas el activo corte.
        for turn_id in run["turn_ids"]:
            if session.remove_queued_turn(turn_id):
                stopped_any = True
    return stopped_any


def _check_and_fire_due_agents() -> None:
    from app import notifier  # noqa: PLC0415 -- import diferido: evita ciclo con main

    now = time.time()
    # REQ-006 (spec agentes-huerfanos-y-gestion-skills, documentado a
    # pedido explicito del usuario: "si tenemos dos agentes con el mismo
    # horario y fecha se encolan priorizando el creado"): `mi.list_agents()`
    # ya devuelve las filas `ORDER BY created_at ASC` (ver master_index.py),
    # y el dict comprehension de abajo PRESERVA ese orden de insercion
    # (garantia de Python 3.7+ para dict.values(), usado mas abajo en el
    # `for agent in agents_by_id.values()`) -- sin cambio de comportamiento,
    # solo se deja explicito que el desempate es INTENCIONAL: si dos
    # agentes vencen exactamente en el mismo instante, el creado ANTES se
    # dispara primero. Test de regresion: test_agents_db.py::
    # test_fire_order_prioritizes_oldest_created_on_tie.
    agents_by_id = {a["id"]: a for a in mi.list_agents()}

    # "Zzz" (posponer, pedido del usuario 2026-09-17): re-dispara SIN pasar
    # por is_due_now -- el snooze es una orden explicita del usuario ("volve
    # a sonar en N minutos"), no depende de si el horario original sigue
    # vigente.
    for agent_id in notifier.pop_due_snoozes("agent", now):
        agent = agents_by_id.get(agent_id)
        if agent is None or not agent["enabled"]:
            continue
        try:
            fire_agent(agent)
        except Exception:
            logger.warning(
                "Fallo al re-disparar (Zzz) el agente '%s' (%s)",
                agent["label"],
                agent["id"],
                exc_info=True,
            )
        finally:
            mi.update_agent_last_fired(agent["id"], now)

    for agent in agents_by_id.values():
        if not agent["enabled"]:
            continue
        # is_due_now (no next_due) a proposito: ver docstring de
        # _most_recent_due, bug real 2026-09-17 -- next_due()>now por
        # construccion, nunca disparaba nada recurrente. is_due_now ademas
        # ya cubre el reprogramado de un "once" (compara contra `due`
        # actual, no solo "alguna vez disparo").
        if not is_due_now(agent, now):
            continue
        try:
            fire_agent(agent)
        except Exception:
            logger.warning(
                "Fallo al disparar el agente '%s' (%s)",
                agent["label"],
                agent["id"],
                exc_info=True,
            )
        finally:
            # Se marca como disparado incluso si fallo -- evita un loop de
            # reintento infinito contra un agente roto; el error queda
            # visible en agent_runs.
            mi.update_agent_last_fired(agent["id"], now)


def _sweep_finished_runs() -> None:
    """Cierra las filas `agent_runs` que quedaron 'running' apenas TODOS
    los turnos de la secuencia (uno por comando/skill) terminan -- nunca
    queda un run colgado sin que el usuario sepa que paso (ver historial en
    la pestaña Agentes). No aplica a automation_type='file': esas se
    cierran SINCRONICAMENTE al disparar (`_fire_file_agent`), nunca quedan
    'running'."""
    store = get_session_store()
    for run in mi.list_running_agent_runs():
        turn_ids = run["turn_ids"]
        if not turn_ids:
            continue  # ya se cerro como error al disparar (nada faltante deberia quedar 'running' sin turnos)

        session = store.get(run["session_id"])
        if session is None:
            # Backend reiniciado (o sesion nunca se reconstruyo en memoria):
            # no hay forma de saber el resultado real -- se cierra igual
            # para no dejarlo 'running' para siempre.
            mi.finish_agent_run(
                run["id"], "done", "No se sabe cómo terminó: Vision se reinició mientras corría."
            )
            continue

        turns = [session.turns.get(tid) for tid in turn_ids]
        if any(t is None for t in turns):
            mi.finish_agent_run(
                run["id"], "done", "No se sabe cómo terminó: Vision se reinició mientras corría."
            )
            continue

        if any(t.status == TURN_STATUS_CANCELLED for t in turns):
            # Al menos un paso de la secuencia se cancelo (via "Detener") --
            # si ya no queda ninguno 'queued'/'active', la secuencia entera
            # se considera cancelada (aunque pasos anteriores hayan
            # terminado completos).
            if all(
                t.status in (TURN_STATUS_DONE, TURN_STATUS_CANCELLED) for t in turns
            ):
                mi.finish_agent_run(run["id"], "cancelled")
            continue

        if all(t.status == TURN_STATUS_DONE for t in turns):
            mi.finish_agent_run(run["id"], "done")
        # si algun turno sigue 'queued'/'active', se deja 'running' y se
        # revisa de nuevo en el proximo tick.


async def run_agents_loop() -> None:
    """Tarea de background de larga duracion: dispara agentes vencidos y
    cierra runs terminados cada AGENT_POLL_INTERVAL_SECONDS. Se lanza una
    sola vez desde el startup de FastAPI (ver app/main.py), igual que
    run_purge_loop() -- pero SIN asyncio.to_thread (ver docstring del
    modulo: fire_agent() necesita el thread del event loop)."""
    while True:
        try:
            _check_and_fire_due_agents()
            _sweep_finished_runs()
        except Exception:
            logger.warning("Fallo el ciclo del reloj de agentes", exc_info=True)
        await asyncio.sleep(AGENT_POLL_INTERVAL_SECONDS)
