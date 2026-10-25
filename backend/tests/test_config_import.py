"""Importar el `.env`, `connections.json` y `.env.enc` a la base cifrada (spec gestor-conexiones A6: REQ-003, REQ-004b, regla 6).

Cada test arma sus archivos viejos en `tmp_path` (BASE_DIR, connections.json y .env.enc apuntan ahi) y usa la base maestra temporal (autouse) con el DPAPI real."""

from __future__ import annotations

import json

import pytest

from app import config
from app import config_import as ci
from app import config_store as cs
from app import connections as conns
from app import credentials, secret_store
from app import switches as sw

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

ENV = """
ORACLE_USER=usuario1
ORACLE_PASSWORD=Clave-Oracle-1
ORACLE_HOST=dmartprod2.app.empresa.com
ORACLE_SERVICE_NAME=HA_DMARTP1
SHAREPOINT_SITE_URL=https://empresa.sharepoint.com/sites/log
AZURE_CLIENT_ID=cid-123
AZURE_CLIENT_SECRET=Secreto-Azure-2
AZURE_TENANT_ID=tid-456
SAP_BASE_URL=https://sap.empresa.com
SAP_USER=sapu
SAP_PASSWORD=Clave-Sap-3
CONFLUENCE_EMAIL=usuario1@empresa.com
CONFLUENCE_API_TOKEN=Token-Confluence-4
CONFLUENCE_ROOT_PAGE_ID=611718870
CONFLUENCE_ROOT_PAGE_NAME=MP&L Procedures
FISH_API_KEY=Clave-Fish-5
HTTPS_PROXY=http://proxy.empresa.com:83
APP_PORT=8021
BQ_PROJECT_ID=psapmpldash-e-s-110
ORACLE_CONEXION_NAME=Datamart
SHAREPOINT_CONEXION_NAME=
SAP_CONEXION_NAME=Con_guion_bajo_invalido
"""
SECRETS = ("Clave-Oracle-1", "Secreto-Azure-2", "Clave-Sap-3", "Token-Confluence-4", "Clave-Fish-5", "private_key")
KEY = {"type": "service_account", "project_id": "psapmpldash-e-s-110", "private_key": "x"}


@pytest.fixture(autouse=True)
def legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(tmp_path / ".env"))
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "APP_PORT", "FISH_API_KEY", "ENABLE_SAP", "ENABLE_BIGQUERY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_VISION_SET_ENV", {})
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()
    yield tmp_path
    conns.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()


def write_env(tmp_path, text=ENV):
    (tmp_path / ".env").write_text(text, encoding="utf-8")


def write_key(tmp_path, data=KEY):
    (tmp_path / "keys").mkdir(exist_ok=True)
    (tmp_path / "keys" / "bigquery_key.json").write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def stored():
    return {f"{c.type_id}:{c.name}": c for c in cs.list_stored()}


# ---------------------------------------------------------------------------------------------------------------------
# Importa todo, con nombre, y queda activa
# ---------------------------------------------------------------------------------------------------------------------
def test_everything_in_the_env_ends_up_encrypted_in_vision(legacy):
    write_env(legacy)
    write_key(legacy)

    report = ci.import_legacy()

    assert set(stored()) == {"oracle:Datamart", "sharepoint:Sitio Logistico", "sap:Principal", "confluence:Principal"}
    assert stored()["oracle:Datamart"].fields["oracle_password"] == "Clave-Oracle-1"
    assert all(c["result"] == "importada" for c in report.connections) and len(report.connections) == 4
    general = cs.get_general()
    assert general["fish_api_key"] == "Clave-Fish-5" and general["app_port"] == "8021" and general["bq_project_id"] == "psapmpldash-e-s-110"
    assert json.loads(general["bq_key_json"]) == KEY, "la key de BigQuery pasa a la base: el archivo deja de hacer falta"
    assert cs.is_active() is True


def test_a_connection_without_a_valid_name_gets_its_usual_name_never_none(legacy):
    write_env(legacy)

    ci.import_legacy()

    names = {c.type_id: c.name for c in cs.list_stored()}
    assert names["sharepoint"] == "Sitio Logistico", "SHAREPOINT_CONEXION_NAME vacio = el de siempre"
    assert names["sap"] == "Principal", "un nombre invalido tampoco deja la conexion sin nombre"


def test_confluence_keeps_its_root_page_as_a_section_and_its_personal_token(legacy):
    write_env(legacy)

    ci.import_legacy()

    conf = stored()["confluence:Principal"]
    assert conf.fields["confluence_api_token"] == "Token-Confluence-4"
    assert "611718870" in conf.fields["sections"] and "MP&L Procedures" in conf.fields["sections"]


def test_the_report_names_things_but_never_carries_a_value(legacy):
    write_env(legacy)
    write_key(legacy)

    text = json.dumps(ci.import_legacy().as_dict(), ensure_ascii=False)

    for secret in (*SECRETS, "dmartprod2", "proxy.empresa.com", "psapmpldash"):
        assert secret not in text, f"{secret} aparecio en el informe"
    assert "Oracle - Datamart" in text and "Clave de Fish Audio" in text


# ---------------------------------------------------------------------------------------------------------------------
# Se puede repetir: nunca duplica ni pisa
# ---------------------------------------------------------------------------------------------------------------------
def test_importing_twice_duplicates_nothing(legacy):
    write_env(legacy)
    ci.import_legacy()

    again = ci.import_legacy()

    assert len(cs.list_stored()) == 4
    assert {c["result"] for c in again.connections} == {"ya estaba"}
    assert again.general == [] and "Clave de Fish Audio" in again.general_kept


def test_importing_never_overwrites_what_the_user_already_saved(legacy):
    cs.add_connection("oracle", "Datamart", {"oracle_user": "u", "oracle_password": "La-Que-Cargo-El-Usuario", "oracle_host": "h", "oracle_service_name": "s"})
    cs.set_general({"fish_api_key": "Fish-De-La-Pantalla"})
    write_env(legacy)

    ci.import_legacy()

    assert stored()["oracle:Datamart"].fields["oracle_password"] == "La-Que-Cargo-El-Usuario"
    assert cs.get_general()["fish_api_key"] == "Fish-De-La-Pantalla"


# ---------------------------------------------------------------------------------------------------------------------
# connections.json y .env.enc
# ---------------------------------------------------------------------------------------------------------------------
def test_connections_json_blocks_are_imported_too_and_the_first_name_wins(legacy):
    write_env(legacy)
    (legacy / "connections.json").write_text(
        json.dumps({"connections": [
            {"type": "sharepoint", "name": "Material Industrial", "fields": {"sharepoint_site_url": "https://empresa.sharepoint.com/sites/mi"}},
            {"type": "oracle", "name": "Datamart", "fields": {"oracle_host": "del-archivo"}},
            {"type": "confluence", "name": "Calidad", "fields": {"confluence_base_url": "https://empresa.atlassian.net/wiki"}},
        ]}),
        encoding="utf-8",
    )

    report = ci.import_legacy()

    assert "sharepoint:Material Industrial" in stored() and "sharepoint:Sitio Logistico" in stored()
    assert stored()["oracle:Datamart"].fields["oracle_host"] == "del-archivo", "la del archivo llego primero"
    assert {"display": "Oracle - Datamart", "result": "ya estaba"} in report.connections
    assert stored()["confluence:Calidad"].fields["confluence_api_token"] == "Token-Confluence-4", "el token personal sale del .env, como en ejecucion"


def test_values_saved_encrypted_in_env_enc_are_imported(legacy, monkeypatch):
    monkeypatch.setattr(credentials, "_load_encrypted", lambda: {"bq_key_json": json.dumps(KEY), "elevenlabs_api_key": "Eleven-6", "bq_project_id": "p-enc"})

    ci.import_legacy()

    general = cs.get_general()
    assert json.loads(general["bq_key_json"]) == KEY and general["elevenlabs_api_key"] == "Eleven-6" and general["bq_project_id"] == "p-enc"


def test_a_huge_or_broken_key_file_is_reported_and_not_imported(legacy):
    write_env(legacy, "BQ_PROJECT_ID=p\n")
    write_key(legacy, "{ no es una clave")

    report = ci.import_legacy()

    assert "bq_key_json" not in cs.get_general() and any("BigQuery" in p for p in report.problems)


# ---------------------------------------------------------------------------------------------------------------------
# ENABLE_* -> interruptores; vale sin reiniciar
# ---------------------------------------------------------------------------------------------------------------------
def test_a_connection_the_env_had_off_is_imported_switched_off(legacy):
    write_env(legacy, ENV + "ENABLE_SAP=false\nENABLE_BIGQUERY=false\n")

    report = ci.import_legacy()

    assert sw.is_connection_enabled("sap", "Principal") is False
    assert sw.is_connection_enabled("bigquery", "bigquery") is False
    assert "SAP - Principal" in report.switched_off and sw.is_connection_enabled("oracle", "Datamart") is True


def test_after_importing_the_live_settings_no_longer_carry_the_env(legacy):
    write_env(legacy, ENV + "ENABLE_SAP=false\n")
    live = config.get_settings()
    assert live.oracle_host == "dmartprod2.app.empresa.com" and live.enable_sap is False  # antes: del .env

    ci.import_legacy()

    assert live.oracle_host == "" and live.enable_sap is True, "la MISMA instancia: el .env ya no pesa (el apagado de SAP es ahora un interruptor)"
    assert live.fish_api_key == "Clave-Fish-5" and live.app_port == 8021


# ---------------------------------------------------------------------------------------------------------------------
# Los archivos no se tocan; sin archivos tambien funciona; un valor raro no frena el resto
# ---------------------------------------------------------------------------------------------------------------------
def test_the_old_files_are_never_touched(legacy):
    write_env(legacy)
    write_key(legacy)
    before = {p: p.read_bytes() for p in (legacy / ".env", legacy / "keys" / "bigquery_key.json")}

    ci.import_legacy()

    assert all(p.read_bytes() == data for p, data in before.items())


def test_with_no_old_files_the_import_just_activates_the_store(legacy):
    report = ci.import_legacy()

    assert report.connections == [] and report.general == [] and cs.is_active() is True
    assert report.sources == {"env": False, "connections_json": False, "env_enc": False, "bq_key_file": False}


def test_an_invalid_value_is_reported_and_the_rest_is_imported(legacy):
    write_env(legacy, ENV.replace("APP_PORT=8021", "APP_PORT=ocho mil"))

    report = ci.import_legacy()

    assert "app_port" not in cs.get_general() and cs.get_general()["fish_api_key"] == "Clave-Fish-5"
    assert any("número" in p for p in report.problems) and len(cs.list_stored()) == 4
