"""Unit de `app/tools/base.py`: contrato Tool/ToolRegistry, dispatch (nunca
lanza), modo plan (`exclude_dangerous`) y auditoria append-only de tools
peligrosas. Complementa `test_tool_dispatch_threading.py` (paralelismo)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.tools import base
from app.tools.base import Tool, ToolRegistry


def _tool(name="t", execute=None, **kw) -> Tool:
    return Tool(
        name=name,
        description="desc",
        parameters={"type": "object", "properties": {}},
        execute=execute or (lambda args, session=None: "ok"),
        **kw,
    )


@pytest.fixture
def timings(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(base, "record_duration", lambda name, ms: calls.append((name, ms)))
    return calls


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "audit.log"
    monkeypatch.setattr(base, "get_settings", lambda: type("S", (), {"audit_log_file": path})())
    return path


def test_openai_schema_shape():
    schema = _tool("bash").to_openai_schema()
    assert schema == {
        "type": "function",
        "function": {"name": "bash", "description": "desc", "parameters": {"type": "object", "properties": {}}},
    }


def test_registry_register_get_all_and_override():
    reg = ToolRegistry()
    reg.register(_tool("a"))
    reg.register(_tool("a", dangerous=True))  # mismo nombre reemplaza
    assert reg.get("a").dangerous is True and reg.get("zzz") is None
    assert [t.name for t in reg.all()] == ["a"]


def test_definitions_plan_mode_hides_dangerous_tools():
    reg = ToolRegistry()
    reg.register(_tool("read"))
    reg.register(_tool("bash", dangerous=True))
    assert [d["function"]["name"] for d in reg.definitions()] == ["read", "bash"]
    assert [d["function"]["name"] for d in reg.definitions(exclude_dangerous=True)] == ["read"]


def test_dispatch_unknown_tool():
    assert "no existe" in asyncio.run(ToolRegistry().dispatch("nada", "{}"))


def test_dispatch_invalid_json_never_raises():
    reg = ToolRegistry()
    reg.register(_tool("t"))
    assert asyncio.run(reg.dispatch("t", "{no json")).startswith("Error: argumentos invalidos")


def test_dispatch_empty_args_and_result_is_stringified(timings):
    reg = ToolRegistry()
    seen = {}
    reg.register(_tool("t", execute=lambda args, session=None: seen.update(a=args) or 42))
    assert asyncio.run(reg.dispatch("t", "")) == "42"
    assert seen["a"] == {}


def test_dispatch_passes_args_and_session(timings):
    reg = ToolRegistry()
    got = {}
    reg.register(_tool("t", execute=lambda args, session=None: got.update(args=args, s=session) or "ok"))
    asyncio.run(reg.dispatch("t", '{"x": 1}', session="SESION"))
    assert got == {"args": {"x": 1}, "s": "SESION"}


def test_dispatch_async_tool_is_awaited(timings):
    async def run(args, session=None):
        return "async-ok"

    reg = ToolRegistry()
    reg.register(_tool("t", execute=run))
    assert asyncio.run(reg.dispatch("t", "{}")) == "async-ok"


def test_dispatch_not_thread_safe_tool_runs_in_event_loop_thread(timings):
    import threading

    threads: list[int] = []
    reg = ToolRegistry()
    reg.register(
        _tool("t", thread_safe=False, execute=lambda a, s=None: threads.append(threading.get_ident()) or "ok")
    )

    async def go():
        loop_thread = threading.get_ident()
        await reg.dispatch("t", "{}")
        return loop_thread

    loop_thread = asyncio.run(go())
    assert threads == [loop_thread]


def test_dispatch_exception_becomes_error_text_and_still_records_timing(timings):
    def boom(args, session=None):
        raise ValueError("kaboom")

    reg = ToolRegistry()
    reg.register(_tool("t", execute=boom))
    out = asyncio.run(reg.dispatch("t", "{}"))
    assert out == "Error ejecutando 't': kaboom"
    assert [name for name, _ in timings] == ["t"]  # el fallo tambien se mide


def test_dangerous_tool_is_audited_before_running(timings, audit_file):
    reg = ToolRegistry()
    reg.register(_tool("bash", dangerous=True))
    reg.register(_tool("read"))
    asyncio.run(reg.dispatch("bash", '{"command": "dir"}'))
    asyncio.run(reg.dispatch("read", "{}"))  # no peligrosa: no se audita
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "bash" and entry["args"] == {"command": "dir"} and entry["ts"]


def test_audit_is_append_only_and_keeps_unicode(audit_file):
    base._audit("write", {"path": "canción.txt"})
    base._audit("edit", {"path": "b"})
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["tool"] for x in lines] == ["write", "edit"]
    assert "canción" in lines[0]  # ensure_ascii=False
