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
import uuid
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
# REQ-011 (spec adjuntar-archivos-chat): archivos que el usuario adjunta desde el
# chat (Excel, CSV, PDF...). Nombre exacto en singular por decision del usuario.
# Se crea bajo demanda, recien cuando algo escribe ahi (mismo criterio que el resto).
ATTACHMENT_SUBDIR = "attachment"
PROJECT_SUBDIRS = (
    SKILLS_SUBDIR,
    SESSIONS_SUBDIR,
    DECISIONS_SUBDIR,
    COMMANDS_SUBDIR,
    EXPORTS_SUBDIR,
    ATTACHMENT_SUBDIR,
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


def _history_rows(history: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """REQ-016 (spec fix-detener-y-progreso-visible, revision): pasa lo que la sesion tiene en memoria (formato OpenAI, como
    `session.messages`) a las filas que escribe el turno en vivo: un mensaje `user` con su part de texto (y uno por imagen), y un
    mensaje `assistant` por cada respuesta del modelo con su texto y un part `tool` por herramienta (con el resultado del mensaje
    `tool` que la sigue, si llego). El system prompt nunca se guarda (se rearma en vivo, como siempre).

    Cada mensaje de usuario puede traer `message_id` (el id con que la pagina lo mando: ↻/🗑 y F5 lo buscan por ese id) y `text`
    (lo que escribio la persona, sin las instrucciones que Vision le agrega al modelo); si no, id nuevo y el texto del contenido.
    `created_at` crece de a 1 ms y termina ANTES de `now`: lo que se escriba despues (con la hora de ese momento) queda detras."""

    def new_id() -> str:
        return str(uuid.uuid4())

    rows: list[dict[str, Any]] = []
    for i, msg in enumerate(history):
        role = msg.get("role")
        content = msg.get("content")
        parts: list[dict[str, Any]] = []
        if role == "user":
            blocks = content if isinstance(content, list) else []
            text = msg.get("text")
            if text is None:
                text = (
                    content
                    if isinstance(content, str)
                    else "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                )
            parts.append({"id": new_id(), "type": "text", "data": {"text": text}})
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    url = (block.get("image_url") or {}).get("url")
                    if url:
                        parts.append({"id": new_id(), "type": "image", "data": {"url": url}})
            rows.append({"id": msg.get("message_id") or new_id(), "role": "user", "parts": parts})
        elif role == "assistant":
            if isinstance(content, str) and content:
                parts.append({"id": new_id(), "type": "text", "data": {"text": content}})
            results: dict[Any, Any] = {}
            for following in history[i + 1 :]:  # los resultados vienen justo despues, uno por herramienta
                if following.get("role") != "tool":
                    break
                results.setdefault(following.get("tool_call_id"), following.get("content"))
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                data = {"call_id": call.get("id"), "name": fn.get("name", ""), "arguments": fn.get("arguments", "")}
                if call.get("id") in results:
                    data["result"] = results[call.get("id")]
                parts.append({"id": new_id(), "type": "tool", "data": data})
            if parts:
                rows.append({"id": new_id(), "role": "assistant", "parts": parts})
    first = now - 0.001 * (len(rows) + 1)
    for n, row in enumerate(rows):
        row["created_at"] = first + 0.001 * n
    return rows


def assign_project_path(
    session_id: str,
    project_path: str,
    title: str = "",
    unsaved_history: list[dict[str, Any]] | None = None,
) -> None:
    """Asigna (primera vez) o cambia (despues) la carpeta de proyecto de
    UNA sesion especifica -- actualiza el indice maestro. Cambiar la carpeta
    despues NO mueve/copia datos entre carpetas distintas: la sesion sigue
    viendo su historial viejo en la carpeta anterior hasta que se le pida
    algo nuevo, que ya persistira en la carpeta nueva (mismo criterio que
    la spec proyecto-por-sesion original, seccion de reglas de negocio).
    No crea subcarpetas fijas aca -- se crean al vuelo en cada comando que
    escribe (pedido del usuario 2026-09-09, ver ensure_project_structure).

    REQ-016 (spec fix-detener-y-progreso-visible, revision): `unsaved_history` = lo que la sesion hablo SIN carpeta (solo en la
    memoria del backend, ver `_history_rows`). La PRIMERA vez que recibe carpeta se guarda en su vision.db, salvo que esa base ya
    tenga mensajes de la sesion. Antes no se guardaba: SQLite arrancaba con el primer mensaje posterior y, como F5 lee de SQLite y la
    pagina borra su copia cuando SQLite tiene mensajes, lo de antes desaparecia (y con el proximo reinicio, del todo). Va a la cola de
    escritura ANTES de registrar la carpeta en el indice maestro: hasta ese momento ninguna escritura de la sesion llega a esa base
    (sin carpeta no hay donde escribir), asi que todo lo nuevo -- tambien lo de un turno que este corriendo -- queda detras."""
    if not Path(project_path).is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {project_path}")
    ensure_project_structure(project_path)
    if unsaved_history and master_index.get_project_path(session_id) is None:
        rows = _history_rows(unsaved_history, time.time())
        if rows:
            history_db = db.db_path_for(project_path)
            saved_at = time.time()
            submit(lambda: db.insert_history_if_empty(history_db, session_id, rows, saved_at))
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


def _same_folder(a: str, b: str) -> bool:
    """True si `a` y `b` son la MISMA carpeta aunque se escriban distinto (otra barra, mayusculas de unidad, barra final). En Windows
    `normcase` pasa a minusculas y unifica las barras, y `normpath` quita la barra final."""
    import os

    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def master_index_has_folder(session_id: str) -> bool:
    """True si la sesion YA tiene una carpeta registrada en el indice (es decir, asignar otra es un CAMBIO y no la primera vez)."""
    return master_index.get_project_path(session_id) is not None


def folder_change_preview(session_id: str, new_path: str) -> dict[str, Any]:
    """spec cambio-carpeta-historial REQ-001/002: lo que la persona necesita saber ANTES de cambiar la carpeta de la sesion. `applicable`
    es True solo si la sesion YA tiene otra carpeta distinta, con historial, y la nueva NO tiene mensajes de esa sesion (si los tiene no se
    pregunta ni se copia: se respeta lo que hay). De mejor esfuerzo y SOLO LECTURA: nunca escribe ni crea la carpeta nueva, y nunca lanza
    (si algo no se puede leer, `applicable` es False y el cambio de carpeta sigue como siempre)."""
    info: dict[str, Any] = {
        "applicable": False,
        "old_path": "",
        "new_path": new_path,
        "message_count": 0,
        "target_message_count": 0,
        "attachments": 0,
    }
    old_path = master_index.get_project_path(session_id)
    # `_same_folder` es una defensa REDUNDANTE a proposito: aunque la comparacion fallara, la misma carpeta ya tiene los mensajes de la sesion
    # (`target > 0`) y mas abajo `applicable` daria False. Se deja para no depender de ese efecto lateral (mutacion equivalente conocida).
    if not old_path or _same_folder(old_path, new_path):
        return info
    if not Path(new_path).is_dir():
        # Una carpeta que no existe no se puede asignar (el guardado daria error): no se pregunta nada que despues no se pueda cumplir.
        return info
    info["old_path"] = old_path
    try:
        messages, attachments = db.count_session_messages_readonly(db.db_path_for(old_path), session_id)
        target, _ = db.count_session_messages_readonly(db.db_path_for(new_path), session_id)
    except Exception:
        logger.warning("No se pudo consultar el historial de %s para el cambio de carpeta", session_id, exc_info=True)
        return info
    info.update(message_count=messages, target_message_count=target, attachments=attachments)
    info["applicable"] = messages > 0 and target == 0
    return info


def change_folder_with_history(session_id: str, new_path: str, title: str = "") -> int:
    """spec cambio-carpeta-historial REQ-003/006: cambia la carpeta de la sesion COPIANDO su historial a la base de la carpeta nueva (la de
    origen no se toca). Devuelve cuantos mensajes se copiaron (0 si no habia carpeta anterior, si es la misma, si el destino ya tenia
    historial de la sesion o si la copia fallo: un fallo se registra y el cambio de carpeta VALE igual, nunca lanza por la copia).
    La copia va a la cola de escritura ANTES de apuntar el indice a la carpeta nueva (ver `assign_project_path`): asi ninguna escritura
    posterior de la sesion la precede. Lanza `FileNotFoundError` si la carpeta nueva no existe (igual que `assign_project_path`)."""
    if not Path(new_path).is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {new_path}")
    old_path = master_index.get_project_path(session_id)
    copied = 0
    # Misma defensa redundante: sin ella, `copy_session_history` tampoco copiaria nada a la misma carpeta (ya tiene los mensajes).
    if old_path and not _same_folder(old_path, new_path):
        src, dst = db.db_path_for(old_path), db.db_path_for(new_path)
        # REQ-013 (spec importar-historial-de-carpeta): la cantidad se calcula ANTES, en lectura. Con el hilo escritor REAL el trabajo
        # corre despues y su resultado no estaba disponible al responder (se informaba 0 aunque la copia SI se hacia: error de 0.35.9).
        try:
            expected, _ = db.count_session_messages_readonly(src, session_id)
            already, _ = db.count_session_messages_readonly(dst, session_id)
        except Exception:
            logger.warning("No se pudo contar el historial de %s antes de copiarlo", session_id, exc_info=True)
            expected, already = 0, 1  # sin poder contar no se promete nada
        copied = expected if already == 0 else 0
        outcome: list[
            int
        ] = []  # lo que REALMENTE copio el trabajo, si ya corrio al responder (cola en linea o hilo veloz)

        def _copy() -> None:
            try:
                done = db.copy_session_history(src, dst, session_id, time.time())
                outcome.append(done or 0)
                if done:
                    logger.info(
                        "Historial copiado: sesion %s, %d mensajes, de %s a %s (la carpeta anterior no se toco)",
                        session_id,
                        done,
                        old_path,
                        new_path,
                    )
            except Exception:
                outcome.append(0)
                logger.warning(
                    "No se pudo copiar el historial de la sesion %s de %s a %s",
                    session_id,
                    old_path,
                    new_path,
                    exc_info=True,
                )

        submit(_copy)
        # Si el trabajo YA corrio, manda su resultado real: una copia que fallo informa 0, no lo esperado. Con el hilo escritor real
        # todavia no corrio y se informa lo esperado (la copia es UNA transaccion; un fallo queda en el log, nunca se calla).
        if outcome:
            copied = outcome[0]
    assign_project_path(session_id, new_path, title)
    return copied


def get_session_visibility(session_id: str) -> bool:
    """spec sesion-publica-privada REQ-007: True si la sesion esta marcada publica en la base de su carpeta. Sin carpeta, sin base o sin fila
    es privada (el valor por defecto): nunca lanza."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return False
    try:
        meta = db.get_session_meta(db_path, session_id)
    except Exception:
        logger.warning("No se pudo leer la visibilidad de la sesion %s", session_id, exc_info=True)
        return False
    return bool(meta and meta["is_public"])


def set_session_visibility(session_id: str, public: bool) -> bool:
    """spec sesion-publica-privada REQ-002/007: marca la sesion como publica o privada en la base de su carpeta y devuelve el estado final. La
    fila de la sesion se crea si todavia no existe (una sesion recien asignada a una carpeta aun no hablo). Lanza `LookupError` con una frase para la
    persona si la sesion no tiene carpeta asignada. Va directo a la base (no por la cola de escritura): es un cambio de la persona, de una sola fila,
    y la respuesta tiene que reflejar el estado real."""
    project_path = master_index.get_project_path(session_id)
    if not project_path:
        raise LookupError(
            "Primero elegí la carpeta de esta sesión: sin carpeta no hay dónde guardar si es pública o privada."
        )
    db_path = db.db_path_for(project_path)
    now = time.time()
    db.ensure_session(db_path, session_id, now)
    db.set_public(db_path, session_id, public, current_windows_user(), now)
    return public


def _is_own_source(source: dict[str, Any], user: str) -> bool:
    """True si la conversacion es MIA: el dueño en la base soy yo, o el indice maestro (local, de esta PC) la tiene a mi nombre. Esto ultimo cubre
    mi propia sesion que marque publica (en la base queda sin dueño)."""
    if master_index._owner_name(source["windows_user_created"]) == user:  # noqa: SLF001
        return True
    return master_index.get_owner(source["id"]) == user


def _can_import_source(source: dict[str, Any], user: str) -> bool:
    """spec sesion-publica-privada REQ-003/004: se puede importar si es mia o si esta marcada PUBLICA (`is_public = 1`). Una privada de otra persona
    no; tampoco una sin dueño que no este marcada publica (base vieja o editada a mano): solo `is_public` abre la puerta."""
    return _is_own_source(source, user) or bool(source.get("is_public"))


def _foreign_public_sources(session_id: str, folder: str) -> list[dict[str, Any]]:
    """Las conversaciones PUBLICAS de otras personas en la base de `folder`, distintas de `session_id`, con la mas reciente primero. Solo lectura.
    Nunca incluye las propias (ni mi sesion publicada: el indice maestro de esta PC dice que es mia) ni las privadas de otros."""
    user = current_windows_user()
    try:
        found = db.read_sessions_readonly(db.db_path_for(folder))
    except Exception:
        logger.warning("No se pudo leer la base de %s para buscar conversaciones publicas", folder, exc_info=True)
        return []
    # `published_by == user` tambien descarta: es MI conversacion publicada (por ejemplo desde otra PC mia, donde el indice maestro no la conoce);
    # seguirla seria resumirme a mi mismo. Es solo para NO seguirla: no da ningun permiso ni cambia como se importa.
    out = [
        s
        for s in found
        if s["id"] != session_id
        and s.get("is_public")
        and not _is_own_source(s, user)
        and s.get("published_by") != user
    ]
    out.sort(key=lambda s: (-(s["updated_at"] or 0), s["id"]))
    return out


def has_foreign_public_sessions(session_id: str) -> bool:
    """spec comando-update-sesiones-publicas REQ-010: True si la carpeta de la sesion tiene alguna conversacion publica ajena con mensajes.
    Decide si el menu «/» ofrece `/update`. Nunca lanza."""
    folder = master_index.get_project_path(session_id)
    if not folder:
        return False
    return any(s["message_count"] > 0 for s in _foreign_public_sources(session_id, folder))


def _display_path(raw: str, folder: str) -> tuple[str, bool, bool]:
    """spec comando-update-sesiones-publicas REQ-013: `(ruta para mostrar, existe en esta PC, es de otra PC)`. Una ruta dentro de la carpeta de la
    sesion se muestra RELATIVA; una fuera se deja como esta y se rotula «ruta de otra PC» (no se puede saber si existe aca)."""
    base = Path(folder)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / raw
    try:
        rel = candidate.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        # La ruta de otra PC (ej. C:/Users/otra/...) no es relativa a esta carpeta: se rotula, sin tocar el disco de esta PC.
        return str(raw), False, True
    return rel.as_posix(), candidate.exists(), False


def _news_note(csid: str, title: str, news: dict[str, Any], summary: str, files: list[dict[str, Any]]) -> str:
    """El texto de la nota que queda en la sesion: quien, de que conversacion, cuantos mensajes nuevos, el resumen y el mapeo de archivos
    (spec comando-update-sesiones-publicas REQ-005/012/014). Sin jerga. Nunca el contenido de archivos ni el texto de comandos."""
    n = len(news["texts"])
    plural = "s" if n != 1 else ""
    lines = [f"[Actualización de CSID {csid} · «{title}» · {n} mensaje{plural} nuevo{plural}]"]
    if summary:
        lines += ["", summary]
    if files:
        shown = []
        for f in files:
            tag = " (ruta de otra PC)" if f["other_pc"] else (" (aún no sincronizado)" if not f["exists"] else "")
            shown.append(f"{f['path']} ({f['op']}){tag}")
        lines += ["", f"Archivos nuevos de CSID {csid}: " + " · ".join(shown)]
    if news["bash_count"]:
        c = news["bash_count"]
        lines += ["", f"CSID {csid} ejecutó {c} comando{'s' if c != 1 else ''} de consola; no sé qué archivos crearon."]
    return "\n".join(lines)


def _save_update_note(db_path: Path, session_id: str, text: str, now: float) -> None:
    """Guarda la nota como un mensaje de `user` de la sesion (queda en el historial y en lo que ve el modelo en el proximo turno)."""
    message_id = f"{session_id}:update:{int(now * 1000)}:{uuid.uuid4().hex[:6]}"
    db.insert_message(db_path, message_id, session_id, "user", now)
    db.insert_part(db_path, f"{message_id}:p", message_id, session_id, "text", {"text": text}, now)


def _update_model() -> str:
    """El modelo con que se resume: el de siempre (`settings.llm_model`, con su proveedor), igual que `/compact` sin cuerpo."""
    from app.config import get_settings  # import diferido: config no debe cargarse al importar persistencia

    from app.llm.providers import canonical  # import diferido: providers importa la config

    return canonical(get_settings().llm_model)


def _update_message(result: dict[str, Any]) -> str:
    """La frase que ve la persona al terminar `/update` (sin jerga)."""
    parts: list[str] = []
    if result["updated"]:
        total = sum(n["messages"] for n in result["notes"])
        who = ", ".join(sorted({n["csid"] for n in result["notes"]}))
        many = result["updated"] != 1
        mensajes = "s" if total != 1 else ""
        # «conversación» -> «conversaciones»: el plural pierde la tilde (antes salia «conversaciónes»).
        conversaciones = "conversaciones públicas" if many else "conversación pública"
        parts.append(
            f"Actualicé {result['updated']} {conversaciones} ({total} mensaje{mensajes} nuevo{mensajes}) de: {who}."
        )
    elif not result["failed"]:
        parts.append("No hay novedades en las conversaciones públicas de esta carpeta.")
    for f in result["failed"]:
        parts.append(f"No pude resumir «{f['title']}» (CSID {f['csid']}): {f['reason']}")
    gone = len(result["no_longer_public"])
    if gone:
        if gone == 1:
            parts.append("1 conversación ya no es pública y se dejó de seguir.")
        else:
            parts.append(f"{gone} conversaciones ya no son públicas y se dejó de seguir.")
    return " ".join(parts)


def update_from_public(session_id: str, folder: str) -> dict[str, Any]:
    """spec comando-update-sesiones-publicas: resume SOLO lo nuevo de cada conversacion publica ajena de la carpeta, con el CSID de quien la publico,
    y lista los archivos que esa persona creo o edito (`write`/`edit`/adjuntos). Una nota por conversacion con novedades; las notas anteriores no se
    tocan. La marca de agua de cada conversacion avanza SOLO si su nota se guardo. De mejor esfuerzo por conversacion: una que falla no frena a las
    demas. Devuelve `{updated, notes, files, failed, no_longer_public, message}`. Sincrona: la llama el endpoint desde un hilo."""
    import asyncio

    from app.friendly_errors import (
        describe_model_error,
    )  # import diferido: evita cargar el cliente de errores al importar persistencia
    from app.session import compaction  # import diferido: compaction importa el cliente LLM (ciclo)

    result: dict[str, Any] = {
        "updated": 0,
        "notes": [],
        "files": [],
        "failed": [],
        "no_longer_public": [],
        "message": "",
    }
    db_path = db.db_path_for(folder)
    now = time.time()
    followed = {s["id"]: s for s in _foreign_public_sources(session_id, folder)}

    # Las que ya se seguian y pasaron a privadas: se avisa y se dejan de seguir. Las borradas simplemente no aparecen.
    try:
        known = db.read_sessions_readonly(db_path)
    except Exception:
        known = []
    for s in known:
        if (
            s["id"] not in followed
            and s["id"] != session_id
            and not s.get("is_public")
            and db.get_follow_mark(db_path, session_id, s["id"])
        ):
            result["no_longer_public"].append(s["id"])

    for source_id, meta in followed.items():
        title = meta["title"] or source_id[:8]
        csid = meta.get("published_by") or "desconocido"
        news = db.read_news_since(db_path, source_id, db.get_follow_mark(db_path, session_id, source_id))
        if news["last"] is None:
            continue  # sin novedades: ni modelo, ni nota, ni marca
        files = []
        for f in news["files"]:
            shown, exists, other_pc = _display_path(f["path"], folder)
            files.append(
                {"path": shown, "op": f["op"], "exists": exists, "other_pc": other_pc, "csid": csid, "title": title}
            )
        try:
            summary = ""
            if news["texts"]:
                transcript = "\n".join(
                    f"[{'Usuario' if role == 'user' else 'Asistente'}] {text}" for role, text in news["texts"]
                )
                summary = asyncio.run(compaction._summarize(transcript, _update_model()))
            note = _news_note(csid, title, news, summary, files)
            _save_update_note(db_path, session_id, note, now + 0.001 * result["updated"])
            db.set_follow_mark(db_path, session_id, source_id, news["last"][0], news["last"][1], now)
        except Exception as exc:  # noqa: BLE001 -- una conversacion que falla no frena a las demas ni avanza su marca
            friendly = describe_model_error(exc, "")
            logger.warning(
                "No se pudo actualizar desde la conversacion publica %s [%s]: %s",
                source_id,
                friendly.code,
                exc,
                exc_info=True,
            )
            result["failed"].append({"id": source_id, "title": title, "csid": csid, "reason": friendly.message})
            continue
        result["updated"] += 1
        result["notes"].append({"id": source_id, "title": title, "csid": csid, "messages": len(news["texts"])})
        result["files"].extend(files)

    result["message"] = _update_message(result)
    return result


def folder_sessions(session_id: str, new_path: str) -> dict[str, Any]:
    """spec importar-historial-de-carpeta REQ-001/002/003: las conversaciones ANTERIORES del usuario que ya viven en la base de `new_path`,
    para avisarle y ofrecerle importarlas o abrirlas. SOLO LECTURA (nunca escribe ni crea la carpeta) y de mejor esfuerzo (nunca lanza:
    si algo no se puede leer, `applicable` es False y la carpeta se asigna como siempre). Cuenta solo sesiones PROPIAS (mismo
    `windows_user_created`), con mensajes y distintas de la actual; la mas reciente primero, con tope de 20.
    `can_import` es False si la sesion actual ya tiene una conversacion en memoria (importar encima la mezclaria)."""
    from app.session.events import get_session_store  # import diferido: events importa persistence (ciclo)

    result: dict[str, Any] = {"applicable": False, "can_import": False, "sessions": []}
    if not Path(new_path).is_dir():
        return result
    own = master_index.get_project_path(session_id)
    if own and _same_folder(own, new_path):
        return result  # la carpeta que la sesion YA tiene no es un cambio: no se avisa
    try:
        found = db.read_sessions_readonly(db.db_path_for(new_path))
    except Exception:
        logger.warning("No se pudo leer la base de %s para buscar conversaciones anteriores", new_path, exc_info=True)
        return result
    user = current_windows_user()
    if any(s["id"] == session_id and s["message_count"] > 0 for s in found):
        return result  # la sesion actual ya hablo en esa carpeta: importar encima la mezclaria
    registered = {
        e["session_id"]
        for e in master_index.list_all_for_user(user)
        if e.get("project_path") and _same_folder(e["project_path"], new_path)
    }
    mine = [
        {
            "id": s["id"],
            "title": s["title"] or s["id"][:8],
            "message_count": s["message_count"],
            "updated_at": s["updated_at"],
            "registered": s["id"] in registered,
            # spec sesion-publica-privada REQ-003: `own` = es mia (la base me lo dice, o el indice maestro la tiene a mi nombre aunque la haya
            # hecho publica); `public` = esta marcada publica. Una publica AJENA se ofrece; una privada ajena, nunca.
            "own": _is_own_source(s, user),
            "public": bool(s.get("is_public")),
            # spec comando-update-sesiones-publicas REQ-002: de quien viene (solo una etiqueta); «desconocido» si se publico antes de esta version.
            "published_by": (s.get("published_by") or "desconocido") if s.get("is_public") else "",
        }
        for s in found
        if s["id"] != session_id and s["message_count"] > 0 and _can_import_source(s, user)
    ]
    mine.sort(key=lambda s: (-(s["updated_at"] or 0), s["id"]))
    if not mine:
        return result
    live = get_session_store().get(session_id)
    talked = live is not None and any(m.get("role") in ("user", "assistant") for m in live.messages)
    return {"applicable": True, "can_import": not talked, "sessions": mine[:20]}


def preflight_import(session_id: str, folder: str, source_id: str) -> None:
    """spec importar-historial-de-carpeta REQ-006: valida, SOLO LEYENDO, que se pueda importar `source_id` a `session_id` en `folder`, para que el
    endpoint rechace con un 400 claro ANTES de asignar la carpeta. Lanza `FileNotFoundError` (carpeta inexistente) o `ValueError` con una frase
    para la persona: origen inexistente o de otro usuario, importar una conversacion en si misma, o una sesion que ya hablo en esa carpeta
    (importar encima la mezclaria)."""
    if not Path(folder).is_dir():
        raise FileNotFoundError(f"La carpeta no existe: {folder}")
    if source_id == session_id:
        raise ValueError("No se puede importar una conversación en sí misma.")
    user = current_windows_user()
    try:
        found = {s["id"]: s for s in db.read_sessions_readonly(db.db_path_for(folder))}
    except Exception as exc:
        raise ValueError("No se pudo leer el historial de esa carpeta.") from exc
    source = found.get(source_id)
    if source is None or not _can_import_source(source, user):
        raise ValueError("No se encontró esa conversación anterior en la carpeta.")
    if found.get(session_id, {}).get("message_count", 0) > 0:
        raise ValueError("Esta conversación ya tiene mensajes en esa carpeta: importar encima los mezclaría.")


def import_session_history(session_id: str, folder: str, source_id: str) -> int:
    """spec importar-historial-de-carpeta REQ-004/005/006: COPIA el historial de la sesion anterior `source_id` (de la base de `folder`) a la
    sesion actual `session_id`, que vive en esa misma carpeta. La sesion de origen NO se modifica. Devuelve cuantos mensajes copio (0 si
    la actual ya tenia mensajes ahi: nunca se mezcla). Lanza `FileNotFoundError` si la carpeta no existe y `ValueError` con una frase para la
    persona si no se puede importar (origen inexistente o ajeno, o importar una sesion en si misma).
    A diferencia de la copia de 0.35.9, es SINCRONA (no pasa por la cola de escritura): la sesion es nueva, no hay escrituras suyas
    que ordenar, y asi el llamador puede recargar la memoria de inmediato y responder con la cantidad real."""
    preflight_import(session_id, folder, source_id)
    db_path = db.db_path_for(folder)
    # spec sesion-publica-privada REQ-005/006: si la conversacion es de OTRA persona (publica), los resultados de Outlook/Teams no se copian; la
    # copia queda privada y a nombre de quien importa (la fila nueva nace con `windows_user_tag()` y `is_public = 0`).
    source = next((x for x in db.read_sessions_readonly(db_path) if x["id"] == source_id), None)
    foreign = source is not None and not _is_own_source(source, current_windows_user())
    copied = db.import_session_history(
        db_path, source_id, session_id, db.windows_user_tag(), time.time(), hide_private_tools=foreign
    )
    if copied:
        logger.info(
            "Historial importado: sesion %s <- %s, %d mensajes, carpeta %s (la sesion anterior no se toco)",
            session_id,
            source_id,
            copied,
            folder,
        )
    return copied


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


def record_part_start(session_id: str, message_id: str, part_id: str, part_type: str, data: dict[str, Any]) -> None:
    """REQ-002: insert al INICIAR un part (bloque de texto o tool-call) --
    si el backend crashea antes de que termine, ya queda un registro
    parcial en disco."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.insert_part(db_path, part_id, message_id, session_id, part_type, data, now))


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


def user_message_snapshot(session_id: str, message_id: str) -> dict[str, Any] | None:
    """editar-mensaje-tras-reinicio REQ-001/003: texto, imagenes y archivos de un mensaje 'user' cerrado,
    leidos de SQLite (ver `db.user_message_snapshot`). `None` si no hay base o no existe ese id."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return None
    return db.user_message_snapshot(db_path, session_id, message_id)


def truncate_from_message(session_id: str, message_id: str, until_message_id: str | None = None) -> None:
    """editar-mensaje-tras-reinicio REQ-002/004/007: borra en SQLite ese mensaje y todo lo posterior. Va por
    la MISMA cola de escritura que el resto, asi que las escrituras del turno que se relance despues se
    ejecutan DESPUES del truncado (nunca al reves).

    REQ-014 c (spec fix-detener-y-progreso-visible): `until_message_id` = el mensaje de usuario del turno que
    esta CORRIENDO: se borra solo hasta ahi (sin incluirlo), para no llevarse lo que ese turno ya escribio."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    submit(lambda: db.truncate_from_message(db_path, session_id, message_id, until_message_id))


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


def session_summary(session_id: str, project_path: str | None = None) -> dict[str, Any]:
    """spec indice-resiliente-y-borrado-seguro: titulo, carpeta y cantidad de mensajes de una sesion, de MEJOR ESFUERZO (nunca
    lanza): `message_count` es None si no hay carpeta o su base no se pudo leer. Lee en solo lectura; no incluye texto de mensajes."""
    folder = project_path if project_path is not None else master_index.get_project_path(session_id)
    summary: dict[str, Any] = {"title": "", "project_path": folder or "", "message_count": None}
    if not folder:
        return summary
    try:
        for s in db.read_sessions_readonly(db.db_path_for(folder)):
            if s["id"] == session_id:
                summary["title"] = s["title"] or ""
                summary["message_count"] = s["message_count"]
                break
        else:
            summary["message_count"] = 0
    except Exception:
        logger.warning("No se pudo leer la base de %s para resumir la sesion %s", folder, session_id, exc_info=True)
    return summary


def log_session_deleted(session_id: str, summary: dict[str, Any]) -> None:
    """REQ-001: UNA linea en el log por cada sesion borrada (manual o por purga): id, titulo, carpeta y cuantos mensajes tenia.
    Nunca el texto de los mensajes. Antes un borrado no dejaba NADA y no se pudo saber quien borro 9 sesiones."""
    count = summary.get("message_count")
    logger.info(
        "Sesion borrada: id=%s titulo=%r carpeta=%s mensajes=%s",
        session_id,
        summary.get("title") or "",
        summary.get("project_path") or "sin carpeta",
        "?" if count is None else count,
    )


def delete_session(session_id: str) -> None:
    # REQ-001: el resumen se toma ANTES de borrar (despues ya no hay nada que contar) y el registro se escribe cuando el borrado
    # termino, desde el mismo hilo escritor.
    summary = session_summary(session_id)
    if not summary["title"]:
        summary["title"] = _index_title(session_id)
    db_path = _db_path_for_session(session_id)
    if db_path is not None:
        submit(lambda: db.delete_session(db_path, session_id))
    # El registro va DENTRO del mismo trabajo que borra del indice (no es un trabajo aparte): un borrado sin carpeta sigue
    # encolando UN solo trabajo y el registro solo se escribe si el indice ya quedo borrado.
    submit(lambda: _finish_delete(session_id, summary))


def _finish_delete(session_id: str, summary: dict[str, Any]) -> None:
    master_index.delete_session(session_id)
    log_session_deleted(session_id, summary)


def _index_title(session_id: str) -> str:
    try:
        for entry in master_index.list_all_for_user(current_windows_user()):
            if entry["session_id"] == session_id:
                return entry.get("title") or ""
    except Exception:
        logger.warning("No se pudo leer el titulo del indice para %s", session_id, exc_info=True)
    return ""


_EMPTY_OTHERS: dict[str, list] = {"mine": [], "public": [], "private_by_user": []}
_OTHERS_CAP = 20  # tope de mis sesiones listadas en el aviso de eliminar


def _others_in_folder(session_id: str, folder: str | None) -> dict[str, list]:
    """spec aviso-eliminar-sesion-usuarios REQ-001..005: quien mas usa la carpeta de la sesion que se va a eliminar, SIN romper el aislamiento por usuario.
    - `mine`: mis otras sesiones con mensajes (nombre y cantidad), la mas reciente primero, tope 20.
    - `public`: las publicas de OTRAS personas con su titulo y quien las publico («desconocido» si es anterior a `published_by`). Son publicas a proposito.
    - `private_by_user`: las PRIVADAS de otras personas, solo `{user, count}` (mas sesiones primero). NUNCA titulos, ids ni cantidad de mensajes.
    La sesion que se elimina no se repite. Solo lectura y de mejor esfuerzo: sin carpeta o con una base ilegible devuelve vacio y nunca lanza."""
    empty: dict[str, list] = {key: [] for key in _EMPTY_OTHERS}
    if not folder:
        return empty
    try:
        found = db.read_sessions_readonly(db.db_path_for(folder))
    except Exception:
        logger.warning("No se pudo leer la base de %s para listar las otras sesiones", folder, exc_info=True)
        return empty
    user = current_windows_user()
    mine: list[dict[str, Any]] = []
    public: list[dict[str, Any]] = []
    private_counts: dict[str, int] = {}
    for s in sorted(found, key=lambda x: (-(x["updated_at"] or 0), x["id"])):
        if s["id"] == session_id:
            continue
        if _is_own_source(s, user):
            if s["message_count"] > 0:
                mine.append({"id": s["id"], "title": s["title"] or s["id"][:8], "message_count": s["message_count"]})
        elif s.get("is_public"):
            if s["message_count"] > 0:
                public.append(
                    {
                        "id": s["id"],
                        "title": s["title"] or s["id"][:8],
                        "published_by": s.get("published_by") or "desconocido",
                        "message_count": s["message_count"],
                    }
                )
        elif s["message_count"] > 0:
            owner = master_index._owner_name(s["windows_user_created"]) or "desconocido"  # noqa: SLF001
            private_counts[owner] = private_counts.get(owner, 0) + 1
    groups = [{"user": u, "count": c} for u, c in sorted(private_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"mine": mine[:_OTHERS_CAP], "public": public[:_OTHERS_CAP], "private_by_user": groups}


def deletion_info(session_id: str) -> dict[str, Any]:
    """REQ-010: lo que el modal «Eliminar sesion» le muestra a la persona ANTES de confirmar: cuantos mensajes se van a perder, en que
    carpeta estan y si el MISMO id existe tambien en otra carpeta conocida (borrar la sesion no la elimina). De mejor esfuerzo.
    `others_in_folder` (spec aviso-eliminar-sesion-usuarios) es ADITIVO: quien mas usa esa carpeta, sin titulos de sesiones privadas ajenas."""
    own = master_index.get_project_path(session_id)
    summary = session_summary(session_id, own)
    copies: list[dict[str, Any]] = []
    user = current_windows_user()
    for folder in master_index.known_folders():
        if folder == own:
            continue
        try:
            for s in db.read_sessions_readonly(db.db_path_for(folder)):
                if s["id"] == session_id and master_index._owner_name(s["windows_user_created"]) == user:  # noqa: SLF001
                    copies.append({"project_path": folder, "message_count": s["message_count"]})
        except Exception:
            logger.warning("No se pudo mirar la carpeta %s buscando copias de %s", folder, session_id, exc_info=True)
    return {
        "message_count": summary["message_count"] or 0,
        "project_path": own or "",
        "copies_elsewhere": copies,
        "others_in_folder": _others_in_folder(session_id, own),
    }


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
    # REQ-010 a: la raiz del proyecto permite calcular `exists` de cada adjunto.
    return db.load_messages_for_display(db_path, session_id, project_root_for_session(session_id))


def load_pending_question(session_id: str) -> dict[str, Any] | None:
    """REQ-018 (spec fix-detener-y-progreso-visible): la ultima pregunta (tool `question`) de esta sesion sin un mensaje del usuario
    despues, desde su vision.db (ver db.load_pending_question). None si la sesion no tiene carpeta: sin SQLite no hay de donde leerla."""
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return None
    return db.load_pending_question(db_path, session_id)


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


def search_sessions(term: str, session_id_for_path: str | None = None) -> list[dict[str, Any]]:
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


def known_project_folders() -> list[str]:
    """Carpetas de proyecto que el indice maestro conoce para el usuario de Windows actual,
    sin repetir. Vacia = todavia no hay ninguna sesion con carpeta (nada que buscar en SQLite)."""
    folders: list[str] = []
    for entry in master_index.list_all_for_user(current_windows_user()):
        project_path = entry.get("project_path") or ""
        if project_path and project_path not in folders:
            folders.append(project_path)
    return folders


def search_all_sessions_for_current_user(term: str, limit: int = 50) -> list[dict[str, Any]]:
    """REQ-007 (spec busqueda-mensajes, pedido 2026-09-30): busca en TODAS las carpetas de
    proyecto del usuario, no solo en la de la sesion abierta.

    Cada sesion vive en SU carpeta (`<carpeta>/session/vision.db`), asi que la busqueda global
    recorre las carpetas que el indice maestro conoce para este usuario de Windows -- las mismas
    que ya usa `list_all_sessions_for_current_user` para el sidebar -- y une los resultados. El
    filtro por usuario sigue aplicandose en cada base (REQ-005 de proyecto-por-sesion): en una
    carpeta compartida cada uno ve solo lo suyo. Una carpeta que ya no existe o con la base
    ilegible se OMITE (no rompe la busqueda en las demas). Resultado: las mas recientes primero,
    maximo `limit` en total.
    """
    user = current_windows_user()
    folders = known_project_folders()

    merged: dict[str, dict[str, Any]] = {}
    for project_path in folders:
        db_path = db.db_path_for(project_path)
        try:
            found = db.search_sessions(db_path, term, limit=limit, current_user=user)
        except Exception:  # noqa: BLE001 -- una base rota no debe tapar los resultados del resto
            logger.warning("No se pudo buscar en la carpeta '%s'", project_path, exc_info=True)
            continue
        for item in found:
            merged.setdefault(item["id"], item)  # la misma sesion no se repite
    return sorted(merged.values(), key=lambda r: r.get("updated_at") or 0, reverse=True)[:limit]


def update_message_response_type(session_id: str, message_id: str, response_type: str) -> None:
    db_path = _db_path_for_session(session_id)
    if db_path is None:
        return
    now = time.time()
    submit(lambda: db.update_message_response_type(db_path, message_id, session_id, response_type, now))
