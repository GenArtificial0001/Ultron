"""REQ-004 (spec bateria-qa-integracion): comando thin `/qa-audit`. El endpoint
corre la bateria como subproceso y vuelca los FAIL / SIN COBERTURA a
`session.todos` (shape todowrite). Determinista: se inyecta un
`subprocess.run` FAKE (patron DBManager: el test NO depende del estado real
del repo ni corre tooling externo) que devuelve un hallazgo conocido.
"""

from __future__ import annotations

from subprocess import CompletedProcess

from fastapi.testclient import TestClient

import app.persistence.service as persistence
from app import main


def test_qa_audit_creates_pending_todos(tmp_master_db, monkeypatch):
    monkeypatch.setattr(main, "_assert_session_owner", lambda sid: None)
    monkeypatch.setattr(persistence, "record_todos", lambda *a, **k: None)

    def fake_run(*args, **kwargs):
        return CompletedProcess(
            args=(), returncode=1, stdout="[FAIL] test fake hallazgo\n", stderr=""
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    store = main.get_session_store()
    session = store.get_or_create("qa-sess")
    session.todos = []

    c = TestClient(main.app)
    r = c.post("/api/commands/qa-audit/qa-sess")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ran"] is True and data["exit_code"] == 1
    assert any("test fake hallazgo" in i for i in data["issues"])

    session = store.get("qa-sess")
    pending = [t for t in (session.todos or []) if t.get("status") == "pending"]
    assert len(pending) >= 1, "se esperaba al menos 1 tarea QA pendiente"
    assert any(t["content"].startswith("QA: ") for t in pending)
