"""spec tiempos-herramientas-seguros: el registro de tiempos no pierde mediciones ni se corrompe con hilos."""

from __future__ import annotations

import json
import threading
import time

import pytest

from app import tool_timings as tt


@pytest.fixture(autouse=True)
def timings_file(tmp_path, monkeypatch):
    f = tmp_path / "data" / "tool_timings.json"
    monkeypatch.setattr(tt, "TIMINGS_FILE", f)
    return f


def run_threads(fn, n_threads=8):
    errors: list[BaseException] = []

    def wrap(k):
        try:
            fn(k)
        except BaseException as exc:  # noqa: BLE001 -- el test los reporta
            errors.append(exc)

    ths = [threading.Thread(target=wrap, args=(k,)) for k in range(n_threads)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert not errors, errors


# REQ-001
def test_concurrent_records_do_not_lose_measurements():
    def work(_k):
        for i in range(8):
            tt.record_duration("bash", float(i))

    run_threads(work)

    assert len(tt._load()["bash"]) == tt.WINDOW_SIZE  # 320 escritas: antes quedaban 4-10


def test_each_operation_keeps_its_own_history_under_concurrency():
    def work(k):
        for i in range(12):  # 2 hilos x 12 = 24 por operacion: supera la ventana (20) y ejercita el recorte
            tt.record_duration(f"op{k % 4}", float(i))

    run_threads(work)

    data = tt._load()
    assert sorted(data) == ["op0", "op1", "op2", "op3"]
    assert all(len(v) == tt.WINDOW_SIZE for v in data.values())


# REQ-002
def test_the_file_is_always_valid_json_even_while_being_written():
    stop = threading.Event()
    bad: list[str] = []

    def reader(_k):
        while not stop.is_set():
            time.sleep(0.002)
            if tt.TIMINGS_FILE.exists():
                try:
                    json.loads(tt.TIMINGS_FILE.read_text(encoding="utf-8"))
                except ValueError as exc:
                    bad.append(str(exc))
                except OSError:
                    time.sleep(0.001)  # Windows: el archivo se esta reemplazando justo ahora; lo que importa es que no se vea cortado

    t = threading.Thread(target=reader, args=(0,))
    t.start()
    try:
        run_threads(lambda _k: [tt.record_duration("read", float(i)) for i in range(10)], n_threads=4)
    finally:
        stop.set()
        t.join()

    assert not bad, f"un lector vio el archivo a medias: {bad[:2]}"
    # y, con un lector externo abriendo el archivo todo el tiempo, ninguna escritura se perdio por PermissionError
    assert len(tt._load()["read"]) == tt.WINDOW_SIZE


def test_a_crash_while_writing_leaves_the_previous_file_intact(monkeypatch):
    tt.record_duration("read", 1.0)
    before = tt.TIMINGS_FILE.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disco lleno")

    monkeypatch.setattr(tt.os, "replace", boom)
    tt.record_duration("read", 2.0)  # no debe lanzar

    assert tt.TIMINGS_FILE.read_text(encoding="utf-8") == before
    assert not list(tt.TIMINGS_FILE.parent.glob("*.tmp")), "quedo un temporal huerfano"


def test_a_transient_permission_error_is_retried_not_lost(monkeypatch):
    """Windows: si un antivirus o un lector tiene el archivo abierto justo ahora, `os.replace` da PermissionError.
    Antes la medicion se perdia en silencio; ahora se reintenta unos milisegundos."""
    real_replace = tt.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError("archivo en uso")
        return real_replace(src, dst)

    monkeypatch.setattr(tt.os, "replace", flaky)
    tt.record_duration("read", 7.0)

    assert calls["n"] == 4
    assert tt._load() == {"read": [7.0]}


def test_a_permanent_permission_error_gives_up_without_raising(monkeypatch):
    monkeypatch.setattr(tt.os, "replace", lambda *_a: (_ for _ in ()).throw(PermissionError("bloqueado")))
    tt.record_duration("read", 1.0)  # no debe lanzar ni dejar temporales
    assert not list(tt.TIMINGS_FILE.parent.glob("*.tmp"))


# REQ-003
def test_reading_the_budget_during_writes_never_fails_or_returns_empty():
    tt.record_duration("bash", 100.0)
    seen_empty: list[bool] = []

    def work(k):
        for _ in range(15):
            if k % 2:
                tt.record_duration("bash", 100.0)
            else:
                _ms, has_history = tt.get_budget_ms("bash")
                if not has_history:
                    seen_empty.append(True)

    run_threads(work)

    assert not seen_empty, "get_budget_ms vio el historial vacio por un archivo cortado"


# REQ-004
def test_a_corrupt_file_is_set_aside_and_logged_once(caplog):
    tt.TIMINGS_FILE.parent.mkdir(parents=True)
    tt.TIMINGS_FILE.write_text('{"read": [1.0, 2.', encoding="utf-8")

    with caplog.at_level("WARNING", logger="app.tool_timings"):
        assert tt._load() == {}
        tt.record_duration("read", 5.0)
        tt.record_duration("read", 6.0)

    assert (tt.TIMINGS_FILE.parent / "tool_timings.json.corrupto").read_text(encoding="utf-8").startswith('{"read"')
    assert sum("corrupto" in r.getMessage() for r in caplog.records) == 1
    assert tt._load() == {"read": [5.0, 6.0]}


# REQ-005
def test_window_format_and_budget_are_unchanged():
    for i in range(tt.WINDOW_SIZE + 5):
        tt.record_duration("grep", float(i))

    data = json.loads(tt.TIMINGS_FILE.read_text(encoding="utf-8"))
    assert data["grep"] == [float(i) for i in range(5, tt.WINDOW_SIZE + 5)]
    avg, has = tt.get_budget_ms("grep")
    assert has and avg == pytest.approx(sum(data["grep"]) / tt.WINDOW_SIZE)
    assert tt.get_budget_ms("nunca-visto") == (tt.DEFAULT_BUDGET_MS, False)
