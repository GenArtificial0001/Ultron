"""
Fachada de persistencia de sesiones (spec persistencia-sqlite-sesiones,
arquitectura definitiva "carpeta-por-sesion"): resuelve DONDE persiste
CADA SESION y expone funciones de alto nivel para que `app/llm/loop.py`,
`app/main.py` y las tools no tengan que conocer el esquema SQL ni el hilo
escritor.

CAMBIO DE ARQUITECTURA (pedido explicito del usuario, version definitiva):
hubo 2 vueltas de decision sobre este punto -- (1) la spec original
proyecto-por-sesion introdujo carpeta-por-sesion via `master_index.py`;
(2) se revirtio brevemente a una unica carpeta GLOBAL (`cfg.local_path`)
por confusion con el caso de uso real; (3) el usuario aclaro el caso real
completo: sesiones distintas son TEMAS/PROYECTOS distintos (ej. "Quimica"
vs "Matematica"), cada uno con su propio AGENTS.md/skills/decisions como
CONTEXTO -- mezclarlos en una carpeta global rompe ese aislamiento. Se
vuelve a carpeta-por-sesion, esta vez DEFINITIVA: ya no hay ningun
fallback a una carpeta global (`cfg.local_path` se elimina del todo, ver
app/project_context.py) -- una sesion SIN carpeta propia asignada
simplemente no persiste nada (chat 100% en memoria) hasta que se le
asigne una, lo cual ahora es OBLIGATORIO al crear la sesion (ver
POST /api/sessions/{id}/project-path y el modal bloqueante del frontend).

Ademas de la carpeta en si, `assign_project_path()` crea la estructura
FIJA de subcarpetas dentro de ella (skills/, sessions/, decisions/,
commands/) -- ver `PROJECT_SUBDIRS` y `ensure_project_structure()` mas
abajo, usado tambien por record_decision/commands/export/skill locales
para saber donde leer y escribir.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.persistence import db, master_index
from app.persistence.writer import submit

logger = logging.getLogger(__name__)

# Subcarpetas fijas que TODA carpeta de proyecto de sesion debe tener,
# creadas automaticamente al asignarla (ver ensure_project_structure).
# Nombres en minuscula (pedido explicito del usuario, ej. "agents.md" en
# vez de "AGENTS.md" -- pero AGENTS.md en si lo crea /init por separado,
# no esta funcion; aca solo van las 4 subcarpetas de contenido generado).
#
# `SKILLS_SUBDIR = "skills"` (con la "s"). Pedido explicito del usuario
# (2026-09): aclaracion de que SOLO se le saco el punto al nombre historico
# `.skills` -- el folder de skills locales dentro de la carpeta de proyecto
# de cada sesion se llama "skills", NO "skill" (singular). Solo cambia la
# ruta de donde se toma; se crea al vuelo cuando se va a usar.
# `_migrate_legacy_skills_dirs()` migra automaticamente cualquier `.skills/`
# de antes, para no perder skills locales ya guardadas.
SKILLS_SUBDIR = "skills"
_LEGACY_SKILLS_SUBDIRS = (".skills",)
SESSIONS_SUBDIR = "sessions"
DECISIONS_SUBDIR = "decisions"
COMMANDS_SUBDIR = "commands"
# REQ-004 (spec controles-seguridad-optimizacion-lightspeed-bigquery): CSV
# completos exportados cuando un resultado de conector supera el umbral de
# filas configurado -- se crea bajo demanda, igual criterio que el resto de
# PROJECT_SUBDIRS (ver ensure_project_structure: no se crea de entrada, solo
# cuando la tool que la usa va a escribir ahi por primera vez).
EXPORTS_SUBDIR = "exports"
PROJECT_SUBDIRS = (
    SKILLS_SUBDIR,
    SESSIONS_SUBDIR,
    DECISIONS_SUBDIR,
    COMMANDS_SUBDIR,
    EXPORTS_SUBDIR,
)


def current_windows_user() -> str:
    """spec proyecto-por-sesion REQ-005: usuario de Windows del proceso
    actual, usado para filtrar el listado de sesiones en carpetas
    compartidas. Sin sufijo de timestamp (a diferencia de
    db.windows_user_tag(), que identifica el momento de CREACION de una
    sesion puntual) -- esto identifica a QUIEN esta consultando ahora."""
    import os

    return os.environ.get("USERNAME") or os.environ.get("USER") or "desconocido"


def _migrate_legacy_skills_dirs(root: Path) -> None:
    """Migracion automatica, una sola vez por carpeta: si esta carpeta de
    proyecto tiene la subcarpeta de skills de una convencion ANTERIOR
        (`.skills/`, con punto) y todavia NO tiene la actual `skills/`, renombra
        la que exista a `skills/` -- las skills locales que el usuario ya guardo
        (en `.skills/<nombre>/SKILL.md`) quedan intactas en la ubicacion nueva,
        en vez de perderse silenciosamente.

        Si por algun motivo ya existe `skills/` nueva ademas de la vieja, no se
        pisa nada -- se deja la vieja tal cual, sin fusionar automaticamente,
        para no arriesgar un merge no pedido; el usuario resuelve a mano."""
    new_dir = root / SKILLS_SUBDIR
    if new_dir.exists():
        return
    for old_name in _LEGACY_SKILLS_SUBDIRS:
        legacy_dir = root / old_name
        if legacy_dir.is_dir():
            try:
                legacy_dir.rename(new_dir)
                logger.info(
                    "Migrada carpeta de skills locales: '%s' -> '%s'",
                    legacy_dir,
                    new_dir,
                )
            except OSError:
                logger.warning(
                    "No se pudo migrar '%s' a '%s' automaticamente",
                    legacy_dir,
                    new_dir,
                    exc_info=True,
                )
            break


def ensure_project_structure(project_path: str) -> None:
    """Registra la carpeta como proyecto de una sesion: corre la migracion
    de skills locales legadas (`.skills/` -> `skills/`). NO crea ninguna
    de las subcarpetas fijas al asignar la carpeta -- pedido explicito del
    usuario (2026-09-09): a veces quiere dar contexto de una carpeta de
    archivos sin ejecutar comandos ni guardar nada, y crear las 4 de
    entrada llenaria su carpeta de basura innecesaria. Cada subcarpeta se
    crea al vuelo cuando el COMANDO que la usa va a escribir dentro:
    `transcript.export_session_to_file` crea `sessions/`, el tool
    record_decision crea `decisions/`, save_custom_command crea
    `commands/`, y skills/ solo existe si algo escribe ahi. `session/`
    (SQLite) la crea `db.py` bajo demanda, y `AGENTS.md` el comando
    `/init`."""
    root = Path(project_path)
    _migrate_legacy_skills_dirs(root)


def _db_path_for_session(session_id: str) -> Path | None:
    """Resuelve la carpeta SQLite de la carpeta de proyecto PROPIA de esta
    sesion (indice maestro `master_index.py`, `%USERPROFILE%\\.vision\\
    vision.db`) -- sin ningun fallback global. Sesiones sin carpeta
    asignada devuelven None (100% en memoria, ver docstring del modulo)."""
    own_path = master_index.get_project_path(session_id)
    if own_path:
        return db.db_path_for(own_path)
    return None


def project_root_for_session(session_id: str) -> Path | None:
    """Raiz de la carpeta de proyecto de esta sesion (NO la ruta al
    vision.db, que vive un nivel mas adentro en `session/`) -- usado por
    record_decision/commands/export/skill locales para ubicar sus propias
    subcarpetas fijas. `None` si la sesion no tiene carpeta asignada."""
    own_path = master_index.get_project_path(session_id)
    return Path(own_path) if own_path else None


def resolve_project_path_for_display(session_id: str) -> str:
    """Ruta a MOSTRAR en el chip del command-bar -- la carpeta propia de
    esta sesion, o la raiz del propio proyecto Vision como referencia
    visual si todavia no tiene ninguna (nunca implica que ahi se este
    persistiendo)."""
    own_path = master_index.get_project_path(session_id)
    if own_path:
        return own_path
    from app.config import BASE_DIR

    return str(BASE_DIR)


def assign_project_path(session_id: str, project_path: str, title: str = "") -> None:
    """Asigna (primera vez) o cambia (despues) la carpeta de proyecto de
    UNA sesion especifica -- actualiza el indice maestro. Cambiar la carpeta
    despues NO mueve/copia datos entre carpetas distintas: la sesion sigue
    viendo su historial viejo en la carpeta anterior hasta que se le pida
    algo nuevo, que ya persistira en la carpeta nueva (mismo criterio que
    la spec proyecto-por-sesion original, seccion de reglas de negocio).
    No crea subcarpetas fijas aca -- se crean al vuelo en cada comando que
    escribe (pedido del usuario 2026-09-09, ver ensure_project_structure)."""
    if not Path(project_path).is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {project_path}")
    ensure_project_structure(project_path)
    # Pasar current_windows_user() asegura que el owner quede registrado en el indice
    # maestro para el control BOLA/IDOR (evitando que el fallback de 403 deje la sesion huerfana)
    master_index.set_project_path(
        session_id,
        project_path,
        title=title,
        windows_user_created=current_windows_user(),
    )
    # Bug real reportado por el usuario 2026-09-16: el nombre que el
    # usuario escribe A MANO en el modal bloqueante de "Nueva sesión" solo
    # quedaba en el indice maestro -- la fila `session` de ESTE
    # `vision.db` (la que de verdad consulta GET /api/sessions/all, ver
    # list_all_sessions_for_current_user: usa a proposito la copia local,
    # "no la potencialmente vieja del indice maestro") ni existia todavia.
    # Con el primer mensaje real, `ensure_session` la creaba con el
    # placeholder "Nueva sesion - <iso>" y `maybe_autoname_session` la
    # pisaba enseguida con los primeros 40 caracteres del mensaje -- el
    # nombre elegido a mano se perdia sin que el usuario lo tocara. Se
    # siembra aca mismo, marcado `title_manual=1` (mismo mecanismo que un
    # renombre manual, via db.rename_session) para que ningun criterio
    # automatico lo vuelva a pisar despues.
    title = title.strip()
    if title:
        db_path = db.db_path_for(project_path)
        now = time.time()
        submit(lambda: db.ensure_session(db_path, session_id, now))
        submit(lambda: db.rename_session(db_path, session_id, title, now))


def is_persistence_available(session_id: str | None = None) -> bool:
    """REQ-006 (persistencia-sqlite-sesiones): usado por el endpoint de
    status para que el frontend sepa si debe mostrar el banner de 'sin
    carpeta de proyecto'. Sin `session_id`, siempre False (ya no existe
    una carpeta global de la cual depender)."""
    if not session_id:
        return False
    return _db_path_for_session(session_id) is not None


def get_active_db_path(session_id: str | None = None) -> Path | None:
    """Alias publico para modulos hermanos (transcript.py, purge.py) que
    necesitan la ruta cruda. Sin `session_id`, siempre None."""
    if not session_id:
        return None
    return _db_path_for_session(session_id)


# ---------------------------------------------------------------------------
# Escritura (REQ-002 persistencia-sqlite-sesiones): cada funcion resuelve
# la ruta de ESA sesion en el momento de encolar el trabajo (no cuando se
# ejecuta, ya que para entonces el usuario pudo haber reasignado la
# carpeta) y no hace nada si esa sesion no tiene ninguna carpeta resuelta.
# ---------------------------------------------------------------------------
def ensure_session(session_id: str, model: str | None = None) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.ensure_session(db_path, session_id, now, model))


def maybe_autoname_session(session_id: str, title: str) -> None:
    """REQ-006 (spec gestion-sesiones): equivalente persistido del criterio
    "primeras palabras del primer mensaje" que hoy vive solo en el
    frontend (touchCurrentSessionInIndex) -- se llama con el mismo texto
    que arma el frontend, pero SOLO pisa `session.title` si todavia NO fue
    renombrada a mano (title_manual=0, chequeado atomicamente en el UPDATE
    de db.autoname_session). Es un no-op si no hay persistencia disponible."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.autoname_session(db_path, session_id, title, now))


def record_message_start(session_id: str, message_id: str, role: str) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.insert_message(db_path, message_id, session_id, role, now))


def record_part_start(
    session_id: str, message_id: str, part_id: str, part_type: str, data: dict[str, Any]
) -> None:
    """REQ-002: insert al INICIAR un part (bloque de texto o tool-call) --
    si el backend crashea antes de que termine, ya queda un registro
    parcial en disco."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(
        lambda: db.insert_part(
            db_path, part_id, message_id, session_id, part_type, data, now
        )
    )


def record_part_end(session_id: str, part_id: str, data: dict[str, Any]) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.update_part(db_path, part_id, data, now))


def record_todos(session_id: str, todos: list[dict[str, Any]]) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    submit(lambda: db.replace_todos(db_path, session_id, todos))


def delete_message(session_id: str, message_id: str) -> None:
    """Borra una fila `message` por su ID INTERNO de SQLite (uuid propio de
    la fila) -- uso directo cuando ya se conoce ese id. Para eliminar-
    respuestas, ver `delete_assistant_response()` mas abajo: esa recibe la
    posicion del turno (0-indexed por cantidad de mensajes 'user' previos),
    no el uuid interno, porque el frontend/main.py identifican una
    respuesta por el turno que la origino, no por el id de fila de SQLite."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    submit(lambda: db.delete_message(db_path, message_id))


def find_user_occurrence(session_id: str, message_id: str) -> int | None:
    """Posicion (0-indexed entre mensajes 'user') del user message
    identificado por `message_id`, leida del SQLite. Usado por
    delete_response_endpoint como fallback en sesiones reconstruidas donde
    `session.turns` en memoria esta vacio."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return None
    return db.find_user_occurrence(db_path, session_id, message_id)


def clear_messages(session_id: str) -> None:
    """Limpia TODO el historial (preguntas y respuestas) de una sesion en
    SQLite -- usado por /clear / "limpiar conversacion". Solo borra las
    filas `message` (cascada a parts); no toca la sesion ni su carpeta."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    submit(lambda: db.clear_session_messages(db_path, session_id))


def delete_assistant_response(session_id: str, user_occurrence: int) -> None:
    """eliminar-respuestas REQ-005: borra en SQLite el mensaje 'assistant'
    (y sus 'part' via cascada) que corresponde al turno identificado por
    `user_occurrence` -- cuantos mensajes 'user' de esa sesion lo preceden
    (0-indexed), MISMO criterio de conteo que usa app/main.py del lado de
    `session.messages` para que ambas capas queden consistentes (REQ-005)
    sin depender de compartir un id de fila."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    submit(lambda: db.delete_assistant_response(db_path, session_id, user_occurrence))


def rename_session(session_id: str, title: str) -> None:
    """REQ-001/006 (spec gestion-sesiones): renombrado MANUAL -- marca
    title_manual=1 (ver db.rename_session), asi ningun criterio automatico
    lo vuelve a pisar despues. Tambien espeja el titulo en el indice
    maestro (para que el listado de sesiones muestre el nombre correcto
    sin tener que abrir cada vision.db)."""
    db_path = _db_path_for_session(session_id)
    if db_path is not None:
        now = time.time()
        submit(lambda: db.rename_session(db_path, session_id, title, now))
    submit(lambda: master_index.update_title(session_id, title))


def delete_session(session_id: str) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is not None:
        submit(lambda: db.delete_session(db_path, session_id))
    submit(lambda: master_index.delete_session(session_id))


# ---------------------------------------------------------------------------
# Lectura (REQ-003/REQ-004 persistencia-sqlite-sesiones): sincronas, se
# llaman fuera del hot path del streaming.
# ---------------------------------------------------------------------------
def list_persisted_sessions(
    session_id_for_path: str | None = None,
) -> list[dict[str, Any]]:
    """REQ-004 (persistencia-sqlite-sesiones)/REQ-005 (proyecto-por-sesion):
    lista las sesiones de la carpeta PROPIA de `session_id_for_path`,
    filtradas por el usuario de Windows ACTUAL -- `[]` si esa sesion no
    tiene carpeta asignada o no se paso ningun session_id (ya no hay
    carpeta global de la cual caer). Listar cross-carpeta (todas las
    carpetas que el usuario alguna vez uso) queda fuera de alcance, ver
    Dashboard de viabilidad."""
    if not session_id_for_path:
        return []
    db_path = _db_path_for_session(session_id_for_path)
    if db_path is None:
        return []
    return db.list_sessions(db_path, current_user=current_windows_user())


def list_all_sessions_for_current_user() -> list[dict[str, Any]]:
    """Bug real reportado por el usuario: abrir Vision en OTRO navegador
    (misma maquina) mostraba el sidebar vacio, aunque las sesiones YA
    estaban persistidas en SQLite (`list_persisted_sessions()` de arriba
    exige un `session_id_for_path` con carpeta YA conocida para saber que
    `vision.db` abrir -- un navegador nuevo arranca con un `session_id`
    recien generado, sin ninguna carpeta asignada, asi que no alcanza).

    Esta funcion resuelve el caso "no se todavia en que carpeta esta" --
    cruza TODAS las entradas del indice maestro (`master_index.
    list_all_for_user()`, ya creadas para sesiones anteriores de este
    mismo usuario de Windows, sin importar en que carpeta de proyecto
    quedaron) y abre cada `vision.db` propio para traer su metadata real
    (titulo/actividad/cantidad de mensajes vigentes, no la copia
    potencialmente vieja que guarda el indice maestro). Usada por el
    frontend como fuente de verdad para el listado del sidebar -- ver
    GET /api/sessions/all y renderSessionsPanel() en app.js, que la
    fusiona con el indice liviano de localStorage (cache, no fuente de
    verdad) en vez de reemplazarlo del todo."""
    entries = master_index.list_all_for_user(current_windows_user())
    # Agrupa por carpeta antes de consultar: varias sesiones del indice
    # maestro suelen compartir la misma carpeta de proyecto, y cada
    # `vision.db` solo hace falta abrirlo una vez (list_sessions ya trae
    # TODAS las sesiones de esa carpeta con su message_count real, en una
    # sola query -- mas barato que una query nueva por sesion individual).
    by_path: dict[str, list[str]] = {}
    for entry in entries:
        project_path = entry.get("project_path") or ""
        if project_path:
            by_path.setdefault(project_path, []).append(entry["session_id"])

    result: list[dict[str, Any]] = []
    for project_path, session_ids in by_path.items():
        db_path = db.db_path_for(project_path)
        sessions_in_folder = {s["id"]: s for s in db.list_sessions(db_path)}
        for session_id in session_ids:
            meta = sessions_in_folder.get(session_id)
            if meta is None:
                # La entrada del indice maestro sigue existiendo pero el
                # vision.db de esa carpeta no tiene (o ya no tiene) esta
                # sesion puntual -- ej. se borro a mano. Se cae al espejo
                # liviano del indice maestro en vez de omitirla del todo.
                entry = next(e for e in entries if e["session_id"] == session_id)
                meta = {
                    "id": session_id,
                    "title": entry.get("title") or session_id[:8],
                    "model": None,
                    "created_at": entry.get("created_at", 0),
                    "updated_at": entry.get("updated_at", 0),
                    "message_count": 0,
                }
            result.append({**meta, "project_path": project_path})

    result.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return result


def session_exists_in_db(session_id: str) -> bool:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return False
    return db.session_exists(db_path, session_id)


def load_session_messages(session_id: str) -> list[dict[str, Any]]:
    """REQ-003: reconstruye el historial (formato OpenAI) desde SQLite."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return []
    return db.load_messages(db_path, session_id)


def load_session_messages_for_display(session_id: str) -> list[dict[str, Any]]:
    """Version 'para pantalla' de la de arriba (ver docstring de
    db.load_messages_for_display) -- usada por GET /api/sessions/{id}/
    messages para que el frontend pueda recuperar el historial real de
    una sesion existente aunque su localStorage este vacio (navegador
    nuevo, cache borrada, etc)."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return []
    return db.load_messages_for_display(db_path, session_id)


def load_session_todos(session_id: str) -> list[dict[str, Any]]:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return []
    return db.load_todos(db_path, session_id)


def find_tool_result(
    session_id: str, call_id: str | None = None, tool_names: list[str] | None = None
) -> dict[str, Any] | None:
    """spec dashboard-graficos REQ-002: `None` si no hay persistencia
    disponible -- el caller (tool render_chart) debe manejar ese caso
    cayendo al resultado truncado del SSE (mismo criterio 'mejor esfuerzo'
    del resto del proyecto sin carpeta de proyecto elegida)."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return None
    return db.find_tool_result(db_path, session_id, call_id, tool_names)


def get_session_meta(session_id: str) -> dict[str, Any] | None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return None
    return db.get_session_meta(db_path, session_id)


def search_sessions(
    term: str, session_id_for_path: str | None = None
) -> list[dict[str, Any]]:
    """REQ-002 (spec busqueda-mensajes): [] si no hay persistencia
    disponible -- el frontend cae al fallback de localStorage en ese caso
    (REQ-006 de esa spec), no es un error. Filtrada por usuario de Windows
    actual (spec proyecto-por-sesion REQ-005)."""
    if not session_id_for_path:
        return []
    db_path = _db_path_for_session(session_id_for_path)
    if db_path is None:
        return []
    return db.search_sessions(db_path, term, current_user=current_windows_user())


def update_message_response_type(
    session_id: str, message_id: str, response_type: str
) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(
        lambda: db.update_message_response_type(
            db_path, message_id, session_id, response_type, now
        )
    )
