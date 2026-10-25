"""API del gestor de conexiones `/api/vault/*` (spec gestor-conexiones A7: REQ-003/004/004b/005/006/007/007b/008/009/010).

Lo central: TODO pedido pasa por la barrera «esta PC + la propia pagina de Vision» (sin interruptor que la apague), ninguna respuesta lleva un secreto salvo `/secret/`
y esa solo responde DESBLOQUEADO. Base maestra en `tmp_path` (autouse) y DPAPI real; el verificador de Windows Hello se inyecta (el real llega en B3)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app import config_store as cs
from app import connections as conns
from app import credentials
from app import main as main_module
from app import secret_access as sa
from app import secret_store
from app import switches as sw

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

PASSWORD = "Clave-Datamart-77"
BASE = "http://127.0.0.1:8020"
KEY = {"type": "service_account", "project_id": "psapmpldash-e-s-110", "private_key": "CLAVE-PRIVADA-FALSA", "client_email": "sa@p.iam.gserviceaccount.com"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / ".env"))
    monkeypatch.setattr(config, "_VISION_SET_ENV", {})
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()
    sa.lock()
    sa.set_verifier(None)
    yield tmp_path
    sa.lock()
    sa.set_verifier(None)
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()


@pytest.fixture
def api():
    return TestClient(main_module.app, base_url=BASE, client=("127.0.0.1", 50000))


def oracle(name="Datamart", **over):
    fields = {"oracle_user": "usuario1", "oracle_password": PASSWORD, "oracle_host": "dmart.empresa.com", "oracle_service_name": "HA_DMARTP1"}
    fields.update(over)
    return {"type": "oracle", "name": name, "fields": fields}


def unlock_ok():
    sa.set_verifier(lambda: True)
    sa.unlock()


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007b: la barrera, siempre
# ---------------------------------------------------------------------------------------------------------------------
ROUTES = [
    ("get", "/api/vault/status"),
    ("post", "/api/vault/import"),
    ("get", "/api/vault/connections"),
    ("post", "/api/vault/connections"),
    ("get", "/api/vault/general"),
    ("put", "/api/vault/general"),
    ("post", "/api/vault/bigquery-key"),
    ("post", "/api/vault/connections/test-draft"),  # «Probar» antes de guardar (2026-10-10)
    ("post", "/api/vault/bigquery/test"),
    ("get", "/api/vault/unlock"),
    ("post", "/api/vault/unlock"),
    ("get", "/api/vault/connections/x/secret/oracle_password"),
    ("get", "/api/vault/general/secret/fish_api_key"),
    ("delete", "/api/vault/legacy-files"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_request_from_another_machine_is_refused_on_every_vault_route(method, path):
    other = TestClient(main_module.app, base_url=BASE, client=("10.0.0.5", 50000))

    resp = getattr(other, method)(path, **({"json": {}} if method in ("post", "put") else {}))

    assert resp.status_code == 403 and "PC donde corre ULTRON" in resp.json()["detail"]


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_host_that_is_not_local_is_refused_even_from_this_pc(method, path):
    """DNS rebinding: un dominio ajeno que resuelve a 127.0.0.1 llega desde loopback pero con otro Host."""
    rebound = TestClient(main_module.app, base_url="http://ataque.example:8020", client=("127.0.0.1", 50000))

    resp = getattr(rebound, method)(path, **({"json": {}} if method in ("post", "put") else {}))

    assert resp.status_code == 403


def test_the_barrier_comes_before_anything_else_even_for_routes_that_do_not_exist():
    """El rechazo es lo PRIMERO: un pedido ajeno no llega ni a saber si la ruta existe (403, no 404) ni a validar su cuerpo (403, no 422)."""
    other = TestClient(main_module.app, base_url=BASE, client=("10.0.0.5", 50000))

    assert other.get("/api/vault/ruta-que-no-existe").status_code == 403
    assert other.put("/api/vault/general", content=b"no es json").status_code == 403


@pytest.mark.parametrize(
    "origin,site,ok",
    [
        (None, "none", True),  # el usuario abrio la URL a mano
        (BASE + "/", "same-origin", True),  # barra final
        ("http://localhost:8020", None, False),  # otro origen aunque sea local: no es ESTA pagina
        (None, "same-site", False),  # otra pagina del mismo sitio
    ],
)
def test_the_fine_print_of_the_page_check(origin, site, ok):
    problem = sa.local_page_problem("127.0.0.1", "127.0.0.1", origin, site, BASE)

    assert (problem is None) is ok


def test_vision_opened_as_localhost_is_its_own_page():
    assert sa.local_page_problem("127.0.0.1", "localhost", "http://localhost:8020", "same-origin", "http://localhost:8020") is None


def test_another_page_open_in_the_browser_is_refused(api):
    resp = api.get("/api/vault/connections", headers={"Origin": "https://pagina-maliciosa.example"})

    assert resp.status_code == 403 and "Otra página" in resp.json()["detail"]


def test_a_cross_site_fetch_is_refused_even_without_origin(api):
    assert api.get("/api/vault/connections", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_the_own_page_of_vision_gets_through(api):
    resp = api.get("/api/vault/connections", headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"})

    assert resp.status_code == 200 and resp.json() == {"connections": []}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004/004b/005/010: alta, nombre obligatorio, tarjetas sin secretos
# ---------------------------------------------------------------------------------------------------------------------
def test_a_connection_without_a_name_is_refused_and_nothing_is_saved(api):
    resp = api.post("/api/vault/connections", json={**oracle(), "name": "  "})

    assert resp.status_code == 422 and "Poné un nombre" in resp.json()["detail"]
    assert cs.list_stored() == [] and cs.is_active() is False


def test_the_card_shows_plain_fields_and_only_whether_secrets_are_set(api):
    resp = api.post("/api/vault/connections", json=oracle())

    card = resp.json()
    assert resp.status_code == 201 and card["display"] == "Oracle - Datamart" and card["default"] is True
    assert card["fields"]["oracle_user"] == "usuario1" and card["fields"]["oracle_host"] == "dmart.empresa.com"
    assert card["secrets"] == {"oracle_password": True} and "oracle_password" not in card["fields"]
    assert PASSWORD not in resp.text


def test_two_sharepoint_sites_are_two_cards_with_their_own_switch(api):
    sp = {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"}
    api.post("/api/vault/connections", json={"type": "sharepoint", "name": "Sitio Logistico", "fields": sp})
    api.post("/api/vault/connections", json={"type": "sharepoint", "name": "Material Industrial", "fields": sp})

    cards = api.get("/api/vault/connections").json()["connections"]

    assert [c["display"] for c in cards] == ["SharePoint - Sitio Logistico", "SharePoint - Material Industrial"]
    assert all(c["enabled"] is True for c in cards)


def test_the_list_never_carries_a_secret_value(api):
    api.post("/api/vault/connections", json=oracle())
    api.post("/api/vault/connections", json={"type": "confluence", "name": "Principal", "fields": {"confluence_email": "a@empresa.com", "confluence_api_token": "Token-Conf-9"}})

    text = api.get("/api/vault/connections").text

    assert PASSWORD not in text and "Token-Conf-9" not in text and "a@empresa.com" in text


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006/007b: editar; cambiar un secreto ya guardado exige desbloqueo
# ---------------------------------------------------------------------------------------------------------------------
def test_editing_plain_fields_needs_no_unlock(api):
    card = api.post("/api/vault/connections", json=oracle()).json()

    resp = api.put(f"/api/vault/connections/{card['id']}", json={"name": "Datamart Prod", "fields": {"oracle_host": "otro.empresa.com"}})

    assert resp.status_code == 200 and resp.json()["name"] == "Datamart Prod" and resp.json()["fields"]["oracle_host"] == "otro.empresa.com"


def test_changing_a_stored_password_while_locked_is_refused_and_nothing_changes(api):
    card = api.post("/api/vault/connections", json=oracle()).json()

    resp = api.put(f"/api/vault/connections/{card['id']}", json={"fields": {"oracle_password": "Nueva"}})

    assert resp.status_code == 423 and "desbloqueá" in resp.json()["detail"]
    assert cs.get_stored(card["id"]).fields["oracle_password"] == PASSWORD


def test_changing_a_stored_password_after_unlocking_works(api):
    card = api.post("/api/vault/connections", json=oracle()).json()
    unlock_ok()

    assert api.put(f"/api/vault/connections/{card['id']}", json={"fields": {"oracle_password": "Nueva"}}).status_code == 200
    assert cs.get_stored(card["id"]).fields["oracle_password"] == "Nueva"


def test_loading_a_password_for_the_first_time_needs_no_unlock(api):
    card = api.post("/api/vault/connections", json=oracle(oracle_password="")).json()

    assert api.put(f"/api/vault/connections/{card['id']}", json={"fields": {"oracle_password": "Primera"}}).status_code == 200


def test_editing_or_deleting_an_unknown_connection_is_404(api):
    assert api.put("/api/vault/connections/no-existe", json={"name": "X"}).status_code == 404
    assert api.delete("/api/vault/connections/no-existe").status_code == 404


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009: borrar olvida el interruptor
# ---------------------------------------------------------------------------------------------------------------------
def test_deleting_removes_the_card_and_forgets_its_switch(api):
    card = api.post("/api/vault/connections", json=oracle()).json()
    sw.set_connection_enabled("oracle:datamart", False)

    assert api.delete(f"/api/vault/connections/{card['id']}").status_code == 200

    assert cs.list_stored() == [] and "oracle:datamart" not in sw.disabled_connection_keys()


# ---------------------------------------------------------------------------------------------------------------------
# Probar: el resultado queda en la tarjeta
# ---------------------------------------------------------------------------------------------------------------------
def test_testing_records_the_result_on_the_card(api, monkeypatch):
    card = api.post("/api/vault/connections", json=oracle()).json()

    async def fake_test(type_id, name):
        return {"type": type_id, "connection": name, "ok": False, "detail": "No se pudo conectar."}

    monkeypatch.setattr(main_module, "test_one_connection", fake_test)

    resp = api.post(f"/api/vault/connections/{card['id']}/test")

    assert resp.json()["ok"] is False
    after = api.get("/api/vault/connections").json()["connections"][0]
    assert after["test_status"] == "error" and after["tested_at"] is not None


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007: el ojo (secreto guardado) solo desbloqueado
# ---------------------------------------------------------------------------------------------------------------------
def test_the_eye_is_refused_while_locked(api):
    card = api.post("/api/vault/connections", json=oracle()).json()

    resp = api.get(f"/api/vault/connections/{card['id']}/secret/oracle_password")

    assert resp.status_code == 423 and PASSWORD not in resp.text


def test_the_eye_shows_the_stored_password_once_unlocked_and_is_never_cached(api):
    card = api.post("/api/vault/connections", json=oracle()).json()
    unlock_ok()

    resp = api.get(f"/api/vault/connections/{card['id']}/secret/oracle_password")

    assert resp.status_code == 200 and resp.json() == {"value": PASSWORD}
    assert resp.headers["cache-control"] == "no-store"


def test_the_eye_only_reveals_secret_fields(api):
    card = api.post("/api/vault/connections", json=oracle()).json()
    unlock_ok()

    assert api.get(f"/api/vault/connections/{card['id']}/secret/oracle_user").status_code == 404
    assert api.get("/api/vault/connections/no-existe/secret/oracle_password").status_code == 404


def test_the_eye_is_refused_to_another_page_even_when_unlocked(api):
    card = api.post("/api/vault/connections", json=oracle()).json()
    unlock_ok()

    resp = api.get(f"/api/vault/connections/{card['id']}/secret/oracle_password", headers={"Origin": "https://pagina-maliciosa.example"})

    assert resp.status_code == 403 and PASSWORD not in resp.text


# ---------------------------------------------------------------------------------------------------------------------
# Desbloqueo
# ---------------------------------------------------------------------------------------------------------------------
def test_unlocking_without_windows_hello_available_is_a_clear_refusal(api):
    resp = api.post("/api/vault/unlock")

    assert resp.status_code == 403 and "Windows Hello" in resp.json()["detail"] and sa.is_unlocked() is False


@pytest.mark.parametrize("verifier", [lambda: False, lambda: 1 / 0])
def test_a_denied_or_failed_confirmation_never_unlocks(api, verifier):
    sa.set_verifier(verifier)

    assert api.post("/api/vault/unlock").status_code == 403 and sa.is_unlocked() is False


def test_unlocking_works_for_a_few_minutes_and_can_be_locked_again(api, monkeypatch):
    sa.set_verifier(lambda: True)
    now = [1_000_000.0]
    monkeypatch.setattr(sa.time, "time", lambda: now[0])

    until = api.post("/api/vault/unlock").json()["unlocked_until"]
    assert until == now[0] + sa.UNLOCK_SECONDS and api.get("/api/vault/unlock").json()["unlocked_until"] == until

    now[0] += sa.UNLOCK_SECONDS + 1
    assert api.get("/api/vault/unlock").json()["unlocked_until"] is None, "vence solo"

    api.post("/api/vault/unlock")
    api.delete("/api/vault/unlock")
    assert sa.is_unlocked() is False


# ---------------------------------------------------------------------------------------------------------------------
# Configuracion general
# ---------------------------------------------------------------------------------------------------------------------
def test_general_settings_are_saved_applied_and_never_show_secrets(api):
    resp = api.put("/api/vault/general", json={"values": {"fish_api_key": "Fish-Secreta", "llm_model": "modelo-x"}})

    assert resp.status_code == 200 and "Fish-Secreta" not in resp.text
    assert config.get_settings().llm_model == "modelo-x"
    rows = {r["key"]: r for r in api.get("/api/vault/general").json()["fields"]}
    assert rows["fish_api_key"]["set"] is True and "value" not in rows["fish_api_key"]


def test_overwriting_a_stored_general_secret_needs_unlock(api):
    api.put("/api/vault/general", json={"values": {"fish_api_key": "Fish-1"}})

    assert api.put("/api/vault/general", json={"values": {"fish_api_key": "Fish-2"}}).status_code == 423
    unlock_ok()
    assert api.put("/api/vault/general", json={"values": {"fish_api_key": "Fish-2"}}).status_code == 200
    assert api.get("/api/vault/general/secret/fish_api_key").json() == {"value": "Fish-2"}


def test_a_general_value_that_is_not_valid_is_a_clear_422(api):
    resp = api.put("/api/vault/general", json={"values": {"app_port": "ocho mil"}})

    assert resp.status_code == 422 and "número" in resp.json()["detail"]


def test_the_general_eye_is_refused_while_locked_and_for_non_secrets(api):
    api.put("/api/vault/general", json={"values": {"fish_api_key": "Fish-1", "fish_voice_id": "voz"}})

    assert api.get("/api/vault/general/secret/fish_api_key").status_code == 423
    unlock_ok()
    assert api.get("/api/vault/general/secret/fish_voice_id").status_code == 404


# ---------------------------------------------------------------------------------------------------------------------
# REQ-008: key de BigQuery
# ---------------------------------------------------------------------------------------------------------------------
def test_importing_the_bigquery_key_saves_it_encrypted_and_autocompletes_the_project(api):
    resp = api.post("/api/vault/bigquery-key", json={"key_json": json.dumps(KEY)})

    assert resp.status_code == 200 and resp.json() == {"project_id": "psapmpldash-e-s-110", "client_email": "sa@p.iam.gserviceaccount.com", "project_set": True}
    assert "CLAVE-PRIVADA-FALSA" not in resp.text
    assert json.loads(cs.get_general()["bq_key_json"]) == KEY and config.get_settings().bq_project_id == "psapmpldash-e-s-110"


def test_an_existing_project_is_not_overwritten_by_the_key(api):
    api.put("/api/vault/general", json={"values": {"bq_project_id": "mi-proyecto"}})

    assert api.post("/api/vault/bigquery-key", json={"key_json": json.dumps(KEY)}).json()["project_set"] is False
    assert cs.get_general()["bq_project_id"] == "mi-proyecto"


@pytest.mark.parametrize(
    "text",
    ["{ no es json", json.dumps(["lista"]), json.dumps({**KEY, "type": "authorized_user"}), json.dumps({k: v for k, v in KEY.items() if k != "private_key"}), "x" * 100_001],
    # ids cortos: pytest pone el id en una variable de entorno y Windows no admite una de 100 000 caracteres
    ids=["no-json", "lista", "otro-tipo", "sin-clave", "enorme"],
)
def test_something_that_is_not_a_service_account_key_is_refused(api, text):
    resp = api.post("/api/vault/bigquery-key", json={"key_json": text})

    assert resp.status_code == 422 and "bq_key_json" not in cs.get_general()


def test_replacing_a_stored_key_needs_unlock(api):
    api.post("/api/vault/bigquery-key", json={"key_json": json.dumps(KEY)})

    assert api.post("/api/vault/bigquery-key", json={"key_json": json.dumps({**KEY, "project_id": "otro"})}).status_code == 423
    unlock_ok()
    assert api.post("/api/vault/bigquery-key", json={"key_json": json.dumps({**KEY, "project_id": "otro"})}).status_code == 200


# ---------------------------------------------------------------------------------------------------------------------
# Resguardo: con archivos viejos sin importar, nada se escribe (si no, las conexiones del .env desaparecerian de golpe)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("legacy_file", [".env", "connections.json", ".env.enc"])
def test_with_old_files_not_imported_nothing_can_be_saved_and_the_store_stays_off(api, _isolated, legacy_file):
    (_isolated / legacy_file).write_text("ORACLE_HOST=h\n" if legacy_file == ".env" else "{}", encoding="utf-8")

    responses = [
        api.post("/api/vault/connections", json=oracle()),
        api.put("/api/vault/general", json={"values": {"fish_api_key": "x"}}),
        api.post("/api/vault/bigquery-key", json={"key_json": json.dumps(KEY)}),
    ]

    assert [r.status_code for r in responses] == [409, 409, 409]
    assert "importá" in responses[0].json()["detail"].lower()
    assert cs.is_active() is False and cs.list_stored() == [] and cs.get_general() == {}


def test_after_importing_the_old_files_new_connections_can_be_added(api, _isolated):
    (_isolated / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    api.post("/api/vault/import")

    resp = api.post("/api/vault/connections", json=oracle("Planta"))

    assert resp.status_code == 201 and {c.name for c in cs.list_stored()} == {"Datamart", "Planta"}, "la importada y la nueva conviven"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011 (Fase C): borrar los archivos viejos, solo despues de importar
# ---------------------------------------------------------------------------------------------------------------------
def write_all_legacy(folder):
    (folder / ".env").write_text("ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD=p\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")
    (folder / "connections.json").write_text('{"connections": []}', encoding="utf-8")
    (folder / ".env.enc").write_text("{}", encoding="utf-8")


def test_the_old_files_cannot_be_deleted_before_importing_them(api, _isolated):
    write_all_legacy(_isolated)

    resp = api.delete("/api/vault/legacy-files")

    assert resp.status_code == 409 and "importá" in resp.json()["detail"].lower()
    assert all((_isolated / n).exists() for n in (".env", "connections.json", ".env.enc")), "nada se borro"


def test_after_importing_the_old_files_are_deleted_and_vision_keeps_working(api, _isolated):
    write_all_legacy(_isolated)
    api.post("/api/vault/import")

    resp = api.delete("/api/vault/legacy-files")

    assert resp.status_code == 200 and sorted(resp.json()["deleted"]) == sorted([".env", "connections.json", ".env.enc"])
    assert not any((_isolated / n).exists() for n in (".env", "connections.json", ".env.enc"))
    assert [c.name for c in cs.list_stored()] == ["Datamart"], "la conexion importada sigue en la base"
    status = api.get("/api/vault/status").json()
    assert status["legacy"] == {"env": False, "connections_json": False, "env_enc": False, "bq_key_file": False} and status["active"] is True


def test_deleting_when_there_is_nothing_left_is_harmless(api):
    api.post("/api/vault/import")  # activa la base sin archivos

    resp = api.delete("/api/vault/legacy-files")

    assert resp.status_code == 200 and resp.json() == {"deleted": [], "problems": []}


def write_key_file(folder, data):
    (folder / "keys").mkdir(exist_ok=True)
    (folder / "keys" / "bigquery_key.json").write_text(json.dumps(data), encoding="utf-8")
    return folder / "keys" / "bigquery_key.json"


def test_the_bigquery_key_file_imported_into_vision_is_deleted_too(api, _isolated):
    """Revision de seguridad 2026-10-08: la key quedaba en texto plano en keys/ despues de importarla (la spec pide que no quede ningun secreto en claro)."""
    key_file = write_key_file(_isolated, KEY)
    api.post("/api/vault/import")
    assert api.get("/api/vault/status").json()["legacy"]["bq_key_file"] is True

    resp = api.delete("/api/vault/legacy-files")

    assert "la clave de BigQuery (.json)" in resp.json()["deleted"] and not key_file.exists()
    assert json.loads(cs.get_general()["bq_key_json"]) == KEY, "la key sigue, cifrada, en ULTRON"


def test_a_bigquery_key_file_different_from_the_stored_one_is_never_deleted(api, _isolated):
    api.post("/api/vault/import")  # sin archivos: la base queda activa y sin key
    api.post("/api/vault/bigquery-key", json={"key_json": json.dumps(KEY)})
    key_file = write_key_file(_isolated, {**KEY, "private_key": "OTRA-CLAVE"})

    resp = api.delete("/api/vault/legacy-files")

    assert key_file.exists() and any("no es igual" in p for p in resp.json()["problems"])


def test_a_key_file_alone_counts_as_something_to_import_first(api, _isolated):
    write_key_file(_isolated, KEY)

    assert api.post("/api/vault/connections", json=oracle()).status_code == 409


def test_every_vault_answer_is_marked_not_to_be_cached(api):
    for resp in (api.get("/api/vault/status"), api.get("/api/vault/connections"), api.get("/api/vault/general")):
        assert resp.headers["cache-control"] == "no-store"


def test_the_deletion_report_carries_names_not_paths_or_values(api, _isolated):
    write_all_legacy(_isolated)
    api.post("/api/vault/import")

    text = api.delete("/api/vault/legacy-files").text

    assert str(_isolated) not in text and "ORACLE_PASSWORD" not in text


# ---------------------------------------------------------------------------------------------------------------------
# Estado e importacion
# ---------------------------------------------------------------------------------------------------------------------
def test_the_status_lists_the_old_files_and_the_schema_without_values(api, _isolated):
    (_isolated / ".env").write_text(f"ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD={PASSWORD}\n", encoding="utf-8")

    data = api.get("/api/vault/status").json()

    assert data["active"] is False and data["legacy"]["env"] is True and data["unlocked_until"] is None
    oracle_type = next(t for t in data["types"] if t["type"] == "oracle")
    assert {"key": "oracle_password", "label": "Contrasena", "secret": True, "required": True, "structured": False} in oracle_type["fields"]
    assert PASSWORD not in json.dumps(data)


def test_importing_through_the_api_reports_names_only(api, _isolated):
    (_isolated / ".env").write_text(f"ORACLE_HOST=h\nORACLE_USER=u\nORACLE_PASSWORD={PASSWORD}\nORACLE_SERVICE_NAME=s\n", encoding="utf-8")

    resp = api.post("/api/vault/import")

    assert resp.status_code == 200 and resp.json()["connections"] == [{"display": "Oracle - Datamart", "result": "importada"}]
    assert PASSWORD not in resp.text and cs.is_active() is True
