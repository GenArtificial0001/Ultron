"""
Auto-purga de sesiones inactivas (spec persistencia-sqlite-sesiones,
REQ-007): sesiones sin actividad hace mas de 30 dias se EXPORTAN a
Markdown antes de borrarse -- nunca se pierde informacion sin dejar un
respaldo en disco. Se corre al arrancar el proceso y despues una vez por
dia mientras el backend sigue corriendo (ver app/main.py, arranque del
task de background).

Arquitectura carpeta-por-sesion (definitiva): ya NO hay una unica carpeta
"activa" global sobre la cual purgar -- se recorren TODAS las carpetas
distintas que aparecen en el indice maestro (`master_index.py`), cada una
con su propio vision.db, y se purga cada una por separado."""

from __future__ import annotations

import asyncio
import logging
import time

from app.persistence import db, master_index
from app.persistence.service import current_windows_user
from app.persistence.transcript import export_session_to_file

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
PURGE_INTERVAL_SECONDS = 24 * 60 * 60  # 1 vez por dia


def _purge_one_project_folder(project_path: str) -> int:
    """Purga las sesiones inactivas de UNA carpeta de proyecto puntual.
    Separado de `purge_once()` para poder iterar sobre todas las carpetas
    conocidas sin duplicar la logica de purga en si."""
    db_path = db.db_path_for(project_path)
    if not db_path.exists():
        return 0

    cutoff = time.time() - (RETENTION_DAYS * 86400)
    candidates = db.sessions_older_than(db_path, cutoff)
    purged = 0
    for candidate in candidates:
        session_id = candidate["id"]
        try:
            exported_path = export_session_to_file(session_id)
            if exported_path is None:
                logger.warning(
                    "No se pudo exportar la sesion '%s' antes de purgarla -- se "
                    "OMITE el borrado para no perder informacion sin respaldo.",
                    session_id,
                )
                continue
            db.delete_session(db_path, session_id)
            master_index.delete_session(session_id)
            purged += 1
            logger.info(
                "Sesion '%s' purgada por inactividad (>%s dias); respaldo en %s",
                session_id,
                RETENTION_DAYS,
                exported_path,
            )
        except Exception:
            # REQ-007: un fallo en UNA sesion no debe frenar la purga del
            # resto -- se loguea y se sigue con la siguiente candidata.
            logger.warning("Fallo al purgar la sesion '%s'", session_id, exc_info=True)
    return purged


def purge_once() -> int:
    """Ejecuta un ciclo de purga sobre TODAS las carpetas de proyecto
    conocidas (una por cada sesion distinta que el usuario actual de
    Windows haya creado, segun el indice maestro). Devuelve cuantas
    sesiones se purgaron en total. Sincrono a proposito (se llama desde
    un thread/tarea de background, no desde el hot path del chat)."""
    entries = master_index.list_all_for_user(current_windows_user())
    project_paths = {e["project_path"] for e in entries if e["project_path"]}

    total_purged = 0
    for project_path in project_paths:
        total_purged += _purge_one_project_folder(project_path)
    return total_purged


async def run_purge_loop() -> None:
    """Tarea de background de larga duracion: purga al arrancar y despues
    cada PURGE_INTERVAL_SECONDS. Se lanza una sola vez desde el startup
    de FastAPI (ver app/main.py)."""
    while True:
        try:
            await asyncio.to_thread(purge_once)
        except Exception:
            logger.warning("Fallo el ciclo de auto-purga de sesiones", exc_info=True)
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
