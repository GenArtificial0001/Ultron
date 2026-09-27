"""spec bateria-qa-integracion (refactor-monolitos REQ-002): el guard de /loop
cuenta como 'pendientes' tambien las SUB-TAREAS, no solo las top-level.
Bug real reportado por el usuario (2026-09): con la tarea principal
`in_progress` y sus pasos `pending`, /loop me fallaba 400 'no hay tareas
pendientes' -- y el principio del proyecto es 'cualquier proceso tiene pasos',
asi que el loop debe seguir mientras haya pasos pendientes.
"""

from __future__ import annotations

from app.main import collect_pending_todos


class _FakeSession:
    def __init__(self, todos):
        self.todos = todos


def test_pending_top_level():
    s = _FakeSession(
        [{"content": "A", "status": "pending"}, {"content": "B", "status": "completed"}]
    )
    assert [t["content"] for t in collect_pending_todos(s)] == ["A"]


def test_pending_subtasks_enable_loop():
    # tarea principal in_progress con pasos pending -- el caso del bug real
    s = _FakeSession(
        [
            {
                "content": "Demo: loop",
                "status": "in_progress",
                "subtasks": [
                    {"content": "Ya hecho", "status": "completed"},
                    {"content": "Resumir", "status": "pending"},
                    {"content": "Guardar", "status": "pending"},
                ],
            }
        ]
    )
    pend = collect_pending_todos(s)
    assert len(pend) == 2, pend
    assert {"Resumir", "Guardar"} == {t["content"] for t in pend}


def test_no_pending():
    s = _FakeSession(
        [
            {
                "content": "X",
                "status": "completed",
                "subtasks": [{"content": "s", "status": "completed"}],
            }
        ]
    )
    assert collect_pending_todos(s) == []
