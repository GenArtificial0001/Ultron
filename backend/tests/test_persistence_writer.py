"""Unit del escritor en background (`persistence/writer.py`, REQ-002): orden
FIFO, aislamiento de fallos y recuperacion del hilo."""

from __future__ import annotations

import threading

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


def test_worker_is_restarted_if_it_died(monkeypatch):
    class Dead:
        def is_alive(self):
            return False

    monkeypatch.setattr(writer, "_worker", Dead())
    ran: list[int] = []
    writer.submit(lambda: ran.append(1))
    writer.flush()
    assert ran == [1] and writer._worker.is_alive()


def test_flush_returns_false_when_join_fails(monkeypatch):
    def broken_join():
        raise RuntimeError("x")

    monkeypatch.setattr(writer._jobs, "join", broken_join)
    assert writer.flush() is False
