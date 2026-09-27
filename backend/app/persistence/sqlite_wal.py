"""
Activacion segura del modo WAL de SQLite, compartida por las dos bases de
Vision (el indice maestro `master_index.py` y la base de cada carpeta de
proyecto `db.py`).

Por que existe (BUG real, QA-009, deteccion QA 2026-09-21): las dos bases
ejecutaban `PRAGMA journal_mode = WAL` en CADA conexion. Pasar a WAL necesita
acceso EXCLUSIVO al archivo, y sobre una base recien creada varias conexiones
lo piden a la vez: al arrancar el backend (purga + agentes + alarmas en hilos
distintos sobre `<carpeta de datos>/vision.db`) o con el hilo escritor de persistencia
y un request HTTP sobre la base de un proyecto nuevo. Dos conexiones que
tienen el archivo abierto y quieren subir a exclusivo se bloquean
mutuamente; SQLite lo detecta y devuelve `database is locked` AL INSTANTE,
sin esperar el `timeout` de `sqlite3.connect` (el busy handler no se invoca
cuando esperar produciria un deadlock). Medido: 1 de cada 3 arranques sobre
una base nueva con 6 hilos fallaba.

Solucion:
    1. WAL es PERSISTENTE en el archivo: una vez activado, no hace falta
       volver a pedirlo. Se LEE el modo actual (lectura compartida, no pide
       exclusivo) y solo se cambia si todavia no es WAL -- en regimen normal
       esto nunca vuelve a competir.
    2. El cambio en si se serializa con un lock del proceso, asi los hilos
       de UN mismo Vision nunca lo piden a la vez.
    3. Contra otro PROCESO (dos Vision abiertos sobre la misma carpeta
       compartida) el lock de proceso no alcanza: se reintenta con espera
       corta hasta `WAL_RETRY_SECONDS` antes de rendirse.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

WAL_RETRY_SECONDS = 5.0
_WAL_RETRY_SLEEP = 0.05
_wal_switch_lock = threading.Lock()


def _current_journal_mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def ensure_wal(conn: sqlite3.Connection) -> None:
    """Deja `conn` en modo WAL sin competir por un lock exclusivo innecesario.
    Lanza `sqlite3.OperationalError` solo si otro proceso retiene la base mas
    de `WAL_RETRY_SECONDS`."""
    if _current_journal_mode(conn) == "wal":
        return
    deadline = time.monotonic() + WAL_RETRY_SECONDS
    with _wal_switch_lock:
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                logger.debug("Base ocupada por otro proceso al activar WAL, reintentando")
                time.sleep(_WAL_RETRY_SLEEP)
