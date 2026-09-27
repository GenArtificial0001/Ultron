"""Investigacion de rendimiento 2026-09: `TurnPerfRecorder` (app/perf_log.py)
escribe una linea JSON por turno en perf.log con el desglose real de tiempo
-- test de humo, redirige el archivo a un temp dir (mismo patron que
`tmp_master_db` en conftest.py, nunca tocar el perf.log real)."""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.perf_log import TurnPerfRecorder


@pytest.fixture
def tmp_perf_log(tmp_path, monkeypatch):
    log_path = tmp_path / "perf.log"
    monkeypatch.setattr(type(get_settings()), "perf_log_file", property(lambda self: log_path))
    return log_path


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_finish_writes_one_json_line_with_expected_fields(tmp_perf_log):
    perf = TurnPerfRecorder("sess-1", "claude-sonnet-latest")
    perf.mark_system_prompt_built(12.3)
    perf.mark_first_token()
    perf.record_tool_call("oracle_query", 450.2)
    perf.record_tool_call("bash", 30.0)
    perf.finish("done")

    lines = _read_lines(tmp_perf_log)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["session_id"] == "sess-1"
    assert entry["model"] == "claude-sonnet-latest"
    assert entry["status"] == "done"
    assert entry["system_prompt_build_ms"] == 12.3
    assert entry["ttft_ms"] is not None
    assert entry["tool_calls"] == [
        {"name": "oracle_query", "ms": 450.2},
        {"name": "bash", "ms": 30.0},
    ]
    assert entry["total_ms"] >= entry["ttft_ms"]


def test_mark_first_token_only_records_the_first_call(tmp_perf_log):
    perf = TurnPerfRecorder("sess-1", "m")
    perf.mark_first_token()
    first = perf.ttft_ms
    perf.mark_first_token()
    assert perf.ttft_ms == first


def test_finish_never_raises_when_log_dir_cannot_be_created(monkeypatch):
    """No debe romper el turno del usuario por un problema de logging de
    diagnostico -- mismo criterio que tool_timings.py."""
    perf = TurnPerfRecorder("sess-1", "m")

    class BrokenPath:
        @property
        def parent(self):
            raise OSError("disco lleno (simulado)")

    monkeypatch.setattr(type(get_settings()), "perf_log_file", property(lambda self: BrokenPath()))
    perf.finish("done")  # no debe lanzar
