"""Unit del escritor en background (`persistence/writer.py`, REQ-002): orden
FIFO, aislamiento de fallos y recuperacion del hilo."""

from __future__ import annotations

import threading

import pytest

from app.persistence import writer


def test_jobs_run_in_submission_order():
    seen: list[int] = []
    for i in range(20):
        writer.submit(lambda i=i: seen.append(i))
    assert writer.flush() is True
    assert seen == list(range(20))


def test_failing_job_is_swallowed_and_next_one_runs():
    seen: list[str] = []

    def boom():
        raise RuntimeError("disco de red caido")

    writer.submit(boom)
    writer.submit(lambda: seen.append("despues"))
    writer.flush()
    assert seen == ["despues"]


def test_jobs_run_on_dedicated_daemon_thread():
    names: list[tuple[str, bool]] = []
    writer.submit(lambda: names.append((threading.current_thread().name, threading.current_thread().daemon)))
    writer.flush()
    assert names == [("vision-session-writer", True)]


# El hilo extra se retira con SystemExit a proposito: pytest lo informa como 'excepcion de hilo no manejada' y aca es lo esperado.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_is_restarted_if_it_died(monkeypatch):
    """Si el hilo escritor murio, `submit` arranca uno nuevo y el trabajo se ejecuta.

    El "hilo muerto" es un doble: el escritor real sigue vivo. Por eso el hilo que arranca `_ensure_worker` NO se deja suelto: se registra y, al terminar,
    se le pide que termine. Antes quedaban DOS escritores sobre la misma cola, que ejecutan trabajos en paralelo y fuera de orden (un `rename_session` antes de que
    exista la fila que renombra), y eso hacia fallar de forma intermitente tests POSTERIORES de otros archivos (p. ej. el del titulo manual en `test_project_endpoints`)."""

    class Dead:
        def is_alive(self):
            return False

    started: list[threading.Thread] = []
    real_thread = threading.Thread

    class Tracking(real_thread):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)

    monkeypatch.setattr(writer.threading, "Thread", Tracking)
    real_worker = writer._worker
    monkeypatch.setattr(writer, "_worker", Dead())
    ran: list[int] = []
    try:
        writer.submit(lambda: ran.append(1))
        writer.flush()
        assert ran == [1] and writer._worker.is_alive()
        assert len(started) == 1 and started[0] is writer._worker  # se arranco UN hilo nuevo, no mas
    finally:
        # Se deja exactamente un escritor: el nuevo se retira y se restaura el original (que nunca murio).
        monkeypatch.undo()
        writer._worker = real_worker
        stop = threading.Event()
        # El hilo nuevo consume de la MISMA cola que el original: no se puede "matar" a ciegas. Se lo hace salir con un trabajo que solo el lo ejecuta y termina su bucle.
        for t in started:
            _ask_to_stop(t, stop)


def _ask_to_stop(thread: threading.Thread, stop: threading.Event) -> None:
    """Pide a un escritor extra que termine: se le encola un trabajo que lanza SystemExit SOLO si lo ejecuta ese hilo; el otro lo reintenta."""

    def retire():
        if threading.current_thread() is thread:
            raise SystemExit

    for _ in range(200):
        if not thread.is_alive():
            return
        writer.submit(retire)
        writer.flush()


def test_flush_returns_false_when_join_fails(monkeypatch):
    def broken_join():
        raise RuntimeError("x")

    monkeypatch.setattr(writer._jobs, "join", broken_join)
    assert writer.flush() is False
