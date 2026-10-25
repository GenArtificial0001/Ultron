"""API de conexiones con nombre (spec conexiones-con-nombre REQ-002/003/004/011).

Se prueba por HTTP contra la app real con un `connections.json` temporal. Lo que mas importa aca es REQ-004: NINGUNA respuesta contiene un valor de campo
(contrasena, secret, host, usuario...), se pruebe como se pruebe: se usa un secreto centinela y se busca en el texto crudo de cada respuesta."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import connections as conns
from app import main
from app.config import Settings

SENTINEL = "SECRETO-CENTINELA-77ab"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    base = Settings(_env_file=None)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(conns, "get_settings", lambda: base)
    conns.reset_for_tests()
    yield
    conns.reset_for_tests()


def write(tmp_path, blocks):
    (tmp_path / "connections.json").write_text(json.dumps({"connections": blocks}), encoding="utf-8")


def oracle(name, host="h-secreto-1", password=SENTINEL):
    return {"type": "oracle", "name": name, "fields": {"oracle_user": "usuario-secreto", "oracle_password": password, "oracle_host": host, "oracle_service_name": "svc"}}


@pytest.fixture
def client():
    return TestClient(main.app)


def test_the_list_shows_every_named_connection_with_its_display_name(client, tmp_path):
    write(tmp_path, [oracle("Datamart"), oracle("Planta")])

    data = client.get("/api/connections").json()

    assert [c["display"] for c in data["connections"] if c["type"] == "oracle"] == ["Oracle - Datamart", "Oracle - Planta"]
    assert data["error"] is None


def test_the_list_never_contains_a_field_value(client, tmp_path):
    write(tmp_path, [oracle("Datamart")])

    body = client.get("/api/connections").text

    for secret in (SENTINEL, "h-secreto-1", "usuario-secreto", "svc"):
        assert secret not in body, f"la respuesta de /api/connections filtro: {secret}"


def test_the_test_endpoint_does_not_echo_the_credentials_when_the_connection_fails(client, tmp_path, monkeypatch):
    """Un conector que falla puede meter la contrasena en el texto de su excepcion (pasa con los drivers que arman el DSN en el mensaje). El endpoint devuelve
    el `technical` al usuario que configura, asi que ahi NO puede viajar la contrasena."""
    write(tmp_path, [oracle("Datamart")])

    class Boom:
        def __init__(self, settings):
            self.pw = settings.oracle_password

        def test_connection(self):
            raise RuntimeError(f"ORA-01017 usuario/clave invalidos: usuario-secreto/{self.pw}@h-secreto-1")

    import app.connectors.oracle_connector as oc

    monkeypatch.setattr(oc, "OracleConnector", Boom)

    resp = client.post("/api/connections/oracle/test", params={"name": "Datamart"})

    assert resp.status_code == 200 and resp.json()["ok"] is False
    assert SENTINEL not in resp.text, "la contrasena viajo en la respuesta del test"


def test_a_connection_test_uses_the_credentials_of_the_requested_name(client, tmp_path, monkeypatch):
    write(tmp_path, [oracle("Datamart", host="h-datamart"), oracle("Planta", host="h-planta")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.oracle_host)

        def test_connection(self):
            return True

    import app.connectors.oracle_connector as oc

    monkeypatch.setattr(oc, "OracleConnector", Fake)

    resp = client.post("/api/connections/oracle/test", params={"name": "Planta"})

    assert resp.status_code == 200 and resp.json()["ok"] is True and seen == ["h-planta"]


def test_testing_without_a_name_uses_the_default_connection(client, tmp_path, monkeypatch):
    write(tmp_path, [oracle("Datamart", host="h-datamart"), oracle("Planta", host="h-planta")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.oracle_host)

        def test_connection(self):
            return True

    import app.connectors.oracle_connector as oc

    monkeypatch.setattr(oc, "OracleConnector", Fake)
    client.post("/api/connections/oracle/test")

    assert seen == ["h-datamart"]


def test_an_unknown_name_is_a_404_that_lists_the_valid_ones(client, tmp_path):
    write(tmp_path, [oracle("Datamart")])

    resp = client.post("/api/connections/oracle/test", params={"name": "Nada"})

    assert resp.status_code == 404 and "'Datamart'" in resp.json()["detail"]


def test_an_unknown_type_is_a_404(client):
    assert client.post("/api/connections/inventado/test").status_code == 404


def test_a_broken_file_reports_its_error_and_keeps_serving_the_last_good_connections(client, tmp_path):
    write(tmp_path, [oracle("Estable")])
    assert [c["name"] for c in client.get("/api/connections").json()["connections"]] == ["Estable"]

    (tmp_path / "connections.json").write_text('{"connections": [ {"type": ', encoding="utf-8")
    data = client.get("/api/connections").json()

    assert [c["name"] for c in data["connections"]] == ["Estable"]
    assert "no se pudo leer" in data["error"]


def test_editing_the_file_shows_up_in_the_next_request_without_restarting(client, tmp_path):
    write(tmp_path, [oracle("Uno")])
    assert len(client.get("/api/connections").json()["connections"]) == 1

    write(tmp_path, [oracle("Uno"), oracle("Dos")])

    assert [c["name"] for c in client.get("/api/connections").json()["connections"]] == ["Uno", "Dos"]


def test_the_list_advertises_the_registered_types_so_a_new_one_shows_up_without_frontend_changes(client, tmp_path):
    conns.register_type(conns.ConnectionType("jira", "Jira", (conns.ConnectionField("jira_url", "URL"),), tester=lambda s: True))
    try:
        types = {t["type"]: t for t in client.get("/api/connections").json()["types"]}
    finally:
        conns.unregister_type("jira")

    assert {"oracle", "sharepoint", "sap", "graph", "jira"} <= set(types) and types["jira"]["testable"] is True


def test_the_legacy_credentials_test_endpoint_uses_the_default_named_connection(client, tmp_path, monkeypatch):
    write(tmp_path, [oracle("Datamart", host="h-datamart")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.oracle_host)

        def test_connection(self):
            return True

    import app.connectors.oracle_connector as oc

    monkeypatch.setattr(oc, "OracleConnector", Fake)

    resp = client.post("/api/config/credentials/test/oracle")

    assert resp.status_code == 200 and seen == ["h-datamart"]


def sharepoint(name, site, secret=SENTINEL):
    return {
        "type": "sharepoint",
        "name": name,
        "fields": {"sharepoint_site_url": site, "azure_client_id": "cid-x", "azure_client_secret": secret, "azure_tenant_id": "tid-x"},
    }


def test_the_sharepoint_lists_come_from_the_requested_site(client, tmp_path, monkeypatch):
    """REQ-010: las listas de CADA SharePoint, no solo las del unico de antes."""
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log"), sharepoint("Sitio Calidad", "https://x/sites/cal")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.sharepoint_site_url)

        def list_lists(self):
            return ["Zeta", "Alfa"]

    import app.main as m

    monkeypatch.setattr(m, "SharePointConnector", Fake)

    resp = client.get("/api/config/connections/sharepoint/lists", params={"name": "Sitio Calidad"})

    assert resp.json() == {"lists": ["Alfa", "Zeta"], "connection": "Sitio Calidad"} and seen == ["https://x/sites/cal"]


def test_the_sharepoint_lists_without_a_name_use_the_default_site(client, tmp_path, monkeypatch):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log"), sharepoint("Sitio Calidad", "https://x/sites/cal")])
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.sharepoint_site_url)

        def list_lists(self):
            return []

    import app.main as m

    monkeypatch.setattr(m, "SharePointConnector", Fake)
    client.get("/api/config/connections/sharepoint/lists")

    assert seen == ["https://x/sites/log"]


def test_the_sharepoint_lists_with_an_unknown_name_are_a_404(client, tmp_path):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log")])

    resp = client.get("/api/config/connections/sharepoint/lists", params={"name": "Nada"})

    assert resp.status_code == 404 and "'Sitio Logistico'" in resp.json()["detail"]


def test_a_failure_listing_sharepoint_does_not_leak_the_secret(client, tmp_path, monkeypatch):
    write(tmp_path, [sharepoint("Sitio Logistico", "https://x/sites/log")])

    class Boom:
        def __init__(self, settings):
            self.secret = settings.azure_client_secret

        def list_lists(self):
            raise RuntimeError(f"AADSTS7000215: Invalid client secret {self.secret} para cid-x")

    import app.main as m

    monkeypatch.setattr(m, "SharePointConnector", Boom)

    resp = client.get("/api/config/connections/sharepoint/lists", params={"name": "Sitio Logistico"})

    assert resp.status_code == 502 and SENTINEL not in resp.text and "cid-x" not in resp.text


def test_the_file_path_shown_to_the_page_does_not_expose_the_windows_user_name(client, monkeypatch, tmp_path):
    """El `file` que viaja a la pantalla sale como `~/.vision/connections.json`, no como `C:/Users/<usuario>/...`."""
    home = tmp_path / "Users" / "nombre.apellido"
    monkeypatch.setattr(conns.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", home / ".vision" / "connections.json")

    body = client.get("/api/connections").json()

    assert body["file"] == "~/.vision/connections.json"
    assert "nombre.apellido" not in client.get("/api/connections").text


def test_a_file_outside_the_user_folder_is_shown_as_is(client, monkeypatch, tmp_path):
    monkeypatch.setattr(conns.Path, "home", classmethod(lambda cls: tmp_path / "otra-casa"))
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "fuera" / "c.json")

    assert client.get("/api/connections").json()["file"] == str(tmp_path / "fuera" / "c.json")
