"""REQ-002 (spec bateria-qa-integracion): endpoints globales `/api/alarms*`
via TestClient. Patron DBManager (AGENTS.md): DB maestro redirigido a temp
dir (fixture `tmp_master_db`), sin pegar al server real ni a credenciales.
El sonido por evento cubre tambien `run` (con/sin archivo) sin lanzar apps:
el caso "archivo inexistente" devuelve 400 y el "sin archivo" ok=False.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_list_empty(tmp_master_db):
    assert TestClient(app).get("/api/alarms").json() == {"schedules": []}


def test_create_crud_fired_run(tmp_master_db):
    c = TestClient(app)
    r = c.post(
        "/api/alarms",
        json={
            "label": "Diaria",
            "kind": "recurring",
            "trigger": {"rule": "daily", "time": "08:30"},
            "sound": "sirena",
            "file_path": "",
            "enabled": True,
        },
    )
    assert r.status_code == 200
    sid = r.json()["schedule"]["id"]
    assert r.json()["schedule"]["sound"] == "sirena"

    fid = c.post(
        "/api/alarms",
        json={
            "label": "Con archivo",
            "kind": "once",
            "trigger": {"target_iso": "2026-09-13T08:30"},
            "sound": "pulse",
            "file_path": "C:/nada/quenoexiste.bat",
            "enabled": True,
        },
    ).json()["schedule"]["id"]

    assert len(c.get("/api/alarms").json()["schedules"]) == 2

    up = c.put(f"/api/alarms/{sid}", json={"label": "Ren", "sound": "double"}).json()[
        "schedule"
    ]
    assert up["sound"] == "double"

    assert c.post(f"/api/alarms/{sid}/fired").status_code == 200

    # run: sin archivo -> ok=False (no intenta abrir nada)
    assert c.post(f"/api/alarms/{sid}/run").json()["ok"] is False
    # run: archivo inexistente -> 400 (OSError, no lanza apps reales)
    assert c.post(f"/api/alarms/{fid}/run").status_code == 400

    assert c.delete(f"/api/alarms/{sid}").status_code == 200
    assert c.delete(f"/api/alarms/{sid}").status_code == 404  # ya borrada
    assert len(c.get("/api/alarms").json()["schedules"]) == 1
