"""«Probar» ANTES de guardar (pedido del usuario 2026-10-10: «todos deberian tener un boton de probar antes de guardar»).

`POST /api/vault/connections/test-draft` prueba lo que el usuario escribio en el formulario SIN guardar nada; al editar, lo que no toco (sobre todo
las contraseñas, que la pantalla no tiene) sale de la conexion guardada. `POST /api/vault/bigquery/test` prueba BigQuery con el proyecto y la
ubicacion escritos y la key guardada. Las dos pasan por la misma barrera del gestor (ver ROUTES en test_vault_api.py) y enmascaran las contraseñas.
Conectores falsos: nunca red. Base maestra en `tmp_path` y DPAPI real."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import config
from app import config_store as cs
from app import connections as conns
from app import connectors_status
from app import credentials
from app import main as main_module
from app import secret_access as sa
from app import secret_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

PASSWORD = "Clave-Datamart-77"
BASE = "http://127.0.0.1:8020"
FIELDS = {"oracle_user": "usuario1", "oracle_password": PASSWORD, "oracle_host": "dmart.empresa.com", "oracle_service_name": "HA_DMARTP1"}


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


@pytest.fixture
def oracle_seen(monkeypatch):
    """Oracle falso: anota con que datos lo probaron y conecta solo contra dmart.empresa.com."""
    import app.connectors.oracle_connector as oracle_module

    seen: list[dict] = []

    class Fake:
        def __init__(self, settings):
            self.settings = settings

        def test_connection(self):
            seen.append({"host": self.settings.oracle_host, "password": self.settings.oracle_password})
            if self.settings.oracle_host != "dmart.empresa.com":
                logging.getLogger("app.connectors.oracle_connector").warning(
                    "Oracle no disponible: ORA-01017 invalid username/password (%s)", self.settings.oracle_password
                )
                return False
            return True

    monkeypatch.setattr(oracle_module, "OracleConnector", Fake)
    return seen


def draft(api, fields, **extra):
    return api.post("/api/vault/connections/test-draft", json={"type": "oracle", "fields": fields, **extra})


def test_probar_tests_what_was_typed_and_saves_nothing(api, oracle_seen):
    resp = draft(api, FIELDS, name="Nueva")

    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert oracle_seen == [{"host": "dmart.empresa.com", "password": PASSWORD}]
    assert api.get("/api/vault/connections").json()["connections"] == [], "probar no guarda nada"
    assert PASSWORD not in resp.text


def test_editing_uses_the_stored_password_when_it_was_not_retyped(api, oracle_seen):
    card = api.post("/api/vault/connections", json={"type": "oracle", "name": "Datamart", "fields": FIELDS}).json()

    resp = draft(api, {"oracle_host": "otro.empresa.com"}, conn_id=card["id"])  # la pantalla no tiene la contraseña: no la manda

    assert resp.json()["ok"] is False
    assert oracle_seen == [{"host": "otro.empresa.com", "password": PASSWORD}], "usa la guardada sin mostrarla"
    assert cs.get_stored(card["id"]).fields["oracle_host"] == "dmart.empresa.com", "la guardada no cambia"


def test_a_retyped_password_wins_over_the_stored_one(api, oracle_seen):
    card = api.post("/api/vault/connections", json={"type": "oracle", "name": "Datamart", "fields": FIELDS}).json()

    draft(api, {"oracle_password": "Otra-Clave-Nueva"}, conn_id=card["id"])

    assert oracle_seen[-1]["password"] == "Otra-Clave-Nueva"
    assert cs.get_stored(card["id"]).fields["oracle_password"] == PASSWORD


def test_missing_required_fields_are_named_without_calling_the_system(api, oracle_seen):
    resp = draft(api, {k: v for k, v in FIELDS.items() if k != "oracle_host"})

    body = resp.json()
    assert body["ok"] is False and "Host" in body["detail"]
    assert oracle_seen == [], "no se llama al sistema"


def test_the_reason_says_why_and_never_shows_the_typed_password(api, oracle_seen):
    resp = draft(api, {**FIELDS, "oracle_host": "otro.empresa.com"})

    body = resp.json()
    assert body["ok"] is False and "credenciales" in body["detail"]
    assert PASSWORD not in resp.text


def test_fields_that_are_not_of_this_type_are_refused(api, oracle_seen):
    resp = draft(api, {**FIELDS, "no_existe": "x"})

    assert resp.status_code == 422 and oracle_seen == []


def test_editing_with_the_id_of_another_type_is_refused(api, oracle_seen):
    sp = {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"}
    card = api.post("/api/vault/connections", json={"type": "sharepoint", "name": "Sitio", "fields": sp}).json()

    assert draft(api, FIELDS, conn_id=card["id"]).status_code == 422
    assert draft(api, FIELDS, conn_id="no-existe").status_code == 404
    assert oracle_seen == []


# ---------------------------------------------------------------------------------------------------------------------
# BigQuery: el proyecto y la ubicacion escritos, con la key guardada
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def bq_seen(monkeypatch):
    seen: list[dict] = []

    class Fake:
        def __init__(self, settings):
            self.settings = settings

        def test_connection(self):
            seen.append({"project": self.settings.bq_project_id, "location": self.settings.bq_location})
            return self.settings.bq_project_id == "proyecto-bueno"

    monkeypatch.setattr(connectors_status, "BigQueryConnector", Fake)
    return seen


def test_probar_bigquery_uses_the_typed_project_and_saves_nothing(api, bq_seen):
    cs.set_general({"bq_project_id": "proyecto-guardado"})
    config.get_settings.cache_clear()

    resp = api.post("/api/vault/bigquery/test", json={"values": {"bq_project_id": "proyecto-bueno", "bq_location": "EU"}})

    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert bq_seen == [{"project": "proyecto-bueno", "location": "EU"}]
    assert cs.get_general()["bq_project_id"] == "proyecto-guardado", "probar no guarda"
    assert config.get_settings().bq_project_id != "proyecto-bueno", "ni cambia la configuracion en uso"


def test_probar_bigquery_without_changes_tests_what_is_saved(api, bq_seen):
    cs.set_general({"bq_project_id": "proyecto-bueno"})
    config.get_settings.cache_clear()

    assert api.post("/api/vault/bigquery/test", json={"values": {}}).json()["ok"] is True
    assert bq_seen[-1]["project"] == "proyecto-bueno"


def test_probar_bigquery_ignores_values_that_do_not_change_where_it_connects(api, bq_seen):
    resp = api.post("/api/vault/bigquery/test", json={"values": {"fish_api_key": "no-es-de-bigquery"}})

    assert resp.status_code == 200
    assert config.get_settings().fish_api_key != "no-es-de-bigquery"


def test_probar_bigquery_waits_the_bigquery_limit_not_the_general_one(api, monkeypatch):
    import time

    class Slow:
        def __init__(self, settings):
            pass

        def test_connection(self):
            time.sleep(0.5)
            return True

    monkeypatch.setattr(connectors_status, "CHECK_TIMEOUT", 0.1)
    monkeypatch.setattr(connectors_status, "BIGQUERY_CHECK_TIMEOUT", 3.0)
    monkeypatch.setattr(connectors_status, "BigQueryConnector", Slow)

    assert api.post("/api/vault/bigquery/test", json={"values": {}}).json()["ok"] is True
