"""Unit de la tool `todowrite`: normalizacion de items, limite de 2 niveles,
evento SSE y persistencia. La persistencia real (SQLite) se stubea -- aca se
verifica el CONTRATO con `persistence.record_todos`, no la DB (eso lo cubren
los tests de `master_index`/`db`)."""

from __future__ import annotations

import app.persistence.service as persistence
from app.tools.todowrite import VALID_STATUSES, _clean_todo_item, _todowrite_execute


class FakeSession:
    def __init__(self, session_id: str | None = "s1") -> None:
        self.session_id = session_id
        self.todos: list[dict] = []
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def test_clean_item_applies_defaults():
    assert _clean_todo_item({}, allow_subtasks=False) == {
        "content": "",
        "status": "pending",
        "priority": "medium",
    }


def test_invalid_status_falls_back_to_pending():
    item = _clean_todo_item({"content": "x", "status": "hecho"}, allow_subtasks=False)
    assert item["status"] == "pending" and "hecho" not in VALID_STATUSES


def test_subtasks_are_limited_to_two_levels():
    item = _clean_todo_item(
        {
            "content": "padre",
            "subtasks": [{"content": "hija", "subtasks": [{"content": "nieta"}]}],
        },
        allow_subtasks=True,
    )
    assert item["subtasks"] == [{"content": "hija", "status": "pending", "priority": "medium"}]


def test_non_list_subtasks_become_empty_list():
    item = _clean_todo_item({"content": "x", "subtasks": "no-es-lista"}, allow_subtasks=True)
    assert item["subtasks"] == []


def test_non_list_todos_returns_error_string_and_never_raises():
    out = _todowrite_execute({"todos": "nope"}, session=FakeSession())
    assert out.startswith("Error:")


def test_without_session_only_returns_summary():
    out = _todowrite_execute({"todos": [{"content": "A", "status": "completed"}]})
    assert "1 items" in out and "A [completed]" in out


def test_empty_list_summary():
    assert "(lista vacia)" in _todowrite_execute({"todos": []})


def test_with_session_sets_todos_pushes_event_and_persists(monkeypatch):
    saved: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(persistence, "record_todos", lambda sid, todos: saved.append((sid, todos)))
    session = FakeSession("abc")
    out = _todowrite_execute(
        {"todos": [{"content": "A", "subtasks": [{"content": "a1"}, {"content": "a2"}]}]},
        session=session,
    )
    assert session.todos[0]["content"] == "A"
    assert session.events == [("todo", {"todos": session.todos})]
    assert saved == [("abc", session.todos)]
    assert "2 sub-tareas" in out


def test_session_without_id_skips_persistence(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("no debe persistir sin session_id")

    monkeypatch.setattr(persistence, "record_todos", _boom)
    _todowrite_execute({"todos": [{"content": "A"}]}, session=FakeSession(session_id=None))
