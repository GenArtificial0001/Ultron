"""Conexiones desde la base cifrada (spec gestor-conexiones A3: REQ-003/004/010/012): cuando la base esta activa es la UNICA fuente; mientras no lo esta, todo como hoy.

Base maestra en `tmp_path` (autouse) y DPAPI real; el `.env` de cada test es un `Settings` armado a mano CON datos de Oracle/SharePoint, para probar que se ignoran."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config_store as cs
from app import connections as conns
from app import secret_store
from app import switches as sw
from app.config import Settings
from app.tools import dbquery as dq

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Un `.env` de hoy con Oracle y SharePoint completos y un connections.json con otra Oracle: con la base activa, NADA de esto debe aparecer."""
    env = Settings(
        _env_file=None,
        oracle_host="host-del-env", oracle_user="u-env", oracle_password="p-env", oracle_service_name="s",
        sharepoint_site_url="https://empresa.sharepoint.com/sites/del-env", azure_client_id="c", azure_client_secret="s", azure_tenant_id="t",
    )
    for target in (conns, dq):
        monkeypatch.setattr(target, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [{"type": "oracle", "name": "Del Archivo", "fields": {"oracle_host": "h", "oracle_user": "u"}}]}), encoding="utf-8")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", path)
    conns.reset_for_tests()
    cs.invalidate()
    yield env
    conns.reset_for_tests()
    cs.invalidate()


def sharepoint(site, secret="sec"):
    return {"sharepoint_site_url": site, "azure_client_id": "cid", "azure_client_secret": secret, "azure_tenant_id": "tid"}


def displays():
    return sorted(c.display for c in conns.list_connections())


# ---------------------------------------------------------------------------------------------------------------------
# REQ-012: inactiva = como hoy
# ---------------------------------------------------------------------------------------------------------------------
def test_while_the_store_is_inactive_the_env_and_the_file_work_as_always():
    assert "Oracle - Del Archivo" in displays() and "SharePoint - Sitio Logistico" in displays()


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: activa = unica fuente
# ---------------------------------------------------------------------------------------------------------------------
def test_once_active_only_the_connections_of_the_store_exist():
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("https://empresa.sharepoint.com/sites/mi"))

    assert displays() == ["Microsoft Graph - Principal", "SharePoint - Material Industrial"], "ni el .env ni connections.json se leen"


def test_the_file_errors_are_not_reported_once_the_store_is_active(monkeypatch, tmp_path):
    broken = tmp_path / "roto.json"
    broken.write_text("{ esto no es json", encoding="utf-8")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", broken)
    conns.reset_for_tests()
    assert conns.last_error() is not None

    cs.add_connection("oracle", "Datamart", {"oracle_host": "h", "oracle_user": "u", "oracle_password": "p", "oracle_service_name": "s"})

    assert conns.last_error() is None


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: varias del mismo tipo, cada una con SUS datos
# ---------------------------------------------------------------------------------------------------------------------
def test_two_sharepoint_sites_are_resolved_by_name_each_with_its_own_settings():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("https://empresa.sharepoint.com/sites/log", "sec-log"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("https://empresa.sharepoint.com/sites/mi", "sec-mi"))

    mi = conns.settings_for(conns.resolve("sharepoint", "material industrial"))
    default = conns.settings_for(conns.resolve("sharepoint"))

    assert (mi.sharepoint_site_url, mi.azure_client_secret) == ("https://empresa.sharepoint.com/sites/mi", "sec-mi")
    assert default.sharepoint_site_url.endswith("/log"), "sin nombre, la predeterminada (la primera)"


def test_a_tool_uses_the_settings_of_the_connection_it_names():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("https://empresa.sharepoint.com/sites/log"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("https://empresa.sharepoint.com/sites/mi"))

    settings, conn, error = dq._connection_context("sharepoint", {"connection": "Material Industrial"})

    assert error is None and conn.origin == "vision" and settings.sharepoint_site_url.endswith("/mi")


def test_a_store_connection_does_not_fall_back_to_the_env_values_it_does_not_have():
    cs.add_connection("oracle", "Datamart", {"oracle_user": "u-base", "oracle_password": "p-base", "oracle_service_name": "s"})

    settings = conns.settings_for(conns.resolve("oracle"))

    assert settings.oracle_host == "" and settings.oracle_user == "u-base", "el host del .env no se cuela en una conexion de la base"
    assert conns.missing_fields(conns.resolve("oracle")) == ["oracle_host"]


def test_each_connection_of_the_store_gets_its_own_switch():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("b"))

    sw.set_connection_enabled("sharepoint:material industrial", False)

    assert sw.enabled_names_for("sharepoint") == ["Sitio Logistico"]


# ---------------------------------------------------------------------------------------------------------------------
# Graph: la SharePoint que el usuario elige (regla 6)
# ---------------------------------------------------------------------------------------------------------------------
def test_graph_takes_the_credentials_of_the_default_sharepoint_unless_another_is_chosen():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a", "sec-log"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("b", "sec-mi"))
    assert conns.resolve("graph").fields["azure_client_secret"] == "sec-log"

    cs.set_general({"graph_source": "Material Industrial"})

    assert conns.resolve("graph").fields["azure_client_secret"] == "sec-mi"


# ---------------------------------------------------------------------------------------------------------------------
# Confluence: correo y token PROPIOS de cada conexion
# ---------------------------------------------------------------------------------------------------------------------
def confluence(email, token):
    return {"confluence_email": email, "confluence_api_token": token, "confluence_base_url": "https://empresa.atlassian.net/wiki"}


def test_each_confluence_of_the_store_uses_its_own_email_and_token():
    cs.add_connection("confluence", "Principal", confluence("a@empresa.com", "tok-principal"))
    cs.add_connection("confluence", "Calidad", confluence("b@empresa.com", "tok-calidad"))

    calidad = conns.settings_for(conns.resolve("confluence", "Calidad"))

    assert (calidad.confluence_email, calidad.confluence_api_token) == ("b@empresa.com", "tok-calidad")
    assert conns.missing_fields(conns.resolve("confluence", "Calidad")) == []


def test_a_confluence_without_its_token_lists_it_as_missing():
    cs.add_connection("confluence", "Principal", {"confluence_email": "a@empresa.com"})

    assert conns.missing_fields(conns.resolve("confluence")) == ["confluence_api_token"]


def test_the_token_of_a_store_connection_is_masked_in_an_error_text():
    cs.add_connection("confluence", "Principal", confluence("a@empresa.com", "tok-secreto-777"))

    text = conns.redact(conns.resolve("confluence"), "fallo con tok-secreto-777 para a@empresa.com")

    assert "tok-secreto-777" not in text and "a@empresa.com" not in text


# ---------------------------------------------------------------------------------------------------------------------
# REQ-010: la descripcion publica no lleva valores; lo ilegible se marca
# ---------------------------------------------------------------------------------------------------------------------
def test_the_public_description_says_where_it_comes_from_and_carries_no_values():
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("https://empresa.sharepoint.com/sites/mi", "sec-muy-secreto"))

    entries = conns.describe_all()
    sp = next(e for e in entries if e["type"] == "sharepoint")

    assert sp["source"] == "vision" and sp["unreadable"] is False and sp["status"] == "configured"
    assert "sec-muy-secreto" not in json.dumps(entries) and "sites/mi" not in json.dumps(entries)


def test_a_connection_from_another_pc_is_listed_as_unreadable_and_incomplete(tmp_master_db):
    from app.persistence import master_index

    master_index.insert_conn_store(
        {"id": "x1", "type_id": "oracle", "name": "De Otra PC", "is_default": True, "fields_blob": b"blob ajeno", "created_at": 1.0}
    )
    cs.activate()
    cs.invalidate()

    [entry] = [e for e in conns.describe_all() if e["type"] == "oracle"]

    assert entry["name"] == "De Otra PC" and entry["unreadable"] is True and entry["status"] == "incomplete"


def test_a_stored_type_that_is_no_longer_registered_is_skipped_without_breaking(monkeypatch):
    cs.add_connection("sap", "Principal", {"sap_base_url": "https://sap", "sap_user": "u", "sap_password": "p"})
    cs.add_connection("oracle", "Datamart", {"oracle_host": "h", "oracle_user": "u", "oracle_password": "p", "oracle_service_name": "s"})
    monkeypatch.setitem(conns._TYPES, "sap", conns._TYPES["sap"])  # restaura al terminar
    del conns._TYPES["sap"]

    assert [c.display for c in conns.list_connections()] == ["Oracle - Datamart"]


def test_inactive_store_keeps_the_env_flag_names_empty_of_vision_origin():
    """Mientras la base esta inactiva, ninguna conexion se marca como de la base."""
    assert all(c.origin == "" for c in conns.list_connections())


def test_connections_json_path_is_never_written(tmp_path):
    before = Path(conns.CONNECTIONS_FILE).read_bytes()

    cs.add_connection("oracle", "Datamart", {"oracle_host": "h", "oracle_user": "u", "oracle_password": "p", "oracle_service_name": "s"})
    conns.list_connections()

    assert Path(conns.CONNECTIONS_FILE).read_bytes() == before, "la base nunca escribe connections.json"
