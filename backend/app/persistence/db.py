"""
Esquema SQLite y CRUD de bajo nivel para la persistencia de sesiones
(spec persistencia-sqlite-sesiones). Adaptado del esquema real de OpenCode
(`packages/core/src/session/sql.ts`: tablas `session`/`message`/`part`/
`todo`) pero reescrito como SQL crudo con `sqlite3` de la stdlib -- sin
Drizzle, sin Effect.

REQ-001: 4 tablas normalizadas (no un blob unico de `SessionState`), para
que operaciones puntuales (renombrar sesion, borrar UN mensaje) sean un
UPDATE/DELETE de fila en vez de deserializar/mutar/reserializar un JSON
gigante -- esto es lo que hace viables las specs C/H/I que dependen de
esta.

REQ-005: la base vive DENTRO de la carpeta de proyecto PROPIA de cada
sesion (arquitectura carpeta-por-sesion, ver app/persistence/
master_index.py), NO en una ruta fija del repo de Vision, y sin ninguna
columna de usuario/maquina -- es intencionalmente portable: si esa
carpeta se copia o se comparte (ej. OneDrive, share de red), las sesiones
viajan con ella.

Todas las funciones de este modulo son SINCRONAS y abren su propia
conexion de corta duracion por llamada (SQLite es barato de abrir/cerrar,
y evita compartir una conexion entre threads). El llamador es responsable
de correrlas fuera del event loop -- ver app/persistence/writer.py, que
las ejecuta desde un unico hilo dedicado para garantizar el ORDEN de
escrituras relacionadas por FK (ej. insertar un 'message' antes que sus
'part' hijos)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.persistence.sqlite_wal import ensure_wal

logger = logging.getLogger(__name__)

# Bug real reportado por el usuario (2026-09-10): un turno de chat con
# historial largo empezaba a fallar SIEMPRE con
# `openai.APIError: messages.N.content.0.tool_use.id: String should match
# pattern '^[a-zA-Z0-9_-]+$'` -- causa raiz encontrada en la sesion
# afectada: 37 `call_id` persistidos con formato
# `call_<timestamp>_0__thought__<firma-base64-de-extended-thinking>`
# (contienen '+', '/', '='), generados en algun momento por un modelo con
# "extended thinking" habilitado. A diferencia del caso ya cubierto mas
# abajo (tool-call incompleta: sin name o sin result), estas SI tenian
# ambos campos completos -- lo que las hacia invalidas no era estar a
# medio terminar, sino el formato del id en si, que la API del proveedor de IA
# rechaza sin importar que tan completo este el resto. Mismo criterio que
# el filtro de incompletas: se DESCARTAN al reconstruir (nunca se
# "arreglan" reescribiendo el id, que romperia la correlacion con
# cualquier tool-result que ya lo referencie en otra parte del historial).
_VALID_TOOL_CALL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# BUG real corregido (QA-005, deteccion QA 2026-09-21): el filtro por usuario
# de Windows armaba el patron como f"{usuario}_%" y lo pasaba a un LIKE SIN
# `ESCAPE`. En SQL, `_` es un comodin de UN caracter y `%` de cualquier
# cantidad -- asi que el usuario "an" matcheaba "ana_20260101...", y veia las
# sesiones de "ana". Eso rompe el aislamiento por usuario que AGENTS.md
# declara como decision de diseno ("en una carpeta compartida, cada usuario ve
# solo lo suyo"). Ahora los metacaracteres del NOMBRE se escapan y el `_`
# separador se deja literal (`\_`), con `ESCAPE '\'` en las dos queries que
# filtran por usuario (list_sessions y search_sessions).
_LIKE_ESCAPE_MAP = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})


def _user_prefix_like(user: str) -> str:
    """Patron LIKE que matchea EXACTAMENTE las sesiones creadas por `user`
    (`windows_user_created` es "usuario_yyyymmddhhmmss", ver
    `windows_user_tag`). Pensado para usarse siempre con `ESCAPE '\\'`."""
    return user.translate(_LIKE_ESCAPE_MAP) + r"\_%"

SESSION_SUBDIR = "db"  # spec carpeta-por-sesion REQ-009: antes "session" -- renombrado
# a "db" (pedido explicito del usuario, sin relacion con ningun bug: mas
# claro que ahi vive la base de datos, no "la sesion" como concepto
# amplio, que incluye tambien skills/decisions/commands/sessions).
DB_FILENAME = "vision.db"

# REQ-001: esquema normalizado adaptado de packages/core/src/session/sql.ts.
# `data` en `part` es un blob JSON (mismo criterio que MessageTable/PartTable
# de OpenCode: el contenido variable vive en JSON, las columnas indexadas
# son solo lo necesario para ordenar/filtrar).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT,
    -- REQ-006 (spec gestion-sesiones): 1 si el titulo fue puesto a mano
    -- por el usuario -- en ese caso, ningun criterio automatico (ni el
    -- placeholder inicial, ni "primeras palabras del primer mensaje" que
    -- vive en el frontend) debe volver a pisarlo jamas.
    title_manual INTEGER NOT NULL DEFAULT 0,
    -- REQ-004/005 (spec proyecto-por-sesion): "usuario_yyyymmddhhmmss" de
    -- quien creo la sesion -- usado para filtrar el listado cuando varios
    -- usuarios de Windows comparten la misma carpeta de proyecto (REQ-005,
    -- reemplaza la portabilidad SIN aislamiento que definia originalmente
    -- persistencia-sqlite-sesiones REQ-005).
windows_user_created TEXT NOT NULL DEFAULT '',
    -- spec sesion-publica-privada: 1 = sesion PUBLICA (sin dueño: otra persona que abra la carpeta puede importar una COPIA); 0 = privada,
    -- el valor por defecto de toda sesion nueva y de toda existente.
    is_public INTEGER NOT NULL DEFAULT 0,
    -- spec comando-update-sesiones-publicas: usuario de Windows de quien publico la sesion (SOLO una etiqueta para que `/update` diga de quien viene
    -- cada novedad; NO da ningun permiso, eso lo decide `is_public`). Vacio si es privada o si se publico antes de esta version.
    published_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- spec comando-update-sesiones-publicas REQ-003: hasta que mensaje de cada conversacion publica ajena ya se resumio en la sesion `session_id`.
-- La clave es el par (created_at, id) del ultimo mensaje visto: asi dos mensajes con la misma hora no se pierden ni se repiten.
CREATE TABLE IF NOT EXISTS public_follow (
    session_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    last_seen_created_at REAL NOT NULL,
    last_seen_message_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, source_id)
);

CREATE TABLE IF NOT EXISTS message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    response_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_session
    ON message(session_id, created_at, id);

CREATE TABLE IF NOT EXISTS part (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_part_message ON part(message_id, seq);
CREATE INDEX IF NOT EXISTS idx_part_session ON part(session_id);

CREATE TABLE IF NOT EXISTS todo (
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    subtasks_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (session_id, position)
);
"""

# REQ-004: mismo patron que OpenCode (`isDefaultTitle` en session.ts) para
# saber si un titulo sigue siendo el placeholder y puede reemplazarse por
# uno mas descriptivo -- acá solo se usa para el placeholder inicial, la
# generacion automatica via LLM queda fuera de alcance de esta spec.
DEFAULT_TITLE_PREFIX = "Nueva sesión - "


def windows_user_tag() -> str:
    """spec proyecto-por-sesion REQ-004: "usuario_yyyymmddhhmmss" de quien
    crea la sesion AHORA MISMO -- se calcula una sola vez, al momento de
    la primera persistencia (ensure_session), nunca se recalcula despues
    (identifica al CREADOR, no a quien la usa despues)."""
    import os
    from datetime import datetime

    user = os.environ.get("USERNAME") or os.environ.get("USER") or "desconocido"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")  # noqa: DTZ005 -- hora local de la maquina, no UTC
    return f"{user}_{stamp}"


def db_path_for(local_path: str) -> Path:
    """REQ-005: ruta de la base DENTRO de la carpeta de proyecto activa,
    nunca una ruta fija del repo de Vision."""
    return Path(local_path) / SESSION_SUBDIR / DB_FILENAME


def _migrate(conn: sqlite3.Connection) -> None:
    if "response_type" not in [row[1] for row in conn.execute("PRAGMA table_info(message)")]:
        conn.execute("ALTER TABLE message ADD COLUMN response_type TEXT")
    """Migraciones defensivas para bases creadas ANTES de agregar una
    columna nueva -- `CREATE TABLE IF NOT EXISTS` no altera tablas ya
    existentes, asi que una `vision.db` de una version anterior de Vision
    no tendria `title_manual` sin esto (mismo criterio defensivo que pide
    el AGENTS.md del proyecto para cambios de esquema futuros)."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(session)")]
    if "title_manual" not in cols:
        conn.execute(
            "ALTER TABLE session ADD COLUMN title_manual INTEGER NOT NULL DEFAULT 0"
        )
    if "windows_user_created" not in cols:
        conn.execute(
            "ALTER TABLE session ADD COLUMN windows_user_created TEXT NOT NULL DEFAULT ''"
        )
    # spec sesion-publica-privada REQ-001: una base de antes de esta feature no tiene `is_public`; todo lo existente queda PRIVADO.
    if "is_public" not in cols:
        conn.execute("ALTER TABLE session ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
    # spec comando-update-sesiones-publicas REQ-001: una base de antes no tiene `published_by`; las publicas viejas quedan sin etiqueta.
    if "published_by" not in cols:
        conn.execute("ALTER TABLE session ADD COLUMN published_by TEXT NOT NULL DEFAULT ''")
    # Sistema de sub-tareas (2 niveles, pedido explicito del usuario): una
    # `vision.db` de antes de esta feature no tiene `subtasks_json` en la
    # tabla `todo` -- mismo criterio defensivo de arriba.
    todo_cols = [row[1] for row in conn.execute("PRAGMA table_info(todo)")]
    if "subtasks_json" not in todo_cols:
        conn.execute(
            "ALTER TABLE todo ADD COLUMN subtasks_json TEXT NOT NULL DEFAULT '[]'"
        )


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Abre (creando la carpeta/archivo si hace falta) una conexion con el
    mismo setup basico de PRAGMAs que usa OpenCode
    (packages/core/src/database/database.ts:22-37), adaptado a lo que
    aplica en SQLite crudo de Python."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        ensure_wal(conn)  # QA-009: sin competir por lock exclusivo, ver sqlite_wal.py
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sesion
# ---------------------------------------------------------------------------
def ensure_session(
    db_path: Path, session_id: str, now: float, model: str | None = None
) -> None:
    """Crea la fila de sesion si no existe todavia (titulo placeholder,
    ver DEFAULT_TITLE_PREFIX); si ya existe, no toca el titulo (para no
    pisar un renombre manual de la spec C)."""
    from datetime import datetime, timezone

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            title = DEFAULT_TITLE_PREFIX + datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO session (id, title, model, windows_user_created, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, title, model, windows_user_tag(), now, now),
            )
        elif model:
            conn.execute(
                "UPDATE session SET model = ?, updated_at = ? WHERE id = ?",
                (model, now, session_id),
            )


def touch_session(db_path: Path, session_id: str, now: float) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE session SET updated_at = ? WHERE id = ?", (now, session_id)
        )


def list_sessions(
    db_path: Path, current_user: str | None = None
) -> list[dict[str, Any]]:
    """REQ-004 (persistencia-sqlite-sesiones)/REQ-005 (proyecto-por-sesion):
    listado ordenado por actividad reciente, con cantidad de mensajes por
    sesion. Si `current_user` se pasa, SOLO devuelve sesiones creadas por
    ESE usuario de Windows (match por prefijo, ya que windows_user_created
    incluye el timestamp de creacion) -- reemplaza la portabilidad sin
    aislamiento que definia originalmente persistencia-sqlite-sesiones
    REQ-005: en una carpeta compartida, cada usuario ve solo lo suyo."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        query = """
            SELECT s.id, s.title, s.model, s.created_at, s.updated_at,
                   s.windows_user_created, COUNT(m.id) AS message_count
            FROM session s
            LEFT JOIN message m ON m.session_id = s.id
        """
        params: tuple[Any, ...] = ()
        if current_user:
            # QA-005: `ESCAPE` + escapado del nombre -- ver `_user_prefix_like`.
            query += " WHERE s.windows_user_created LIKE ? ESCAPE '\\'"
            params = (_user_prefix_like(current_user),)
        query += " GROUP BY s.id ORDER BY s.updated_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "model": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "windows_user_created": r[5],
                "message_count": r[6],
            }
            for r in rows
        ]


def get_session_meta(db_path: Path, session_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, model, created_at, updated_at, is_public FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "model": row[2],
            "created_at": row[3],
            "updated_at": row[4],
            "is_public": bool(row[5]),
        }


def set_public(db_path: Path, session_id: str, public: bool, user: str, now: float) -> bool:
    """spec sesion-publica-privada REQ-002: marca la sesion como PUBLICA o PRIVADA. Publica = `is_public = 1` y SIN dueño (`windows_user_created`
    vacio: es el «borrar el CSID» del pedido); privada = `is_public = 0` y el dueño vuelve a ser `user` (`usuario_aaaammddhhmmss`, igual que al
    crear una sesion). Idempotente, no toca mensajes ni partes. Devuelve False si la sesion no existe en esta base."""
    from datetime import datetime

    with _connect(db_path) as conn:
        owner = "" if public else f"{user}_{datetime.now().strftime('%Y%m%d%H%M%S')}"  # noqa: DTZ005 -- hora local, igual que windows_user_tag
        # `published_by` (spec comando-update-sesiones-publicas REQ-001): al publicar guarda quien lo hizo; al volverla privada se vacia.
        cur = conn.execute(
            "UPDATE session SET is_public = ?, windows_user_created = ?, published_by = ?, updated_at = ? WHERE id = ?",
            (1 if public else 0, owner, user if public else "", now, session_id),
        )
        return cur.rowcount > 0


def session_exists(db_path: Path, session_id: str) -> bool:
    return get_session_meta(db_path, session_id) is not None


def rename_session(db_path: Path, session_id: str, title: str, now: float) -> bool:
    """REQ-001/006 (spec gestion-sesiones): renombra y marca `title_manual`
    -- desde este punto NINGUN criterio automatico (ni el placeholder
    inicial de ensure_session, ni el 'primeras palabras del primer
    mensaje' que vive en el frontend) debe volver a pisar este nombre."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
        return cur.rowcount > 0


def autoname_session(db_path: Path, session_id: str, title: str, now: float) -> bool:
    """REQ-006 (spec gestion-sesiones): version 'automatica' de renombrar
    -- a diferencia de rename_session() (manual), esta SOLO actualiza si
    `title_manual = 0` Y el titulo actual sigue siendo el placeholder
    (`DEFAULT_TITLE_PREFIX%`), ambos chequeados atomicamente en el propio
    WHERE. Nunca marca title_manual=1 -- si el usuario despues renombra a
    mano, rename_session() si lo hace.

    BUG real corregido (deteccion QA 2026-09-16, 2 sesiones reales con
    mensajes de usuario genuinos que quedaron para siempre con el titulo
    placeholder): el llamador anterior (app/llm/loop.py) decidia SI llamar
    a esta funcion contando mensajes 'user' en `session.messages` EN
    MEMORIA (`== 0` = "es el primer turno") -- ese contador se resetea con
    cada reinicio del proceso (uvicorn --reload, o cualquier crash), asi
    que si el reinicio coincidia con el primer turno de una sesion nueva,
    esa UNICA oportunidad de autonombrar se perdia para siempre (en el
    turno 2, el contador en memoria ya no es 0, y jamas vuelve a intentarse
    -- ni con la sesion recien recuperada de disco, porque ahi el historial
    persistido ya trae >=1 mensaje 'user'). Mover la condicion "todavia sin
    nombre" al propio WHERE de SQL la vuelve IDEMPOTENTE y autorecuperable:
    ahora el llamador puede invocarla en CADA turno sin costo (no hay UPDATE
    real una vez que el titulo ya cambio), y un reinicio a mitad de camino
    ya no pierde la unica chance de nombrar la sesion."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE session SET title = ?, updated_at = ? "
            "WHERE id = ? AND title_manual = 0 AND title LIKE ? || '%'",
            (title, now, session_id, DEFAULT_TITLE_PREFIX),
        )
        return cur.rowcount > 0


def delete_session(db_path: Path, session_id: str) -> bool:
    """Borra la sesion (y por cascada sus mensajes, partes y tareas) y COMPACTA el archivo.

    spec indice-resiliente-y-borrado-seguro REQ-009: antes solo marcaba las paginas como libres. Medido en la PC del usuario: la
    base quedo vacia pero pesaba lo mismo y el texto de los mensajes seguia legible dentro del archivo. Compactar es de mejor
    esfuerzo: si falla (base ocupada por otra conexion) el borrado YA valio y solo se avisa en el log, nunca se revierte ni se lanza."""
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
        deleted = cur.rowcount > 0
    if deleted:
        try:
            compact_database(db_path)
        except Exception:
            logger.warning(
                "La sesion %s se borro pero no se pudo compactar el archivo %s", session_id, db_path, exc_info=True
            )
    return deleted


def count_session_messages_readonly(db_path: Path, session_id: str) -> tuple[int, int]:
    """spec cambio-carpeta-historial: (mensajes, adjuntos 'file') de UNA sesion, en SOLO LECTURA (`mode=ro`: no crea el esquema ni la
    carpeta). `(0, 0)` si la base no existe; una base ilegible lanza `sqlite3.Error` (el llamador decide)."""
    if not db_path.exists():
        return (0, 0)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        (messages,) = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)).fetchone()
        (files,) = conn.execute(
            "SELECT COUNT(*) FROM part WHERE session_id = ? AND type = 'file'", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    return (messages, files)


def copy_session_history(src_path: Path, dst_path: Path, session_id: str, now: float) -> int:
    """spec cambio-carpeta-historial REQ-004/005: COPIA el historial de UNA sesion (fila de sesion, mensajes, partes -incluidas las imagenes-
    y tareas) de la base `src_path` a la base `dst_path`. La de origen se abre en SOLO LECTURA: jamas se modifica ni se borra nada alli.
    Si el destino ya tiene mensajes de esa sesion no inserta nada (nunca se mezcla) y devuelve 0; si no, devuelve cuantos mensajes copio.
    Conserva ids, orden, fechas, titulo (y que fue puesto a mano), modelo y creador originales. Los adjuntos (archivos) NO se copian: sus
    mensajes si, y en la carpeta nueva el chip dira «no encontrado». Las excepciones de SQLite se propagan: el llamador las registra."""
    src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        session = src.execute(
            "SELECT id, title, model, title_manual, windows_user_created, created_at, updated_at FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return 0
        messages = src.execute(
            "SELECT id, session_id, role, created_at, updated_at, response_type FROM message WHERE session_id = ? "
            "ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        parts = src.execute(
            "SELECT id, message_id, session_id, seq, type, data, created_at, updated_at FROM part WHERE session_id = ? "
            "ORDER BY message_id, seq",
            (session_id,),
        ).fetchall()
        todos = src.execute(
            "SELECT session_id, position, content, status, priority, subtasks_json FROM todo WHERE session_id = ? ORDER BY position",
            (session_id,),
        ).fetchall()
    finally:
        src.close()
    with _connect(dst_path) as conn:
        (already,) = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)).fetchone()
        if already:
            return 0
        conn.execute(
            "INSERT OR IGNORE INTO session (id, title, model, title_manual, windows_user_created, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            session,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO message (id, session_id, role, created_at, updated_at, response_type) VALUES (?, ?, ?, ?, ?, ?)",
            messages,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO part (id, message_id, session_id, seq, type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            parts,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO todo (session_id, position, content, status, priority, subtasks_json) VALUES (?, ?, ?, ?, ?, ?)",
            todos,
        )
        return len(messages)


# spec sesion-publica-privada REQ-005: los resultados de estas herramientas son correos, chats y reuniones de OTRA persona; al importar una
# conversacion ajena no se copian. El prefijo cubre las 37 `m365_*` (Outlook, Teams, OneDrive, canales).
PRIVATE_TOOL_PREFIXES = ("m365_",)
PRIVATE_RESULT_MARK = "contenido privado omitido"


def _hide_private_result(data_json: str) -> str:
    """Si la parte es una llamada a una herramienta privada, devuelve su JSON con el `result` reemplazado por la marca; el resto, igual."""
    try:
        data = json.loads(data_json)
    except (TypeError, ValueError):
        return data_json
    if isinstance(data, dict) and str(data.get("name", "")).startswith(PRIVATE_TOOL_PREFIXES) and "result" in data:
        data["result"] = PRIVATE_RESULT_MARK
        return json.dumps(data, ensure_ascii=False)
    return data_json


def import_session_history(
    db_path: Path, source_id: str, target_id: str, owner_tag: str, now: float, hide_private_tools: bool = False
) -> int:
    """spec importar-historial-de-carpeta REQ-004/005: COPIA el historial de la sesion `source_id` a la sesion `target_id` DENTRO de la
    misma base (las dos viven en el mismo `vision.db`). Mensajes, partes (texto, herramientas, imagenes, adjuntos) y tareas, con su orden
    y fechas. Los ids de mensaje y de parte de la copia son nuevos y derivados (`<target_id>:<id original>`): no chocan con los del
    origen y la copia es idempotente. La sesion de origen NO se modifica. Devuelve cuantos mensajes copio: 0 si `target_id` ya tiene
    mensajes en esta base (nunca se mezcla) o si el origen no existe. Todo en UNA transaccion; las excepciones de SQLite se propagan.
    `owner_tag` es el `usuario_aaaammddhhmmss` con que se crea la fila de la sesion nueva si todavia no existe.
    `hide_private_tools` (spec sesion-publica-privada REQ-005): con True, el resultado de las herramientas `m365_*` NO se copia (queda la marca
    «contenido privado omitido»): se usa al importar una conversacion de OTRA persona. La copia siempre queda privada y a nombre de quien importa."""
    with _connect(db_path) as conn:
        source = conn.execute("SELECT title, model, title_manual FROM session WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            return 0
        (already,) = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (target_id,)).fetchone()
        if already:
            return 0
        conn.execute(
            "INSERT OR IGNORE INTO session (id, title, model, title_manual, windows_user_created, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target_id, source[0], source[1], source[2], owner_tag, now, now),
        )
        messages = conn.execute(
            "SELECT id, role, created_at, updated_at, response_type FROM message WHERE session_id = ? ORDER BY created_at, id",
            (source_id,),
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO message (id, session_id, role, created_at, updated_at, response_type) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"{target_id}:{m[0]}", target_id, m[1], m[2], m[3], m[4]) for m in messages],
        )
        parts = conn.execute(
            "SELECT id, message_id, seq, type, data, created_at, updated_at FROM part WHERE session_id = ? ORDER BY message_id, seq",
            (source_id,),
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO part (id, message_id, session_id, seq, type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"{target_id}:{p[0]}",
                    f"{target_id}:{p[1]}",
                    target_id,
                    p[2],
                    p[3],
                    _hide_private_result(p[4]) if hide_private_tools and p[3] == "tool" else p[4],
                    p[5],
                    p[6],
                )
                for p in parts
            ],
        )
        conn.execute(
            "INSERT OR IGNORE INTO todo (session_id, position, content, status, priority, subtasks_json) "
            "SELECT ?, position, content, status, priority, subtasks_json FROM todo WHERE session_id = ?",
            (target_id, source_id),
        )
        conn.execute("UPDATE session SET updated_at = ? WHERE id = ?", (now, target_id))
        return len(messages)


def get_follow_mark(db_path: Path, session_id: str, source_id: str) -> tuple[float, str] | None:
    """spec comando-update-sesiones-publicas REQ-003: `(created_at, message_id)` del ultimo mensaje de `source_id` que ya se resumio en `session_id`,
    o None si nunca se actualizo desde esa conversacion. Solo lectura; una base sin la tabla (de antes) devuelve None."""
    if not db_path.exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_seen_created_at, last_seen_message_id FROM public_follow WHERE session_id = ? AND source_id = ?",
                (session_id, source_id),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0], row[1]) if row else None


def set_follow_mark(db_path: Path, session_id: str, source_id: str, created_at: float, message_id: str, now: float) -> None:
    """Avanza la marca de agua. Se llama SOLO despues de guardar la nota: si algo fallo antes, la marca queda donde estaba y el proximo
    `/update` vuelve a intentar esas mismas novedades."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO public_follow (session_id, source_id, last_seen_created_at, last_seen_message_id, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, source_id) DO UPDATE SET last_seen_created_at = excluded.last_seen_created_at, "
            "last_seen_message_id = excluded.last_seen_message_id, updated_at = excluded.updated_at",
            (session_id, source_id, created_at, message_id, now),
        )


FILE_WRITING_TOOLS = {"write": "creó", "edit": "editó"}  # herramientas con una ruta EXPLICITA; `bash` no la tiene (limite documentado)


def read_news_since(db_path: Path, source_id: str, mark: tuple[float, str] | None) -> dict[str, Any]:
    """spec comando-update-sesiones-publicas REQ-004/012/014: lo NUEVO de la conversacion `source_id` posterior a `mark` (o todo si es None), en
    SOLO LECTURA. Devuelve `{texts, files, bash_count, last}`:
    - `texts`: `(role, texto)` de los mensajes `user`/`assistant`, en orden. Sin resultados de herramientas, imagenes ni adjuntos.
    - `files`: `{tool|attachment, op, path}` de las herramientas `write`/`edit` y de los adjuntos. NUNCA el contenido ni los argumentos extra.
    - `bash_count`: cuantos comandos `bash` hubo. NUNCA su texto (puede llevar contraseñas).
    - `last`: `(created_at, message_id)` del ultimo mensaje incluido, o None si no hay novedades.
    El orden y la marca usan el par `(created_at, id)`, asi dos mensajes con la misma hora no se pierden ni se repiten."""
    news: dict[str, Any] = {"texts": [], "files": [], "bash_count": 0, "last": None}
    if not db_path.exists():
        return news
    with _connect(db_path) as conn:
        if mark is None:
            rows = conn.execute(
                "SELECT id, role, created_at FROM message WHERE session_id = ? ORDER BY created_at ASC, id ASC", (source_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role, created_at FROM message WHERE session_id = ? AND (created_at > ? OR (created_at = ? AND id > ?)) "
                "ORDER BY created_at ASC, id ASC",
                (source_id, mark[0], mark[0], mark[1]),
            ).fetchall()
        for message_id, role, created_at in rows:
            news["last"] = (created_at, message_id)
            for part_type, data_json in conn.execute(
                "SELECT type, data FROM part WHERE message_id = ? ORDER BY seq ASC", (message_id,)
            ).fetchall():
                try:
                    data = json.loads(data_json)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                if part_type == "text" and role in ("user", "assistant") and str(data.get("text", "")).strip():
                    news["texts"].append((role, str(data["text"])))
                elif part_type == "tool":
                    name = str(data.get("name", ""))
                    if name == "bash":
                        news["bash_count"] += 1
                    elif name in FILE_WRITING_TOOLS:
                        try:
                            args = json.loads(data.get("arguments") or "{}")
                        except (TypeError, ValueError):
                            args = {}
                        path = args.get("path") if isinstance(args, dict) else None
                        if path:
                            news["files"].append({"tool": name, "op": FILE_WRITING_TOOLS[name], "path": str(path)})
                elif part_type == "file":
                    path = data.get("path") or data.get("name")
                    if path:
                        news["files"].append({"tool": "attachment", "op": "adjuntó", "path": str(path)})
    return news


def read_sessions_readonly(db_path: Path) -> list[dict[str, Any]]:
    """spec indice-resiliente-y-borrado-seguro: las sesiones de una base, SOLO LECTURA. A diferencia de `list_sessions`, no pasa por
    `_connect` (que crea el esquema y migra): abre con `mode=ro`, asi mirar una carpeta ajena (re-escaneo del indice, aviso de
    copias en otra carpeta) nunca escribe en ella. `[]` si la base no existe; una base ilegible lanza `sqlite3.Error` (el llamador
    decide: omitir). Cada item: id, title, updated_at, windows_user_created, message_count."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        # spec sesion-publica-privada: una base de antes de la feature (solo lectura: no se puede migrar) no tiene `is_public`; todo es privado.
        has_flag = "is_public" in [row[1] for row in conn.execute("PRAGMA table_info(session)")]
        flag = "s.is_public" if has_flag else "0"
        has_by = "published_by" in [row[1] for row in conn.execute("PRAGMA table_info(session)")]
        by = "s.published_by" if has_by else "''"
        rows = conn.execute(
            f"SELECT s.id, s.title, s.updated_at, s.windows_user_created, COUNT(m.id), {flag}, {by} "
            "FROM session s LEFT JOIN message m ON m.session_id = s.id GROUP BY s.id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "title": r[1],
            "updated_at": r[2],
            "windows_user_created": r[3] or "",
            "message_count": r[4],
            "is_public": bool(r[5]),
            "published_by": r[6] or "",
        }
        for r in rows
    ]


def sessions_older_than(db_path: Path, cutoff_ts: float) -> list[dict[str, Any]]:
    """REQ-007: candidatas a auto-purga (sin actividad hace mas de 30 dias)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM session WHERE updated_at < ?",
            (cutoff_ts,),
        ).fetchall()
        return [
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Mensajes y parts
# ---------------------------------------------------------------------------
def insert_message(
    db_path: Path, message_id: str, session_id: str, role: str, now: float
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO message (id, session_id, role, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, session_id, role, now, now),
        )
        conn.execute(
            "UPDATE session SET updated_at = ? WHERE id = ?", (now, session_id)
        )


def _next_seq(conn: sqlite3.Connection, message_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM part WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row[0]


def insert_part(
    db_path: Path,
    part_id: str,
    message_id: str,
    session_id: str,
    part_type: str,
    data: dict[str, Any],
    now: float,
) -> None:
    """REQ-002: insert AL INICIAR el part (texto o tool-call) -- si el
    proceso crashea antes de que termine, ya queda registrado (aunque sea
    con datos parciales), replicando `projector.ts` (insert on start,
    update on end)."""
    with _connect(db_path) as conn:
        seq = _next_seq(conn, message_id)
        conn.execute(
            "INSERT OR IGNORE INTO part "
            "(id, message_id, session_id, seq, type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                part_id,
                message_id,
                session_id,
                seq,
                part_type,
                json.dumps(data, ensure_ascii=False),
                now,
                now,
            ),
        )


def insert_history_if_empty(
    db_path: Path, session_id: str, rows: list[dict[str, Any]], now: float
) -> int:
    """REQ-016 (spec fix-detener-y-progreso-visible, revision; spec carpeta-por-sesion REQ-008): guarda de una vez lo que una
    sesion hablo SIN carpeta (solo estaba en la memoria del backend), al asignarle la carpeta por primera vez. Cada fila es
    `{id, role, created_at, parts: [{id, type, data}]}`, con la misma forma que escribe el turno en vivo (insert_message +
    insert_part), asi `load_messages` y `load_messages_for_display` la leen igual. Solo si la base todavia NO tiene ningun mensaje
    de esa sesion (se mira y se escribe en la misma transaccion, en el hilo escritor): nunca se mezcla con un historial que ya
    estaba. Devuelve cuantos mensajes guardo."""
    ensure_session(db_path, session_id, now)
    with _connect(db_path) as conn:
        (already,) = conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
        ).fetchone()
        if already:
            logger.info(
                "Sesion %s: su carpeta ya tenia %d mensajes; no se guarda lo hablado sin carpeta", session_id, already
            )
            return 0
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO message (id, session_id, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (row["id"], session_id, row["role"], row["created_at"], row["created_at"]),
            )
            for seq, part in enumerate(row["parts"]):
                conn.execute(
                    "INSERT OR IGNORE INTO part "
                    "(id, message_id, session_id, seq, type, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        part["id"],
                        row["id"],
                        session_id,
                        seq,
                        part["type"],
                        json.dumps(part["data"], ensure_ascii=False),
                        row["created_at"],
                        row["created_at"],
                    ),
                )
        return len(rows)


def update_part(db_path: Path, part_id: str, data: dict[str, Any], now: float) -> None:
    """REQ-002: update AL TERMINAR el part, con el contenido final
    (texto completo acumulado, o resultado de la tool)."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE part SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), now, part_id),
        )


def delete_message(db_path: Path, message_id: str) -> bool:
    """DELETE de una fila por su ID INTERNO -- cascada automatica a sus
    parts via ON DELETE CASCADE."""
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM message WHERE id = ?", (message_id,))
        return cur.rowcount > 0


def find_user_occurrence(db_path: Path, session_id: str, message_id: str) -> int | None:
    """Resuelve la posicion (0-indexed, cuantos mensajes 'user' la preceden)
    de un mensaje 'user' identificado por su `id` (el message_id del
    frontend), a partir del SQLite. Necesario para borrar respuestas en
    sesiones RECONSTRUIDAS (backend reiniciado / carga desde disco) donde
    `session.turns` en memoria esta vacio y no se puede resolver por el
    turno vivo -- la DB conserva el id real de cada mensaje."""
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM message WHERE session_id = ? AND role = 'user' "
            "ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()
    ids = [r[0] for r in rows]
    if message_id not in ids:
        return None
    return ids.index(message_id)


def user_message_snapshot(db_path: Path, session_id: str, message_id: str) -> dict[str, Any] | None:
    """editar-mensaje-tras-reinicio REQ-001/003: lo que el usuario mando en un mensaje 'user' ya cerrado,
    leido de SQLite -- texto, imagenes (sin las eliminadas) y archivos (metadata tal cual se guardo, sin los
    eliminados). Sirve para REENVIAR un mensaje editado cuando el turno ya no esta en memoria (servidor
    reiniciado). `None` si ese id no existe en la sesion. Las rutas de archivos NO se confian: quien las usa
    las revalida contra la carpeta del proyecto."""
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM message WHERE id = ? AND session_id = ? AND role = 'user'",
            (message_id, session_id),
        ).fetchone()
        if row is None:
            return None
        parts = conn.execute(
            "SELECT type, data FROM part WHERE message_id = ? ORDER BY seq ASC", (message_id,)
        ).fetchall()
    text = "".join(json.loads(d).get("text", "") for ptype, d in parts if ptype == "text")
    images = [
        u for ptype, d in parts if ptype == "image"
        for data in [json.loads(d)] if not data.get("deleted") and (u := data.get("url"))
    ]
    files = [
        data for ptype, d in parts if ptype == "file"
        for data in [json.loads(d)] if not data.get("deleted")
    ]
    return {"text": text, "images": images, "files": files}


def truncate_from_message(
    db_path: Path, session_id: str, message_id: str, until_message_id: str | None = None
) -> int:
    """editar-mensaje-tras-reinicio REQ-002/004: borra el mensaje `message_id` y TODO lo posterior de la sesion
    (cascada a sus parts), igual que ya hace el truncado en memoria. Mismo orden que el resto del modulo:
    `(created_at, id)`. Sin esto, reenviar un mensaje editado reutilizaba el mismo id y SQLite le pegaba el
    texto nuevo al viejo (se leia 'viejoNUEVO') y dejaba la respuesta vieja. Devuelve cuantos mensajes borro.

    REQ-014 c (spec fix-detener-y-progreso-visible): con `until_message_id` (el mensaje de usuario del turno que esta
    corriendo) se borra solo hasta ese mensaje, sin incluirlo. Si ese mensaje todavia no esta en la base, todo lo de ese
    turno se va a escribir DESPUES (misma cola de escritura): el truncado completo no le quita nada."""
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        target = conn.execute(
            "SELECT created_at, id FROM message WHERE id = ? AND session_id = ?",
            (message_id, session_id),
        ).fetchone()
        if target is None:
            return 0
        bound = None
        if until_message_id is not None:
            bound = conn.execute(
                "SELECT created_at, id FROM message WHERE id = ? AND session_id = ?",
                (until_message_id, session_id),
            ).fetchone()
        if bound is not None:
            if tuple(bound) <= tuple(target):
                return 0  # no deberia pasar (el turno en curso es posterior): ante la duda no se borra nada suyo
            cur = conn.execute(
                "DELETE FROM message WHERE session_id = ? AND (created_at, id) >= (?, ?) AND (created_at, id) < (?, ?)",
                (session_id, target[0], target[1], bound[0], bound[1]),
            )
            return cur.rowcount
        cur = conn.execute(
            "DELETE FROM message WHERE session_id = ? AND (created_at, id) >= (?, ?)",
            (session_id, target[0], target[1]),
        )
        return cur.rowcount


def clear_session_messages(db_path: Path, session_id: str) -> int:
    """Borra TODAS las filas `message` de una sesion (preguntas y
    respuestas), con cascada a sus parts/tool-results -- usado por el
    comando de "limpiar conversacion". Devuelve cuantas filas borro."""
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
        return cur.rowcount


def delete_assistant_response(
    db_path: Path, session_id: str, user_occurrence: int
) -> bool:
    """eliminar-respuestas REQ-003/004/005: borra el mensaje 'assistant'
    que corresponde al turno identificado por `user_occurrence` (0-indexed,
    cuantos mensajes 'user' de esa sesion lo preceden) -- borrado
    QUIRURGICO: solo ESE mensaje assistant (y sus parts/tool-results via
    cascada), sin tocar mensajes anteriores ni posteriores. Distinto del
    truncado de REQ-009 (parada-y-cola-mensajes), que borra todo lo que
    sigue."""
    if not db_path.exists():
        return False
    with _connect(db_path) as conn:
        # Localiza el ID del mensaje 'user' en la posicion `user_occurrence`
        # (ordenado igual que session.messages: por created_at ASC).
        user_rows = conn.execute(
            "SELECT id, created_at FROM message WHERE session_id = ? AND role = 'user' "
            "ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()
        if user_occurrence >= len(user_rows):
            return False
        user_message_id, user_created_at = user_rows[user_occurrence]

        # BUG real corregido (QA-006, deteccion QA 2026-09-21): antes se
        # tomaba "el primer assistant posterior al user" SIN acotarlo por
        # arriba. Si ese turno no tenia respuesta (interrumpida, ya borrada,
        # o un `/stop` antes del primer chunk), el "primer assistant
        # posterior" resultaba ser el del turno SIGUIENTE -- y se borraba una
        # respuesta legitima que el usuario nunca pidio borrar (perdida de
        # datos silenciosa). Ahora la busqueda queda acotada al intervalo del
        # turno: despues de ESTE user y antes del SIGUIENTE.
        #
        # Se compara `(created_at, id)` como tupla (row values de SQLite) en
        # vez de solo `created_at`, para desempatar igual que el ORDER BY que
        # usa todo este modulo -- si dos mensajes comparten timestamp (la
        # resolucion de `time.time()` no siempre alcanza dentro de un mismo
        # turno), el orden sigue siendo el mismo que ve el usuario.
        sql = (
            "SELECT id FROM message WHERE session_id = ? AND role = 'assistant' "
            "AND (created_at, id) > (?, ?)"
        )
        params: list[Any] = [session_id, user_created_at, user_message_id]
        if user_occurrence + 1 < len(user_rows):
            next_user_id, next_user_created_at = user_rows[user_occurrence + 1]
            sql += " AND (created_at, id) < (?, ?)"
            params += [next_user_created_at, next_user_id]
        sql += " ORDER BY created_at ASC, id ASC LIMIT 1"

        assistant_row = conn.execute(sql, params).fetchone()
        if assistant_row is None:
            return False

        cur = conn.execute("DELETE FROM message WHERE id = ?", (assistant_row[0],))
        return cur.rowcount > 0


def describe_file_part(data: dict[str, Any], project_root: Path | None) -> dict[str, Any]:
    """REQ-010 a: metadata de un part 'file' + `exists` (calculado al cargar).
    Una ruta relativa se resuelve contra la raiz del proyecto; una absoluta
    (leida en su lugar) tal cual. `deleted` (tombstone de REQ-009) manda: un
    archivo eliminado nunca figura como 'no encontrado'."""
    path = data.get("path") or ""
    deleted = bool(data.get("deleted"))
    exists = False
    if path and not deleted:
        p = Path(path)
        if not p.is_absolute() and project_root is not None:
            p = project_root / p
        try:
            exists = p.is_file()
        except OSError:
            exists = False
    return {
        "name": data.get("name", ""),
        "path": path,
        "size": data.get("size"),
        "mime": data.get("mime", ""),
        "kind": data.get("kind", "file"),
        "inplace": bool(data.get("inplace")),
        "exists": exists,
        "deleted": deleted,
        "token_estimate": data.get("token_estimate"),
    }


def load_messages_for_display(
    db_path: Path, session_id: str, project_root: Path | None = None
) -> list[dict[str, Any]]:
    """Bug real reportado por el usuario: abrir Vision en OTRO navegador
    mostraba la sesion vacia de mensajes aunque ya estuvieran persistidos
    aca -- el frontend nunca llamaba a ninguna funcion de lectura de este
    modulo, solo escribia (ver comentario en service.py:
    list_all_sessions_for_current_user). `load_messages()` (arriba) sirve
    para reconstruir el prompt que se manda al proveedor de IA (formato OpenAI con
    tool_calls/tool results intercalados) -- NO es lo que necesita pintar
    el frontend, que solo quiere `{role, text, messageId, time}` por
    burbuja de chat (mismo shape que ya arma `chatHistory` en app.js).
    Se omiten los parts 'tool'/'image' de esta vista (el frontend hoy no
    reconstruye imagenes/tool-badges desde un historial recargado, solo
    texto plano) -- alcance minimo para resolver el bug reportado."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        messages = conn.execute(
            "SELECT id, role, created_at, response_type FROM message WHERE session_id = ? "
            "AND role IN ('user', 'assistant') ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for msg_id, role, created_at, response_type in messages:
            parts = conn.execute(
                "SELECT type, data FROM part WHERE message_id = ? ORDER BY seq ASC",
                (msg_id,),
            ).fetchall()
            text = "".join(
                json.loads(data).get("text", "")
                for ptype, data in parts
                if ptype == "text"
            )
            # REQ-010 a (spec adjuntar-archivos-chat): los parts 'file' se
            # devuelven como `attachments`, con `exists` calculado AHORA.
            attachments = [
                describe_file_part(json.loads(data), project_root)
                for ptype, data in parts
                if ptype == "file"
            ]
            if not text and not attachments:
                # Mensaje 'assistant' que era solo tool-calls, sin texto
                # propio (ej. un turno que solo ejecuto una tool y no
                # redacto respuesta) -- no hay nada que mostrar como
                # burbuja de chat. Un mensaje que SOLO tiene adjuntos (texto
                # vacio) si se muestra: hoy se descartaba (REQ-010 a).
                continue
            entry = {
                "role": role,
                "text": text,
                "messageId": msg_id,
                "time": created_at
                * 1000,  # created_at es epoch segundos; el frontend usa epoch ms (Date.now())
            }
            if attachments:
                entry["attachments"] = attachments
            if response_type:
                entry["responseType"] = response_type
            result.append(entry)
        return result


def _rebuild_text_message(role: str, parts: list[tuple[str, str]]) -> dict[str, Any]:
    """Rearma un mensaje `system`/`user` desde sus parts de SQLite: texto, aviso de archivos adjuntos (se REARMA desde los parts
    'file', el texto persistido sigue limpio) e imagenes (si hay, `content` es una LISTA de bloques; las eliminadas pasan a ser
    el texto «[imagen eliminada]», nunca un image_url roto). `parts` = [(type, data_json)] en orden de `seq`."""
    text = "".join(
        json.loads(data).get("text", "")
        for ptype, data in parts
        if ptype == "text"
    )
    # spec contexto-multimodal REQ-008: si el mensaje tiene
    # parts de imagen, se reconstruye el `content` en formato
    # LISTA (igual que lo arma app/llm/loop.py al enviarlo por
    # primera vez) -- una sesion con imagenes recuperada desde
    # SQLite tras un reinicio del backend debe verlas igual
    # que antes de reiniciar, no perder las imagenes.
    image_parts = [
        json.loads(data) for ptype, data in parts if ptype == "image"
    ]
    # REQ-003 c: el aviso de rutas se REARMA desde los parts
    # 'file' (la conversacion que ve el modelo no cambia al
    # reiniciar). El texto PERSISTIDO sigue limpio.
    file_parts = [
        json.loads(data) for ptype, data in parts if ptype == "file"
    ]
    from app.attachments import with_notice  # import diferido: evita ciclo

    llm_text = with_notice(text, file_parts) if file_parts else text
    # REQ-009 c: una imagen eliminada se rearma como el TEXTO
    # "[imagen eliminada]" -- nunca queda un image_url roto.
    image_urls = [ip.get("url", "") for ip in image_parts if not ip.get("deleted")]
    deleted_images = sum(1 for ip in image_parts if ip.get("deleted"))
    if deleted_images:
        marker = " ".join(["[imagen eliminada]"] * deleted_images)
        llm_text = f"{llm_text}\n{marker}" if llm_text else marker
    if image_urls:
        content_blocks: list[dict[str, Any]] = []
        if llm_text:
            content_blocks.append({"type": "text", "text": llm_text})
        for url in image_urls:
            content_blocks.append(
                {"type": "image_url", "image_url": {"url": url}}
            )
        return {"role": role, "content": content_blocks}
    else:
        return {"role": role, "content": llm_text}
    


def _rebuild_assistant_messages(session_id: str, msg_id: str, parts: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Rearma un mensaje `assistant` y, a continuacion, un mensaje `tool` por cada tool-call completa (misma forma que el flujo
    en vivo). Las tool-calls incompletas (sin nombre, sin resultado o con id invalido) se DESCARTAN: la API rechaza una
    tool-call sin su respuesta. Un assistant que queda sin texto ni tool-calls se omite (devuelve `[]`)."""
    text_parts = [
        json.loads(data).get("text", "")
        for ptype, data in parts
        if ptype == "text"
    ]
    all_tool_parts = [
        json.loads(data) for ptype, data in parts if ptype == "tool"
    ]
    # Bug real encontrado (2026-09-05): `record_part_start` (ver
    # app/persistence/service.py) inserta el part 'tool' con
    # name="" ANTES de conocerse el nombre real de la tool-call
    # (se completa recien en record_part_end, al terminar el
    # streaming) -- si el proceso se corta entre esos dos
    # momentos (crash, kill del .exe, etc.), esa fila queda
    # CONGELADA en SQLite para siempre con name="". Al recargar
    # la sesion (reinicio del backend, SessionStore.
    # get_or_create), reconstruirla como tool_call real con
    # nombre vacio rompe el proximo request al proveedor de IA con
    # "Invalid 'input[N].name': empty string" (400) -- el
    # gateway lo detecta recien al traducir internamente al
    # formato Responses API, por eso el indice en el error no
    # coincide con la posicion en `messages`. Se descartan
    # (nunca se reconstruyen) las tool-calls incompletas: sin
    # nombre, o con nombre pero sin 'result' (mismo problema:
    # la API exige un mensaje 'tool' por cada tool_call
    # anunciada en el mismo turno).
    tool_parts = [
        tp
        for tp in all_tool_parts
        if tp.get("name")
        and "result" in tp
        and _VALID_TOOL_CALL_ID_RE.match(tp.get("call_id") or "")
    ]
    dropped = len(all_tool_parts) - len(tool_parts)
    if dropped:
        logger.warning(
            "Sesion %s: se descartaron %d tool-call(s) incompleta(s) o "
            "con call_id invalido (interrumpidas antes de terminar, o "
            "id con formato no aceptado por la API) al reconstruir el "
            "historial -- mensaje %s",
            session_id,
            dropped,
            msg_id,
        )
    content = "".join(text_parts) or None
    if content is None and not tool_parts:
        # Mensaje 'assistant' completamente vacio (solo tenia la
        # tool-call descartada arriba, sin texto) -- omitirlo
        # entero evita mandarle a la API un mensaje sin
        # content ni tool_calls, que tambien rechaza.
        return []
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if tool_parts:
        assistant_msg["tool_calls"] = [
            {
                "id": tp["call_id"],
                "type": "function",
                "function": {
                    "name": tp["name"],
                    "arguments": tp["arguments"],
                },
            }
            for tp in tool_parts
        ]
    out: list[dict[str, Any]] = [assistant_msg]
    for tp in tool_parts:
        out.append(
            {
                "role": "tool",
                "tool_call_id": tp["call_id"],
                "content": tp["result"],
            }
        )
    
    return out


def load_messages(db_path: Path, session_id: str) -> list[dict[str, Any]]:
    """REQ-003: reconstruye el historial en formato OpenAI chat.completions
    (el mismo que consume app/llm/loop.py) a partir de las tablas
    normalizadas. Un mensaje 'assistant' con tool-calls se reconstruye
    junto con sus mensajes 'tool' sinteticos inmediatamente despues (igual
    forma que el flujo en vivo los va agregando)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        messages = conn.execute(
            "SELECT id, role FROM message WHERE session_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (session_id,),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for msg_id, role in messages:
            parts = conn.execute(
                "SELECT type, data FROM part WHERE message_id = ? ORDER BY seq ASC",
                (msg_id,),
            ).fetchall()

            if role in ("system", "user"):
                result.append(_rebuild_text_message(role, parts))
                continue

            if role == "assistant":
                result.extend(_rebuild_assistant_messages(session_id, msg_id, parts))
                continue

            # role desconocido (defensivo, ver Mantenimiento en la spec):
            # se omite en vez de romper la reconstruccion completa.
            logger.warning("Rol de mensaje desconocido al reconstruir sesion: %s", role)
        return result


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------
def replace_todos(db_path: Path, session_id: str, todos: list[dict[str, Any]]) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM todo WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO todo (session_id, position, content, status, priority, subtasks_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    i,
                    t.get("content", ""),
                    t.get("status", "pending"),
                    t.get("priority", "medium"),
                    # Sub-tareas (pedido explicito del usuario, 2 niveles
                    # fijos: tarea principal + subtasks, sin un 3er nivel)
                    # se serializan como JSON en la misma fila -- mismo
                    # patron de "reemplazo completo" ya usado para la
                    # lista entera, sin complicar el schema con una tabla
                    # relacional/parent_id para un anidamiento fijo y chico.
                    json.dumps(t.get("subtasks", []) or []),
                )
                for i, t in enumerate(todos)
            ],
        )


def load_todos(db_path: Path, session_id: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT content, status, priority, subtasks_json FROM todo WHERE session_id = ? "
            "ORDER BY position ASC",
            (session_id,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                subtasks = json.loads(r[3]) if r[3] else []
            except (ValueError, TypeError):
                subtasks = []  # fila corrupta/vieja -- no rompe la carga del resto de la lista
            result.append(
                {
                    "content": r[0],
                    "status": r[1],
                    "priority": r[2],
                    "subtasks": subtasks,
                }
            )
        return result


def find_tool_result(
    db_path: Path,
    session_id: str,
    call_id: str | None,
    tool_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """spec dashboard-graficos REQ-002: busca el `result` COMPLETO (sin el
    truncado a 4000 caracteres del evento SSE, ver app/llm/loop.py) de una
    tool-call ya persistida -- resuelve la limitacion ya documentada en
    app.js ("el dashboard refleja una muestra, no el total exacto").

    Si `call_id` se pasa, busca ESA tool-call exacta. Si no (`None`), busca
    la MAS RECIENTE cuyo `name` este en `tool_names` (o cualquiera si
    `tool_names` es None) -- cubre el caso "graficame el ultimo resultado"
    sin que el LLM tenga que recordar/citar el call_id exacto."""
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        if call_id:
            rows = conn.execute(
                "SELECT data FROM part WHERE session_id = ? AND type = 'tool' "
                "ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            for (raw,) in rows:
                parsed = json.loads(raw)
                if parsed.get("call_id") == call_id:
                    return parsed
            return None

        rows = conn.execute(
            "SELECT data FROM part WHERE session_id = ? AND type = 'tool' "
            "ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        for (raw,) in rows:
            parsed = json.loads(raw)
            if tool_names is None or parsed.get("name") in tool_names:
                return parsed
        return None


def load_pending_question(db_path: Path, session_id: str) -> dict[str, Any] | None:
    """REQ-018 (spec fix-detener-y-progreso-visible): el part 'tool' de la ULTIMA pregunta (tool `question`, ya con resultado) que no
    tiene ningun mensaje del usuario despues -- la pregunta que sigue sin contestar --, para que F5 la vuelva a mostrar en su sesion. Lo
    devuelve tal cual (`arguments` crudo y `result`): si de verdad espera al usuario lo decide `tools.question.pending_question_view`
    (una que el loop contesto solo tambien queda aca). Cualquier mensaje del usuario la da por contestada, como en la pagina. Una fila
    rota se saltea. Sin base, None (y no crea nada)."""
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT m.role, p.type, p.data FROM message m LEFT JOIN part p ON p.message_id = m.id "
            "WHERE m.session_id = ? AND m.role IN ('user', 'assistant') "
            "ORDER BY m.created_at ASC, m.id ASC, p.seq ASC",
            (session_id,),
        ).fetchall()
    pending: dict[str, Any] | None = None
    for role, ptype, data in rows:
        if role == "user":
            pending = None
            continue
        if ptype != "tool" or not data:
            continue
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("name") == "question" and "result" in parsed:
            pending = parsed
    return pending


# ---------------------------------------------------------------------------
# Busqueda global (spec busqueda-mensajes)
# ---------------------------------------------------------------------------
_SNIPPET_RADIUS = 60  # caracteres de contexto a cada lado del match


def _extract_text(part_type: str, data: dict[str, Any]) -> str:
    """Texto humano-legible de un part, usado tanto para filtrar como para
    armar el snippet -- para 'tool' se concatena nombre+argumentos+
    resultado (asi una busqueda tambien encuentra, ej., un nombre de tabla
    que solo aparece en el resultado de una query)."""
    if part_type == "text":
        return data.get("text", "")
    if part_type == "tool":
        return " ".join(str(data.get(k, "")) for k in ("name", "arguments", "result"))
    if part_type == "file":
        # REQ-010 e: la busqueda de mensajes encuentra tambien por nombre de archivo.
        return str(data.get("name", ""))
    return ""


def _build_snippet(text: str, term: str) -> str:
    """REQ-002: fragmento corto de contexto alrededor de la PRIMERA
    ocurrencia del termino (case-insensitive), con '...' en los bordes
    cuando se corta texto real."""
    lower_text = text.lower()
    lower_term = term.lower()
    idx = lower_text.find(lower_term)
    if idx == -1:
        return text[: _SNIPPET_RADIUS * 2].strip()

    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(term) + _SNIPPET_RADIUS)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def search_sessions(
    db_path: Path, term: str, limit: int = 50, current_user: str | None = None
) -> list[dict[str, Any]]:
    """REQ-002/005 (spec busqueda-mensajes): busqueda case-insensitive por
    substring simple (sin regex, sin FTS5 -- ver spec §8) sobre el texto
    de TODOS los parts de TODAS las sesiones. Devuelve una fila por
    SESION (no por match individual), con un snippet de la primera
    coincidencia encontrada -- si el mismo termino aparece varias veces
    en una sesion, esta funcion no las enumera todas (eso lo resuelve
    REQ-004 en el frontend, resaltando sobre el DOM ya cargado de la
    sesion abierta).

    `current_user` (spec proyecto-por-sesion REQ-005): si se pasa, solo
    busca dentro de sesiones creadas por ESE usuario -- mismo filtrado que
    `list_sessions`."""
    if not db_path.exists() or not term.strip():
        return []

    with _connect(db_path) as conn:
        # BUGS reales corregidos (QA-003 y QA-004, deteccion QA 2026-09-21):
        # antes habia un prefiltro en SQL,
        #   WHERE LOWER(p.data) LIKE '%' || LOWER(?) || '%'
        # que descartaba filas ANTES de que el filtro real de Python las
        # viera, con dos falsos negativos graves:
        #   1. `LOWER()` y `LIKE` de SQLite solo pliegan ASCII: buscar
        #      "canción" NO encontraba "CANCIÓN" -- roto justo en español,
        #      que es el idioma de todo el contenido de este proyecto.
        #   2. El prefiltro corria sobre `p.data`, que es el JSON CRUDO: ahi
        #      las comillas van como \" y los saltos como \n, asi que un
        #      termino con comillas nunca matcheaba. Ademas `%` y `_` del
        #      termino actuaban como comodines de LIKE.
        # El filtro ahora es 100% Python sobre el texto ya decodificado
        # (`str.lower()` si es unicode-aware, y un `in` es substring literal),
        # que es la semantica que la spec pide. Para que sacar el prefiltro no
        # cueste memoria, se itera el cursor de forma perezosa (sin
        # `fetchall()`) y se corta apenas se alcanza `limit`: con el orden por
        # `updated_at DESC`, los resultados aparecen en las sesiones mas
        # recientes primero. Si algun dia el volumen lo justifica, el camino
        # documentado en la spec §8 es FTS5, no volver a un LIKE sobre JSON.
        query = """
            SELECT s.id, s.title, s.updated_at, p.type, p.data
            FROM part p
            JOIN session s ON s.id = p.session_id
        """
        params: tuple[Any, ...] = ()
        if current_user:
            # QA-005: `ESCAPE` + escapado del nombre -- ver `_user_prefix_like`.
            query += " WHERE s.windows_user_created LIKE ? ESCAPE '\\'"
            params = (_user_prefix_like(current_user),)
        query += " ORDER BY s.updated_at DESC, p.created_at ASC"

        seen_sessions: dict[str, dict[str, Any]] = {}
        for session_id, title, updated_at, part_type, raw_data in conn.execute(
            query, params
        ):
            if session_id in seen_sessions:
                continue  # ya se tiene un snippet para esta sesion (REQ-002: 1 resultado por sesion)
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
            text = _extract_text(part_type, data)
            if term.lower() not in text.lower():
                # el match estaba en una parte del JSON que _extract_text
                # no expone como texto humano (ej. un call_id) -- se
                # ignora ese part para el snippet, no es informativo.
                continue
            seen_sessions[session_id] = {
                "id": session_id,
                "title": title,
                "updated_at": updated_at,
                "snippet": _build_snippet(text, term),
            }
            if len(seen_sessions) >= limit:
                break

        return list(seen_sessions.values())

def update_message_response_type(
    db_path: Path, message_id: str, session_id: str, response_type: str, now: float
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE message SET response_type = ?, updated_at = ? WHERE id = ? AND session_id = ?",
            (response_type, now, message_id, session_id)
        )


# ---------------------------------------------------------------------------
# Adjuntos de archivo: consultas y limpieza (spec adjuntar-archivos-chat REQ-009)
# ---------------------------------------------------------------------------
def list_file_parts(db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    """Todos los parts 'file' de la base (o solo de una sesion). Cada item trae
    `part_id`, `message_id`, `session_id` y la metadata guardada."""
    if not db_path.exists():
        return []
    sql = "SELECT id, message_id, session_id, data FROM part WHERE type = 'file'"
    params: tuple[Any, ...] = ()
    if session_id is not None:
        sql += " AND session_id = ?"
        params = (session_id,)
    with _connect(db_path) as conn:
        rows = conn.execute(sql + " ORDER BY created_at ASC", params).fetchall()
    out: list[dict[str, Any]] = []
    for part_id, message_id, sid, data in rows:
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        item.update({"part_id": part_id, "message_id": message_id, "session_id": sid})
        out.append(item)
    return out


def list_image_parts(db_path: Path, session_id: str) -> list[dict[str, Any]]:
    """Parts 'image' (base64) de una sesion, con su tamaño aproximado en bytes,
    SIN devolver el base64 completo (pesa MB)."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, message_id, length(data), data FROM part WHERE type = 'image' AND session_id = ?",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for part_id, message_id, nbytes, data in rows:
        try:
            deleted = bool(json.loads(data).get("deleted"))
        except json.JSONDecodeError:
            deleted = False
        out.append({"part_id": part_id, "message_id": message_id, "bytes": nbytes, "deleted": deleted})
    return out


def tombstone_file_part(db_path: Path, part_id: str, now: float) -> None:
    """REQ-009 b: el part queda como tombstone (`deleted: true`, `deleted_at`);
    conserva nombre/ruta/tamaño para que el chip diga "archivo eliminado"."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT data FROM part WHERE id = ? AND type = 'file'", (part_id,)).fetchone()
        if row is None:
            return
        data = json.loads(row[0])
        data.update({"deleted": True, "deleted_at": now})
        conn.execute(
            "UPDATE part SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), now, part_id),
        )


def tombstone_image_part(db_path: Path, part_id: str, now: float) -> None:
    """REQ-009 c: el base64 se reemplaza por un marcador (libera el espacio);
    `load_messages` la rearma como el texto "[imagen eliminada]"."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE part SET data = ?, updated_at = ? WHERE id = ? AND type = 'image'",
            (json.dumps({"url": "", "deleted": True, "deleted_at": now}), now, part_id),
        )


def compact_database(db_path: Path) -> None:
    """REQ-009 d: `wal_checkpoint(TRUNCATE)` + `VACUUM`. Sin VACUUM, SQLite no
    achica el archivo. Se abre una conexion propia SIN el esquema/migracion de
    `_connect` porque VACUUM no puede correr dentro de una transaccion."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def database_size(db_path: Path) -> int:
    """Bytes en disco de la base + su WAL (para medir lo liberado de verdad)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path) + suffix)
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total
