"""API de interruptores (spec interruptores-conexiones-skills REQ-004/006/008/009/011), por HTTP contra la app real con base maestra temporal.

Lo que mas importa: (1) nada de lo que manda el navegador se guarda sin validarse contra lo REAL configurado; (2) ninguna respuesta lleva campos ni credenciales de una conexion;
(3) lo guardado es solo la lista de lo apagado, en la base global; (4) el `.env` sigue mandando: una conexion que el `.env` apaga no se puede prender desde la pantalla."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import connections as conns
from app import main
from app import switches as sw
from app.config import Settings
from app.persistence import master_index
from app.tools import skill as skill_mod

SECRET = "TOKEN-INTERRUPTORES-7f3a9c"


def _base(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def make_skill(root: Path, name: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\ncuerpo secreto de {name}\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    store: dict[str, str] = {}
    monkeypatch.setattr(master_index, "get_app_setting", lambda k: store.get(k))
    monkeypatch.setattr(master_index, "set_app_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(master_index, "unset_app_setting", lambda k: store.pop(k, None))
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    env = _base(
        oracle_host="host-secreto", oracle_user="u", oracle_password=SECRET, oracle_service_name="s",
        confluence_email="a@b.com", confluence_api_token=SECRET, bq_project_id="proyecto-bq",
    )
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    monkeypatch.setattr(main, "get_settings", lambda: env)
    conns.reset_for_tests()
    globales = tmp_path / "globales"
    globales.mkdir()
    for name in ("auditoria", "slotting"):
        make_skill(globales, name)
    store[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "true"
    monkeypatch.setattr(skill_mod, "discover_skills", lambda: skill_mod._scan_skills_dir(globales))
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: [])
    yield store
    conns.reset_for_tests()


@pytest.fixture
def client():
    return TestClient(main.app)


def _all_strings(node):
    """Todos los textos de un JSON YA DECODIFICADO (valores y claves), a cualquier profundidad."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_strings(v)


def assert_no_paths_or_content(resp) -> None:
    """Ninguna ruta ni contenido en la respuesta. Se comparan los TEXTOS ya decodificados: el JSON escapa las barras de Windows y buscar la ruta cruda en `resp.text` nunca la
    encontraria (ese defecto hizo que este chequeo fuera vacio hasta que una mutacion lo destapo)."""
    import re

    windows_path = re.compile(r"[A-Za-z]:[\\/]")
    for text in _all_strings(resp.json()):
        for needle in ("cuerpo secreto", "SKILL.md", str(Path.home()), "locales", "globales"):
            assert needle not in text, f"la respuesta expone: {needle}"
        assert not windows_path.search(text), f"hay algo con forma de ruta de Windows: {text[:40]}"
        assert "/home/" not in text and "/Users/" not in text


def rows(client) -> dict[str, dict]:
    return {c["key"]: c for c in client.get("/api/switches/connections").json()["connections"]}


# ---------------------------------------------------------------------------------------------------------------------
# conexiones
# ---------------------------------------------------------------------------------------------------------------------
def test_the_list_has_every_configured_connection_active_and_inactive(client):
    client.post("/api/switches/connections", json={"key": "confluence:principal", "enabled": False})

    data = rows(client)

    assert data["confluence:principal"]["enabled"] is False and data["oracle:datamart"]["enabled"] is True and "bigquery:bigquery" in data


def test_turning_one_off_and_on_through_the_api(client):
    off = client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": False})
    assert off.status_code == 200 and rows(client)["oracle:datamart"]["enabled"] is False

    on = client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": True})
    assert on.status_code == 200 and rows(client)["oracle:datamart"]["enabled"] is True


def test_the_post_returns_the_fresh_list_so_the_screen_needs_no_second_request(client):
    resp = client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": False}).json()

    assert {c["key"]: c["enabled"] for c in resp["connections"]}["oracle:datamart"] is False


def test_it_is_stored_in_the_global_master_database(client, _isolated):
    client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": False})

    assert json.loads(_isolated[sw.CONNECTIONS_DISABLED_KEY]) == ["oracle:datamart"]


@pytest.mark.parametrize("bad", ["", "oracle", "oracle:inexistente", "'; DROP TABLE app_settings; --", "../../x", "x" * 5000, "ORACLE:DATAMART ", None])
def test_an_unknown_key_is_a_404_and_nothing_is_stored(client, _isolated, bad):
    resp = client.post("/api/switches/connections", json={"key": bad, "enabled": False})

    assert resp.status_code == 404 and sw.CONNECTIONS_DISABLED_KEY not in _isolated


def test_a_missing_enabled_flag_is_a_422(client, _isolated):
    assert client.post("/api/switches/connections", json={"key": "oracle:datamart"}).status_code == 422
    assert sw.CONNECTIONS_DISABLED_KEY not in _isolated


def test_the_list_never_carries_a_field_or_a_credential(client):
    body = client.get("/api/switches/connections").text

    for secret in (SECRET, "a@b.com", "host-secreto", "proyecto-bq", "oracle_password", "confluence_api_token"):
        assert secret not in body, f"salio un dato de la conexion: {secret}"


def test_the_post_response_never_carries_credentials_either(client):
    body = client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": False}).text

    assert SECRET not in body and "host-secreto" not in body


def test_a_connection_the_env_switches_off_cannot_be_turned_on_from_the_screen(client, monkeypatch):
    off = _base(enable_confluence=False, confluence_email="a@b.com", confluence_api_token=SECRET)
    monkeypatch.setattr(conns, "get_settings", lambda: off)
    monkeypatch.setattr("app.config.get_settings", lambda: off)
    monkeypatch.setattr(main, "get_settings", lambda: off)
    conns.reset_for_tests()
    assert rows(client)["confluence:principal"]["env_disabled"] is True

    resp = client.post("/api/switches/connections", json={"key": "confluence:principal", "enabled": True})

    assert resp.status_code == 409 and ".env" in resp.json()["detail"]


def test_a_connection_the_env_switches_off_can_still_be_turned_off_from_the_screen(client, monkeypatch):
    off = _base(enable_confluence=False, confluence_email="a@b.com", confluence_api_token=SECRET)
    monkeypatch.setattr(conns, "get_settings", lambda: off)
    monkeypatch.setattr("app.config.get_settings", lambda: off)
    monkeypatch.setattr(main, "get_settings", lambda: off)
    conns.reset_for_tests()

    assert client.post("/api/switches/connections", json={"key": "confluence:principal", "enabled": False}).status_code == 200


def test_the_state_changes_what_the_model_receives(client):
    from app.tools import build_registry

    client.post("/api/switches/connections", json={"key": "confluence:principal", "enabled": False})

    sent = {d["function"]["name"] for d in build_registry().definitions()}

    assert not [n for n in sent if n.startswith("confluence_")] and "oracle_query" in sent


# ---------------------------------------------------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------------------------------------------------
def test_the_skills_list_has_each_skill_with_its_state_and_the_general_switch(client):
    data = client.get("/api/switches/skills").json()

    assert data["global_enabled"] is True
    assert {(s["name"], s["scope"], s["enabled"]) for s in data["skills"]} == {("auditoria", "global", True), ("slotting", "global", True)}


def test_turning_a_skill_off_and_on_through_the_api(client):
    off = client.post("/api/switches/skills", json={"name": "slotting", "enabled": False}).json()
    assert {s["name"]: s["enabled"] for s in off["skills"]} == {"auditoria": True, "slotting": False}

    on = client.post("/api/switches/skills", json={"name": "slotting", "enabled": True}).json()
    assert all(s["enabled"] for s in on["skills"])


@pytest.mark.parametrize("bad", ["", "no-existe", "../../x", "'; DROP", "Slotting", "slot ting", "slotting;", None])
def test_an_unknown_skill_is_a_404_and_nothing_is_stored(client, _isolated, bad):
    resp = client.post("/api/switches/skills", json={"name": bad, "enabled": False})

    assert resp.status_code == 404 and sw.SKILLS_DISABLED_KEY not in _isolated


def test_the_skills_list_never_exposes_a_skills_content_or_path(client):
    resp = client.get("/api/switches/skills")

    assert_no_paths_or_content(resp)
    assert {k for s in resp.json()["skills"] for k in s} == {"name", "scope", "enabled"}, "cada skill trae SOLO nombre, origen y estado"


def test_the_general_switch_is_still_reported_and_independent(client, _isolated):
    _isolated[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "false"
    monkeypatch_global = client.get("/api/switches/skills").json()

    assert monkeypatch_global["global_enabled"] is False


def test_skill_and_connection_switches_do_not_interfere(client, _isolated):
    client.post("/api/switches/skills", json={"name": "slotting", "enabled": False})
    client.post("/api/switches/connections", json={"key": "oracle:datamart", "enabled": False})

    assert set(_isolated) >= {sw.SKILLS_DISABLED_KEY, sw.CONNECTIONS_DISABLED_KEY}
    assert rows(client)["oracle:datamart"]["enabled"] is False
    assert {s["name"]: s["enabled"] for s in client.get("/api/switches/skills").json()["skills"]}["slotting"] is False


def test_a_disabled_skill_disappears_from_the_prompt_after_the_api_call(client):
    client.post("/api/switches/skills", json={"name": "slotting", "enabled": False})

    xml = skill_mod.list_available_skills_xml()

    assert "slotting" not in xml and "auditoria" in xml


def test_spaces_around_a_real_skill_name_are_tolerated_but_only_the_exact_name_is_stored(client, _isolated):
    """El endpoint recorta espacios y saltos de linea de los extremos (un copiar-pegar sucio no es un ataque). Lo que se GUARDA es el nombre real y exacto de la skill, nunca la
    cadena que mando el navegador: no puede colarse nada distinto de un nombre de skill existente."""
    resp = client.post("/api/switches/skills", json={"name": "  slotting\n", "enabled": False})

    assert resp.status_code == 200
    assert json.loads(_isolated[sw.SKILLS_DISABLED_KEY]) == ["slotting"]


# ---------------------------------------------------------------------------------------------------------------------
# skills LOCALES de una sesion (REQ-009) y pertenencia de la sesion
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def local_skills(monkeypatch, tmp_path):
    """Una skill local `solo-aca` para la sesion `sid-1` (y ninguna para otra)."""
    locales = tmp_path / "locales"
    locales.mkdir()
    make_skill(locales, "solo-aca")
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: skill_mod._scan_skills_dir(locales) if session_id == "sid-1" else [])
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)


def test_the_local_skills_of_a_session_are_listed_with_their_scope(client, local_skills):
    data = client.get("/api/switches/skills", params={"session_id": "sid-1"}).json()

    scopes = {(s["name"], s["scope"]) for s in data["skills"]}
    assert ("solo-aca", "local") in scopes and ("auditoria", "global") in scopes


def test_without_a_session_only_the_global_skills_are_listed(client, local_skills):
    names = {s["name"] for s in client.get("/api/switches/skills").json()["skills"]}

    assert "solo-aca" not in names and {"auditoria", "slotting"} <= names


def test_another_session_does_not_see_the_local_skills_of_this_one(client, local_skills):
    names = {s["name"] for s in client.get("/api/switches/skills", params={"session_id": "sid-2"}).json()["skills"]}

    assert "solo-aca" not in names


def test_a_local_skill_can_be_turned_off_only_with_its_session(client, local_skills, _isolated):
    sin_sesion = client.post("/api/switches/skills", json={"name": "solo-aca", "enabled": False})
    assert sin_sesion.status_code == 404 and sw.SKILLS_DISABLED_KEY not in _isolated, "sin la sesion no se la reconoce: no se guarda"

    con_sesion = client.post("/api/switches/skills", params={"session_id": "sid-1"}, json={"name": "solo-aca", "enabled": False})
    assert con_sesion.status_code == 200
    assert {s["name"]: s["enabled"] for s in con_sesion.json()["skills"]}["solo-aca"] is False


def test_a_session_that_belongs_to_another_user_is_refused_with_403(client, monkeypatch, local_skills):
    from fastapi import HTTPException

    def not_mine(session_id):
        raise HTTPException(status_code=403, detail="No tenes acceso a esa sesion.")

    monkeypatch.setattr(main, "_assert_session_owner", not_mine)

    assert client.get("/api/switches/skills", params={"session_id": "sid-ajena"}).status_code == 403
    assert client.post("/api/switches/skills", params={"session_id": "sid-ajena"}, json={"name": "slotting", "enabled": False}).status_code == 403


def test_a_refused_session_stores_nothing(client, monkeypatch, local_skills, _isolated):
    from fastapi import HTTPException

    monkeypatch.setattr(main, "_assert_session_owner", lambda sid: (_ for _ in ()).throw(HTTPException(status_code=403, detail="no")))

    client.post("/api/switches/skills", params={"session_id": "sid-ajena"}, json={"name": "slotting", "enabled": False})

    assert sw.SKILLS_DISABLED_KEY not in _isolated


def test_the_local_skill_list_never_exposes_content_or_paths(client, local_skills):
    resp = client.get("/api/switches/skills", params={"session_id": "sid-1"})

    assert_no_paths_or_content(resp)
    assert {k for s in resp.json()["skills"] for k in s} == {"name", "scope", "enabled"}
