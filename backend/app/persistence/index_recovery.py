"""
Recuperacion del indice maestro de sesiones (spec indice-resiliente-y-borrado-seguro, 0.35.7).

Por que existe: el indice maestro (`~/.vision/vision.db`, tabla `session_index`) es el UNICO lugar que dice "la sesion X vive en
la carpeta Y". Si una fila desaparece, la sesion queda huerfana aunque sus datos esten sanos en su carpeta: en la PC del usuario
se encontraron 7 sesiones asi (hasta 571 mensajes) y, para una sesion sin fila, la pantalla mostraba la carpeta de Vision como si
fuera la del proyecto. Este modulo hace dos cosas, ambas al arrancar y antes de la purga:

1. `backup_index`: una copia diaria del indice en `~/.vision/backups/` (API de backup de SQLite: consistente aunque el indice
   se este usando), verificada, escrita a un temporal y renombrada; se conservan las 7 mas recientes.
2. `rescan_index`: vuelve a registrar en el indice las sesiones huerfanas que estan en la base de una carpeta conocida.

Garantias (todas probadas): el re-escaneo NUNCA escribe en una base de sesiones (las abre `mode=ro`), NUNCA registra una sesion
con lapida (borrada a proposito), una de otro usuario de Windows, una que ya esta en el indice ni una sin actividad dentro de la
retencion (la purga la exportaria y borraria enseguida), y nunca lanza: una carpeta ausente o una base ilegible se omiten."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from app.persistence import db, master_index
from app.persistence.service import current_windows_user

logger = logging.getLogger(__name__)

BACKUP_KEEP = 7
BACKUP_PREFIX = "vision-indice-"
# Solo los archivos con ESTE nombre exacto son "de la copia diaria": una copia manual del usuario (u otro archivo) jamas se rota.
_BACKUP_NAME_RE = re.compile(r"^vision-indice-\d{8}\.db$")


def default_backup_dir() -> Path:
    return master_index.master_db_path().parent / "backups"


def backup_index(dest_dir: Path | None = None, now: float | None = None) -> Path | None:
    """REQ-007: copia diaria y verificada del indice maestro. Devuelve la ruta de la copia nueva, o `None` si hoy ya hay una o si
    fallo (el fallo se registra y NUNCA se propaga: no debe impedir ni el re-escaneo ni la purga)."""
    dest = Path(dest_dir) if dest_dir is not None else default_backup_dir()
    moment = time.time() if now is None else now
    final = dest / time.strftime(f"{BACKUP_PREFIX}%Y%m%d.db", time.localtime(moment))
    try:
        if final.exists():
            return None
        dest.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + ".tmp")
        tmp.unlink(missing_ok=True)
        try:
            with master_index._connect() as src:  # noqa: SLF001 - misma capa; asegura el esquema antes de copiar
                out = sqlite3.connect(str(tmp))
                try:
                    src.backup(out)
                    if not _integrity_ok(out):
                        raise sqlite3.DatabaseError("la copia del indice no paso la verificacion de integridad")
                finally:
                    out.close()
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        _rotate(dest)
        logger.info("Copia diaria del indice de sesiones: %s", final)
        return final
    except Exception:
        logger.warning("No se pudo hacer la copia diaria del indice de sesiones", exc_info=True)
        return None


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    """True si SQLite da por buena la base (`PRAGMA integrity_check` = "ok"). Una copia que no lo cumple se descarta."""
    return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _rotate(dest: Path) -> None:
    """Deja solo las `BACKUP_KEEP` copias diarias mas recientes (el nombre lleva la fecha, asi que ordenar por nombre es ordenar por
    dia). Solo toca archivos con el nombre exacto de la copia diaria."""
    mine = sorted((p for p in dest.iterdir() if _BACKUP_NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True)
    for old in mine[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            logger.warning("No se pudo borrar la copia vieja del indice %s", old, exc_info=True)


def rescan_index(now: float | None = None) -> list[str]:
    """REQ-004/005/006: registra en el indice las sesiones huerfanas del usuario actual que estan en la base de una carpeta
    conocida. Devuelve los ids registrados. Idempotente y sin escribir jamas en las bases de sesiones."""
    # import diferido: `purge` importa este modulo (la purga arranca con la copia y el re-escaneo), un import arriba haria un ciclo.
    from app.persistence.purge import RETENTION_DAYS

    user = current_windows_user()
    moment = time.time() if now is None else now
    cutoff = moment - RETENTION_DAYS * 86400
    best: dict[str, dict] = {}  # id -> la copia mas reciente entre todas las carpetas
    for folder in master_index.known_folders():
        try:
            sessions = db.read_sessions_readonly(db.db_path_for(folder))
        except Exception:
            logger.warning("Re-escaneo: se omite la carpeta %s (base ilegible)", folder, exc_info=True)
            continue
        for s in sessions:
            sid = s["id"]
            if master_index._owner_name(s["windows_user_created"]) != user:  # noqa: SLF001 - mismo criterio que get_owner
                continue
            if (s["updated_at"] or 0) < cutoff:
                continue
            if master_index.session_index_exists(sid) or master_index.is_tombstoned(sid):
                continue
            if sid not in best or s["updated_at"] > best[sid]["updated_at"]:
                best[sid] = {**s, "folder": folder}
    registered: list[str] = []
    for sid, s in sorted(best.items(), key=lambda kv: (-kv[1]["updated_at"], kv[0])):
        master_index.set_project_path(sid, s["folder"], title=s["title"] or sid[:8], windows_user_created=user)
        registered.append(sid)
        logger.info(
            "Re-escaneo: sesion %s (%r, %s mensajes) vuelta a registrar en el indice con su carpeta %s",
            sid,
            s["title"],
            s["message_count"],
            s["folder"],
        )
    return registered
