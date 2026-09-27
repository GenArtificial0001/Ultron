"""
Indice maestro de sesiones (arquitectura "carpeta-por-sesion", version
DEFINITIVA -- ver docstring largo en app/persistence/service.py para el
historial completo de idas y vueltas de esta decision): un SQLite propio
en `<carpeta de datos>\\vision.db`, FUERA del repo de Vision y FUERA
de cualquier carpeta de proyecto de usuario -- rompe la circularidad
"para saber en que carpeta vive la sesion X, necesitaria abrir el
vision.db de esa carpeta, pero para eso ya necesitaria saber la carpeta".

Guarda el PUNTERO liviano `session_id -> project_path` mas metadata
minima (titulo, quien la creo) -- el contenido pesado (mensajes,
imagenes, todos) sigue viviendo exclusivamente en el `vision.db` DENTRO
de `<project_path>/db/` (spec persistencia-sqlite-sesiones, subcarpeta
renombrada de "session/" a "db/" en spec carpeta-por-sesion REQ-009 --
mismo criterio de portabilidad, sin cambios). CADA sesion tiene su PROPIA
entrada aca -- a diferencia de la version intermedia que hubo brevemente,
ya NO existe ningun fallback a una carpeta global: sin entrada en este
indice, la sesion es 100% en memoria."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.persistence.sqlite_wal import ensure_wal

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_index (
    session_id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    windows_user_created TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
-- spec programador-alarmas (pedido del usuario 2026-09: persistir en el DB
-- GLOBAL de Vision, no por-sesion): los programados viven a nivel Vision --
-- validos sin importar la sesion activa. Sin FK a session (es global) --
-- eso es lo que hace que una tarea 'a las 8h' suene esté la sesion que esté.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'once' | 'recurring' | 'pomodoro'
    trigger_json TEXT NOT NULL DEFAULT '{}',
    sound TEXT NOT NULL DEFAULT 'beep',  -- 'beep' | 'double' | 'pulse' | 'sirena'
    file_path TEXT NOT NULL DEFAULT '',  -- ruta opcional .exe/.bat/.ps1 a ejecutar al disparar
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- spec agentes-programados (pedido del usuario 2026-09, unificado con la
-- pestaña Tiempo/Agenda salvo Alarmas/Recordatorios -- esas siguen en
-- `schedules`, ver docstring de esa tabla): un "agente" automatiza UNA de
-- 3 cosas (`automation_type`) a un horario dado --
--   'command': UNA LISTA ORDENADA de comandos propios (`commands_json`,
--              cada uno vive en `commands/<name>.md` de la carpeta de
--              `session_id`) que se ejecutan uno detras del otro, en el
--              orden elegido (REQ 2026-09: "varios comandos que se
--              ejecuten de manera secuencial en el orden que yo elija").
--   'skill':   UNA skill (`skill_name`) -- se dispara igual que si el
--              usuario escribiera "/skill <nombre>" en el chat.
--   'file':    UN archivo (`file_path`, .exe/.bat/.ps1) -- se ejecuta
--              directo con `os.startfile` (mismo mecanismo ya auditado
--              que usaba `schedules.file_path`), sin pasar por el LLM.
-- El reloj de `agents` corre en el BACKEND (ver app/scheduler/agents.py) --
-- dispara aunque no haya ningun navegador abierto, con tal de que el
-- proceso backend siga vivo. `schedules` (Alarmas/Recordatorios) tiene su
-- PROPIO reloj de backend (app/scheduler/alarms.py, mismo criterio) porque
-- dispara sonido/toast NATIVO de Windows en vez de un turno del LLM --
-- por eso 'file'/'command'/'skill' viven en `agents` y no en `schedules`.
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    session_id TEXT NOT NULL,
    automation_type TEXT NOT NULL DEFAULT 'command',  -- 'command' | 'skill' | 'file'
    commands_json TEXT NOT NULL DEFAULT '[]',  -- automation_type='command': lista ORDENADA de nombres
    skill_name TEXT NOT NULL DEFAULT '',       -- automation_type='skill'
    file_path TEXT NOT NULL DEFAULT '',        -- automation_type='file'
    kind TEXT NOT NULL,          -- 'once' | 'recurring'
    trigger_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Log historico de cada disparo de un agente -- para que el usuario pueda
-- ver que paso con cada ejecucion (exito/cancelado/error) sin tener que
-- adivinar si algo quedo corriendo en silencio. Un disparo = UNA secuencia
-- de turnos (uno por comando, mismo orden que `commands_json` del agente
-- al momento de dispararse); `commands_json`/`turn_ids_json` son listas
-- paralelas del mismo largo.
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    commands_json TEXT NOT NULL DEFAULT '[]',
    turn_ids_json TEXT NOT NULL DEFAULT '[]',
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'done' | 'cancelled' | 'error'
    error TEXT NOT NULL DEFAULT ''
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Migraciones defensivas para un `vision.db` creado ANTES de agregar
    una columna nueva -- `CREATE TABLE IF NOT EXISTS` no altera tablas ya
    existentes (mismo criterio que `persistence/db.py:_migrate`). Cubre dos
    casos reales de esta maquina: (1) el filtro de `session_index` por
    usuario nunca matcheaba nada (ver `list_all_for_user`, el bug en si se
    arregla ahi, no aca) y (2) `agents`/`agent_runs` pasaron de "un
    comando" a "una lista ordenada de comandos" el mismo dia que se creo la
    feature -- instalaciones que ya habian corrido la version vieja (aunque
    sea un rato) tienen `command_name`/`turn_id` en vez de
    `commands_json`/`turn_ids_json`."""
    agent_cols = [row[1] for row in conn.execute("PRAGMA table_info(agents)")]
    if agent_cols and "automation_type" not in agent_cols:
        # Instalaciones que ya tenian `agents` de antes de sumar
        # skill/archivo: todo lo existente era 'command' (unico tipo que
        # habia), asi que el default alcanza sin backfill adicional.
        conn.execute(
            "ALTER TABLE agents ADD COLUMN automation_type TEXT NOT NULL DEFAULT 'command'"
        )
        conn.execute(
            "ALTER TABLE agents ADD COLUMN skill_name TEXT NOT NULL DEFAULT ''"
        )
        conn.execute("ALTER TABLE agents ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")
    if agent_cols and "commands_json" not in agent_cols:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN commands_json TEXT NOT NULL DEFAULT '[]'"
        )
        if "command_name" in agent_cols:
            # Python (no SQL crudo) para armar el JSON -- evita escapar
            # comillas/backslashes a mano en una UPDATE.
            for agent_id, command_name in conn.execute(
                "SELECT id, command_name FROM agents WHERE command_name IS NOT NULL AND command_name != ''"
            ).fetchall():
                conn.execute(
                    "UPDATE agents SET commands_json = ? WHERE id = ?",
                    (json.dumps([command_name]), agent_id),
                )
            # SQLite moderno (>=3.35, ver requirements): DROP COLUMN es
            # soportado -- se saca del todo en vez de dejar un campo
            # NOT NULL sin uso que las INSERT nuevas ya no completan.
            conn.execute("ALTER TABLE agents DROP COLUMN command_name")

    run_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")]
    if run_cols and "turn_ids_json" not in run_cols:
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN commands_json TEXT NOT NULL DEFAULT '[]'"
        )
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN turn_ids_json TEXT NOT NULL DEFAULT '[]'"
        )
        if "turn_id" in run_cols:
            for run_id, turn_id in conn.execute(
                "SELECT id, turn_id FROM agent_runs WHERE turn_id IS NOT NULL AND turn_id != ''"
            ).fetchall():
                conn.execute(
                    "UPDATE agent_runs SET turn_ids_json = ? WHERE id = ?",
                    (json.dumps([turn_id]), run_id),
                )
            conn.execute("ALTER TABLE agent_runs DROP COLUMN turn_id")


def master_db_path() -> Path:
    """REQ-001: ubicacion FIJA, ajena a cualquier proyecto: la carpeta de
    datos de Vision (spec vision-generico: antes era `~/.vision/vision.db`,
    compartida con cualquier otro Vision instalado en la misma PC)."""
    from app.config import BASE_DIR  # import diferido: evita ciclos al importar

    return BASE_DIR / "vision.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    db_path = master_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        ensure_wal(conn)  # QA-009: sin competir por lock exclusivo, ver sqlite_wal.py
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_project_path(session_id: str) -> str | None:
    """REQ-002: resuelve la carpeta de UNA sesion especifica -- devuelve
    None si la sesion no tiene ninguna entrada todavia (nunca se le asigno
    carpeta, o es una sesion que solo vive en memoria sin persistir)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT project_path FROM session_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        return row[0]


def get_owner(session_id: str) -> str | None:
    """Devuelve el `windows_user_created` (SIN el sufijo de timestamp) del
    dueño registrado de esta sesion en el indice maestro, o None si la
    sesion no tiene entrada (nunca persistida / solo en memoria). Sirve al
    control de autorizacion por objeto de los endpoints de sesion (ver
    `_assert_session_owner` en main.py): con una carpeta de proyecto
    compartida entre varios usuarios de Windows, cada session_id debe
    pertenecer a quien hace el request, no solo al que lo creo."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT windows_user_created FROM session_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None or not row[0]:
        return None
    return row[0].rsplit("_", 1)[0]  # quita el sufijo _yyyymmddhhmmss


def set_project_path(
    session_id: str, project_path: str, title: str = "", windows_user_created: str = ""
) -> None:
    """REQ-001/007: crea o actualiza el puntero de una sesion. Se llama
    tanto al asignar carpeta por primera vez como al CAMBIARLA despues
    (spec §6 reglas de negocio: cambiar la carpeta solo mueve el puntero
    hacia adelante, nunca migra datos entre vision.db distintos)."""
    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT session_id FROM session_index WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO session_index "
                "(session_id, project_path, title, windows_user_created, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, project_path, title, windows_user_created, now, now),
            )
        else:
            conn.execute(
                "UPDATE session_index SET project_path = ?, updated_at = ? "
                "WHERE session_id = ?",
                (project_path, now, session_id),
            )


def update_title(session_id: str, title: str) -> None:
    """Mantiene el titulo espejado en el indice maestro (util para
    listados futuros que crucen carpetas distintas sin abrir cada
    vision.db) -- no-op si la sesion no tiene entrada todavia."""
    with _connect() as conn:
        conn.execute(
            "UPDATE session_index SET title = ?, updated_at = ? WHERE session_id = ?",
            (title, time.time(), session_id),
        )


def get_app_setting(key: str) -> str | None:
    """Lee una config de APLICACION (nivel Vision, NO por sesion) del
    `app_settings` de la DB maestra (skills_repo_path, habilitacion global
    de skills, etc.). None si no esta seteada."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None and row[0] else None


def set_app_setting(key: str, value: str) -> None:
    """Persiste una config de aplicacion (nivel Vision) en `app_settings`."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def unset_app_setting(key: str) -> None:
    """Elimina una config de aplicacion (para la accion de RESET)."""
    with _connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


def delete_session(session_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM session_index WHERE session_id = ?", (session_id,))


def session_index_exists(session_id: str) -> bool:
    """REQ-005 (spec agentes-huerfanos-y-gestion-skills): confirma si
    `session_id` sigue teniendo una fila en el indice maestro -- usado para
    detectar que un agente quedo "sin sesion" tras un borrado, sin
    confundirlo con `get_project_path` (que devuelve None tambien para una
    sesion que EXISTE pero nunca tuvo carpeta asignada -- caso valido, no
    roto)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM session_index WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None


def list_all_for_user(windows_user: str) -> list[dict[str, Any]]:
    """Lista TODAS las entradas del indice creadas por `windows_user`,
    sin importar en que carpeta viven -- util para un futuro listado
    cross-carpeta (fuera de alcance del build inicial de esta spec, pero
    el esquema ya lo soporta).

    Bug real detectado 2026-09 (agentes-programados, "no me reconoce las
    sesiones que tengo guardadas"): esta query esperaba
    `windows_user_created` con sufijo `_yyyymmddhhmmss` (formato que
    `get_owner()` de arriba SI espera, via rsplit), pero
    `assign_project_path()` (persistence/service.py) siempre escribio el
    nombre de usuario PELADO via `current_windows_user()` -- sin sufijo.
    Contra `LIKE '<user>_%'` un valor pelado como "USUARIO1" nunca matchea
    (no tiene el "_" que exige el patron), asi que esta funcion devolvia []
    para CUALQUIER usuario real. Se amplia el match a: exacto (el formato
    real), con sufijo (por si algun dia se usa), o vacio -- mismo criterio
    "fail-open" que `get_owner()` ya aplica para sesiones sin dueño
    registrado (no deberian quedar invisibles para nadie)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT session_id, project_path, title, windows_user_created, "
            "created_at, updated_at FROM session_index "
            "WHERE windows_user_created = ? OR windows_user_created LIKE ? "
            "OR windows_user_created = '' "
            "ORDER BY updated_at DESC",
            (windows_user, f"{windows_user}_%"),
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "project_path": r[1],
                "title": r[2],
                "windows_user_created": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Programados globales (spec programador-alarmas; pedido del usuario 2026-09:
# persistencia en el DB GLOBAL de Vision -- no por-sesion). Esta tabla es la
# fuente de verdad; el motor de disparo es el backend (app/scheduler/
# alarms.py) desde 2026-09 (antes solo el frontend, que necesitaba la
# pestaña de Vision abierta en el momento exacto del horario).
# ---------------------------------------------------------------------------
def _schedule_row(row: tuple[Any, ...]) -> dict[str, Any]:
    try:
        trigger = json.loads(row[3]) if row[3] else {}
    except (ValueError, TypeError):
        trigger = {}
    return {
        "id": row[0],
        "label": row[1],
        "kind": row[2],
        "trigger": trigger,
        "sound": row[4],
        "file_path": row[5],
        "enabled": bool(row[6]),
        "last_fired_at": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


def list_schedules() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, kind, trigger_json, sound, file_path, enabled, "
            "last_fired_at, created_at, updated_at FROM schedules ORDER BY created_at ASC"
        ).fetchall()
        return [_schedule_row(r) for r in rows]


def create_schedule(
    schedule_id: str,
    label: str,
    kind: str,
    trigger: dict[str, Any],
    sound: str,
    file_path: str,
    enabled: bool,
) -> dict[str, Any]:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedules (id, label, kind, trigger_json, sound, file_path, "
            "enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id,
                label,
                kind,
                json.dumps(trigger, ensure_ascii=False),
                sound,
                file_path,
                1 if enabled else 0,
                now,
                now,
            ),
        )
    return _schedule_row(
        (
            schedule_id,
            label,
            kind,
            json.dumps(trigger, ensure_ascii=False),
            sound,
            file_path,
            1 if enabled else 0,
            None,
            now,
            now,
        )
    )


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, label, kind, trigger_json, sound, file_path, enabled, "
            "last_fired_at, created_at, updated_at FROM schedules WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        return _schedule_row(row) if row is not None else None


def update_schedule(schedule_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    cur = get_schedule(schedule_id)
    if cur is None:
        return None
    if "label" in patch:
        cur["label"] = patch["label"]
    if "kind" in patch:
        cur["kind"] = patch["kind"]
    if "trigger" in patch and isinstance(patch["trigger"], dict):
        cur["trigger"] = patch["trigger"]
    if "sound" in patch:
        cur["sound"] = patch["sound"]
    if "file_path" in patch:
        cur["file_path"] = patch["file_path"]
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE schedules SET label = ?, kind = ?, trigger_json = ?, sound = ?, "
            "file_path = ?, enabled = ?, updated_at = ? WHERE id = ?",
            (
                cur["label"],
                cur["kind"],
                json.dumps(cur["trigger"], ensure_ascii=False),
                cur["sound"],
                cur["file_path"],
                1 if cur["enabled"] else 0,
                now,
                schedule_id,
            ),
        )
    cur["updated_at"] = now
    return cur


def update_schedule_last_fired(schedule_id: str, ts: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE schedules SET last_fired_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, schedule_id),
        )


def delete_schedule(schedule_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Agentes programados (spec agentes-programados): mismo criterio que
# `schedules` arriba (persistencia GLOBAL, valida sin importar la sesion
# activa) pero el motor de disparo es el BACKEND (app/scheduler/agents.py),
# no el frontend -- ver docstring de la tabla `agents` en el _SCHEMA.
# ---------------------------------------------------------------------------
_AGENT_COLUMNS = (
    "id, label, session_id, automation_type, commands_json, skill_name, "
    "file_path, kind, trigger_json, enabled, last_fired_at, created_at, updated_at"
)


def _agent_row(row: tuple[Any, ...]) -> dict[str, Any]:
    try:
        commands = json.loads(row[4]) if row[4] else []
        if not isinstance(commands, list):
            commands = [commands]
    except (ValueError, TypeError):
        commands = []
    try:
        trigger = json.loads(row[8]) if row[8] else {}
    except (ValueError, TypeError):
        trigger = {}
    return {
        "id": row[0],
        "label": row[1],
        "session_id": row[2],
        "automation_type": row[3],  # 'command' | 'skill' | 'file'
        "commands": commands,  # automation_type='command': lista ORDENADA, se ejecutan en secuencia
        "skill_name": row[5],  # automation_type='skill'
        "file_path": row[6],  # automation_type='file'
        "kind": row[7],
        "trigger": trigger,
        "enabled": bool(row[9]),
        "last_fired_at": row[10],
        "created_at": row[11],
        "updated_at": row[12],
    }


def list_agents() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_AGENT_COLUMNS} FROM agents ORDER BY created_at ASC"
        ).fetchall()
        return [_agent_row(r) for r in rows]


def create_agent(
    agent_id: str,
    label: str,
    session_id: str,
    automation_type: str,
    commands: list[str],
    skill_name: str,
    file_path: str,
    kind: str,
    trigger: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    now = time.time()
    commands_json = json.dumps(commands, ensure_ascii=False)
    trigger_json = json.dumps(trigger, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO agents (id, label, session_id, automation_type, commands_json, "
            "skill_name, file_path, kind, trigger_json, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                label,
                session_id,
                automation_type,
                commands_json,
                skill_name,
                file_path,
                kind,
                trigger_json,
                1 if enabled else 0,
                now,
                now,
            ),
        )
    return _agent_row(
        (
            agent_id,
            label,
            session_id,
            automation_type,
            commands_json,
            skill_name,
            file_path,
            kind,
            trigger_json,
            1 if enabled else 0,
            None,
            now,
            now,
        )
    )


def get_agent(agent_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_AGENT_COLUMNS} FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return _agent_row(row) if row is not None else None


def update_agent(agent_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    cur = get_agent(agent_id)
    if cur is None:
        return None
    if "label" in patch:
        cur["label"] = patch["label"]
    if "session_id" in patch:
        cur["session_id"] = patch["session_id"]
    if "automation_type" in patch:
        cur["automation_type"] = patch["automation_type"]
    if "commands" in patch and isinstance(patch["commands"], list):
        cur["commands"] = patch["commands"]
    if "skill_name" in patch:
        cur["skill_name"] = patch["skill_name"]
    if "file_path" in patch:
        cur["file_path"] = patch["file_path"]
    if "kind" in patch:
        cur["kind"] = patch["kind"]
    if "trigger" in patch and isinstance(patch["trigger"], dict):
        cur["trigger"] = patch["trigger"]
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE agents SET label = ?, session_id = ?, automation_type = ?, "
            "commands_json = ?, skill_name = ?, file_path = ?, kind = ?, "
            "trigger_json = ?, enabled = ?, updated_at = ? WHERE id = ?",
            (
                cur["label"],
                cur["session_id"],
                cur["automation_type"],
                json.dumps(cur["commands"], ensure_ascii=False),
                cur["skill_name"],
                cur["file_path"],
                cur["kind"],
                json.dumps(cur["trigger"], ensure_ascii=False),
                1 if cur["enabled"] else 0,
                now,
                agent_id,
            ),
        )
    cur["updated_at"] = now
    return cur


def update_agent_last_fired(agent_id: str, ts: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE agents SET last_fired_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, agent_id),
        )


def delete_agent(agent_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute("DELETE FROM agent_runs WHERE agent_id = ?", (agent_id,))
        return cur.rowcount > 0


def count_agents_by_session(session_id: str) -> int:
    """REQ-002/003/004 (spec agentes-huerfanos-y-gestion-skills): cuantos
    agentes tienen HOY `session_id` = la sesion dada -- usado tanto para
    avisar al usuario antes de borrar una sesion cualquiera (REQ-002/003)
    como para BLOQUEAR el borrado de la sesion de reserva mientras tenga
    agentes dependientes reales en ese momento (REQ-004, no por el rol de
    "reserva" en abstracto -- ver Decisiones registradas en spec.md)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else 0


def reassign_agents_session(old_session_id: str, new_session_id: str) -> int:
    """REQ-002: reasigna EN BLOQUE (una sola sentencia, nunca parcial) todos
    los agentes de `old_session_id` a `new_session_id` -- se llama ANTES de
    borrar `old_session_id` cuando hay una sesion de reserva configurada.
    Devuelve cuantos agentes se movieron (para el mensaje de confirmacion
    del frontend)."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agents SET session_id = ?, updated_at = ? WHERE session_id = ?",
            (new_session_id, now, old_session_id),
        )
        return cur.rowcount


# -- log historico de disparos (agent_runs) ----------------------------------
# Un disparo = una SECUENCIA de turnos (uno por comando, mismo orden que la
# lista del agente al momento de dispararse). `commands`/`turn_ids` son
# listas paralelas del mismo largo -- turn_ids[i] es el turno que ejecuto
# commands[i].
def _agent_run_row(row: tuple[Any, ...]) -> dict[str, Any]:
    def _load_list(raw: Any) -> list[str]:
        try:
            val = json.loads(raw) if raw else []
            return val if isinstance(val, list) else []
        except (ValueError, TypeError):
            return []

    return {
        "id": row[0],
        "agent_id": row[1],
        "session_id": row[2],
        "commands": _load_list(row[3]),
        "turn_ids": _load_list(row[4]),
        "started_at": row[5],
        "finished_at": row[6],
        "status": row[7],
        "error": row[8],
    }


def create_agent_run(
    run_id: str,
    agent_id: str,
    session_id: str,
    commands: list[str],
    turn_ids: list[str],
) -> dict[str, Any]:
    now = time.time()
    commands_json = json.dumps(commands, ensure_ascii=False)
    turn_ids_json = json.dumps(turn_ids, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, agent_id, session_id, commands_json, "
            "turn_ids_json, started_at, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (run_id, agent_id, session_id, commands_json, turn_ids_json, now),
        )
    return _agent_run_row(
        (
            run_id,
            agent_id,
            session_id,
            commands_json,
            turn_ids_json,
            now,
            None,
            "running",
            "",
        )
    )


def finish_agent_run(run_id: str, status: str, error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, time.time(), run_id),
        )


def list_running_agent_runs() -> list[dict[str, Any]]:
    """Usado por el reloj del backend para barrer las ejecuciones que quedaron
    `status='running'` y cerrarlas apenas TODOS sus turnos terminen (ver
    app/scheduler/agents.py) -- asi nunca queda una fila 'running' colgada
    sin que el usuario pueda saber que paso."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, agent_id, session_id, commands_json, turn_ids_json, "
            "started_at, finished_at, status, error FROM agent_runs "
            "WHERE status = 'running' ORDER BY started_at ASC"
        ).fetchall()
        return [_agent_run_row(r) for r in rows]


def list_agent_runs(
    agent_id: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    with _connect() as conn:
        if agent_id:
            rows = conn.execute(
                "SELECT id, agent_id, session_id, commands_json, turn_ids_json, "
                "started_at, finished_at, status, error FROM agent_runs "
                "WHERE agent_id = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (agent_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, agent_id, session_id, commands_json, turn_ids_json, "
                "started_at, finished_at, status, error FROM agent_runs "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_agent_run_row(r) for r in rows]


def count_agent_runs(agent_id: str | None = None) -> int:
    """Total de filas (sin paginar) -- alimenta el "página X de Y" del
    historial en la pestaña Agentes."""
    with _connect() as conn:
        if agent_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()
        return row[0] if row else 0


def clear_finished_agent_runs(agent_id: str | None = None) -> int:
    """Pedido del usuario: boton "Limpiar historial viejo" -- borra las
    filas YA terminadas (nunca las 'running', para no perder rastro de algo
    en curso). Devuelve cuantas se borraron."""
    with _connect() as conn:
        if agent_id:
            cur = conn.execute(
                "DELETE FROM agent_runs WHERE agent_id = ? AND status != 'running'",
                (agent_id,),
            )
        else:
            cur = conn.execute("DELETE FROM agent_runs WHERE status != 'running'")
        return cur.rowcount
