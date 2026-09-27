"""
Escritor de persistencia en background (spec persistencia-sqlite-sesiones,
REQ-002): un UNICO hilo dedicado consume una cola de trabajos (callables)
en ORDEN, para garantizar que -por ejemplo- un mensaje se inserte antes
que sus parts hijos (integridad referencial), sin bloquear el event loop
de FastAPI con I/O de disco sincrono.

Mismo criterio de "no romper el turno si falla la persistencia" que ya
usa app/tool_timings.py: cualquier excepcion en un trabajo se loguea y se
descarta, nunca se propaga hacia el loop del agente (persistir es una
capa de resiliencia, no el camino critico de la conversacion).
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_jobs: queue.Queue[Callable[[], Any]] = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def _run_worker() -> None:
    while True:
        job = _jobs.get()
        try:
            job()
        except Exception:
            # REQ-002: persistir es best-effort. Un fallo puntual (ej.
            # carpeta de proyecto en un disco de red momentaneamente no
            # disponible) no debe cortar la conversacion en curso.
            logger.warning("Fallo un trabajo de persistencia de sesion", exc_info=True)
        finally:
            _jobs.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_run_worker, name="vision-session-writer", daemon=True
            )
            _worker.start()


def submit(job: Callable[[], Any]) -> None:
    """Encola un trabajo de escritura. No bloquea al llamador (ni siquiera
    con await) -- se usa desde codigo async con una llamada directa
    (queue.Queue.put_nowait es thread-safe y no bloqueante en la practica
    salvo cola sin limite, que es el caso aca)."""
    _ensure_worker()
    _jobs.put_nowait(job)


def flush(timeout: float = 5.0) -> bool:
    """Espera a que se vacie la cola -- usado en pruebas/validacion end-to-
    end para saber cuando ya se escribio todo lo pendiente."""
    try:
        _jobs.join()
        return True
    except Exception:
        return False
