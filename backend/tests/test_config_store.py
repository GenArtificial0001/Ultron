"""Almacen cifrado de conexiones y configuracion general en `vision.db` (spec gestor-conexiones REQ-001/003/004/004b/005/009/012).

Base maestra en `tmp_path` (fixture autouse `tmp_master_db`) y el DPAPI REAL de la PC: lo que se prueba es lo que queda en disco, no un mock del cifrado."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app import config_store as cs
from app import connections as conns
from app import secret_store
from app.persistence import master_index

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

PASSWORD = "Clave-Datamart-9917"


@pytest.fixture(autouse=True)
def _fresh():
    cs.invalidate()
    conns.reset_for_tests()
    yield
    cs.invalidate()


def oracle_fields(**over):
    base = {"oracle_user": "usuario1", "oracle_password": PASSWORD, "oracle_host": "dmart.empresa.com", "oracle_service_name": "HA_DMARTP1"}
    base.update(over)
    return base


def raw_rows(db):
    """Filas tal como quedaron en disco. Si nunca se escribio nada, la base (o la tabla) ni existe: eso tambien es «nada guardado»."""
    if not db.exists():
        return []
    # closing(): `with sqlite3.connect()` confirma pero NO cierra la conexion (ResourceWarning en la bateria v25)
    with closing(sqlite3.connect(db)) as c:
        try:
            return c.execute("SELECT type_id, name, fields_blob FROM conn_store").fetchall()
        except sqlite3.OperationalError:
            return []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-012 / REQ-003: inactivo hasta la primera carga
# ---------------------------------------------------------------------------------------------------------------------
def test_a_fresh_install_has_the_store_inactive_and_empty():
    assert cs.is_active() is False and cs.list_stored() == [] and cs.get_general() == {}


def test_the_first_connection_activates_the_store():
    cs.add_connection("oracle", "Datamart", oracle_fields())

    assert cs.is_active() is True


def test_an_unreadable_master_database_counts_as_inactive(monkeypatch):
    def boom(key):
        raise OSError("disco")

    monkeypatch.setattr(master_index, "get_app_setting", boom)

    assert cs.is_active() is False


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: cifrado en disco
# ---------------------------------------------------------------------------------------------------------------------
def test_a_saved_connection_comes_back_with_all_its_fields(tmp_master_db):
    cs.add_connection("oracle", "Datamart", oracle_fields())

    [c] = cs.list_stored()
    assert (c.type_id, c.name, c.default) == ("oracle", "Datamart", True)
    assert c.fields == oracle_fields() and c.undecryptable is False


def test_on_disk_only_type_and_name_are_readable_the_fields_are_encrypted(tmp_master_db):
    cs.add_connection("oracle", "Datamart", oracle_fields())

    [(type_id, name, blob)] = raw_rows(tmp_master_db)
    assert (type_id, name) == ("oracle", "Datamart")
    for secret in (PASSWORD, "usuario1", "dmart.empresa.com", "oracle_password"):
        assert secret.encode() not in bytes(blob), f"{secret} quedo legible en la base"


def test_the_general_settings_are_encrypted_on_disk_too(tmp_master_db):
    cs.set_general({"elevenlabs_api_key": "sk-voz-123456", "https_proxy": "http://proxy:83"})

    with closing(sqlite3.connect(tmp_master_db)) as c:
        [(blob,)] = c.execute("SELECT value_blob FROM secure_settings").fetchall()
    assert b"sk-voz-123456" not in bytes(blob) and b"elevenlabs" not in bytes(blob)
    assert cs.get_general() == {"elevenlabs_api_key": "sk-voz-123456", "https_proxy": "http://proxy:83"}


def test_a_row_this_windows_user_cannot_decrypt_is_listed_without_data_and_without_crashing(tmp_master_db):
    master_index.insert_conn_store(
        {"id": "x1", "type_id": "oracle", "name": "De Otra PC", "is_default": True, "fields_blob": b"blob de otra pc", "created_at": 1.0}
    )

    [c] = cs.list_stored()
    assert c.name == "De Otra PC" and c.undecryptable is True and c.fields == {}


def test_reloading_a_connection_that_could_not_be_decrypted_replaces_all_its_fields(tmp_master_db):
    master_index.insert_conn_store(
        {"id": "x1", "type_id": "oracle", "name": "De Otra PC", "is_default": True, "fields_blob": b"blob de otra pc", "created_at": 1.0}
    )

    c = cs.update_connection("x1", fields=oracle_fields())

    assert c.undecryptable is False and c.fields == oracle_fields()


def test_an_unreadable_general_blob_means_empty_general_settings(tmp_master_db):
    master_index.set_secure_setting(cs.GENERAL_KEY, b"ilegible")

    assert cs.get_general() == {}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004b: nombre obligatorio
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_connection_without_a_name_is_never_saved(tmp_master_db, name):
    with pytest.raises(cs.ConfigStoreError) as err:
        cs.add_connection("oracle", name, oracle_fields())

    assert "Poné un nombre" in str(err.value), "el aviso dice QUE falta (el nombre), no un «nombre no valido» generico"
    assert raw_rows(tmp_master_db) == [] and cs.is_active() is False, "nada guardado y el almacen sigue inactivo"


@pytest.mark.parametrize("name", ["Con_guion_bajo", 'Con "comillas"', " Espacio al inicio", "x" * 61, "Llave{"])
def test_an_invalid_name_is_refused(name):
    with pytest.raises(cs.ConfigStoreError):
        cs.add_connection("oracle", name, oracle_fields())


def test_renaming_to_an_empty_name_is_refused():
    c = cs.add_connection("oracle", "Datamart", oracle_fields())

    with pytest.raises(cs.ConfigStoreError):
        cs.update_connection(c.id, name="  ")
    assert cs.get_stored(c.id).name == "Datamart"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: varias del mismo tipo
# ---------------------------------------------------------------------------------------------------------------------
def sharepoint(site):
    return {"sharepoint_site_url": site, "azure_client_id": "cid", "azure_client_secret": "sec", "azure_tenant_id": "tid"}


def test_two_sharepoint_sites_live_side_by_side_each_with_its_own_data():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("https://empresa.sharepoint.com/sites/log"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("https://empresa.sharepoint.com/sites/mi"))

    by_name = {c.name: c for c in cs.list_stored()}
    assert by_name["Material Industrial"].fields["sharepoint_site_url"].endswith("/mi")
    assert by_name["Sitio Logistico"].default is True and by_name["Material Industrial"].default is False


def test_names_are_unique_per_type_ignoring_case():
    cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a"))

    with pytest.raises(cs.ConfigStoreError):
        cs.add_connection("sharepoint", "sitio logistico", sharepoint("b"))


def test_the_same_name_in_another_type_is_fine():
    cs.add_connection("sharepoint", "Principal", sharepoint("a"))
    cs.add_connection("sap", "Principal", {"sap_base_url": "https://sap", "sap_user": "u", "sap_password": "p"})

    assert len(cs.list_stored()) == 2


def test_there_is_a_cap_per_type():
    for i in range(conns.MAX_PER_TYPE):
        cs.add_connection("sap", f"Sap {i}", {"sap_base_url": "https://sap"})

    with pytest.raises(cs.ConfigStoreError):
        cs.add_connection("sap", "Una mas", {"sap_base_url": "https://sap"})


def test_marking_another_one_as_default_moves_the_default():
    first = cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a"))
    second = cs.add_connection("sharepoint", "Material Industrial", sharepoint("b"), default=True)

    assert cs.get_stored(second.id).default is True and cs.get_stored(first.id).default is False


# ---------------------------------------------------------------------------------------------------------------------
# Validacion de tipo y campos (nunca se guarda una cadena arbitraria)
# ---------------------------------------------------------------------------------------------------------------------
def test_an_unknown_type_is_refused():
    with pytest.raises(cs.ConfigStoreError):
        cs.add_connection("mongodb", "X", {})


def test_graph_has_no_data_of_its_own():
    with pytest.raises(cs.ConfigStoreError) as err:
        cs.add_connection("graph", "Principal", {})

    assert "credenciales de otra" in str(err.value)


def test_a_field_that_is_not_of_the_type_is_refused():
    with pytest.raises(cs.ConfigStoreError) as err:
        cs.add_connection("oracle", "Datamart", {**oracle_fields(), "sap_password": "x"})

    assert "sap_password" in str(err.value)


def test_confluence_keeps_its_personal_email_and_token_inside_the_connection():
    c = cs.add_connection("confluence", "Principal", {"confluence_email": "a@empresa.com", "confluence_api_token": "tok-1", "confluence_base_url": "https://empresa.atlassian.net/wiki"})

    assert c.fields["confluence_api_token"] == "tok-1"


def test_a_huge_value_is_refused():
    with pytest.raises(cs.ConfigStoreError):
        cs.add_connection("oracle", "Datamart", oracle_fields(oracle_host="h" * (cs.MAX_VALUE_CHARS + 1)))


def test_error_messages_carry_no_values():
    with pytest.raises(cs.ConfigStoreError) as err:
        cs.add_connection("oracle", "Datamart", {**oracle_fields(), "campo_raro": PASSWORD})

    assert PASSWORD not in str(err.value)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006 editar / REQ-009 eliminar / REQ-005 resultado de la prueba
# ---------------------------------------------------------------------------------------------------------------------
def test_editing_changes_only_what_comes_and_none_removes_a_field():
    c = cs.add_connection("oracle", "Datamart", oracle_fields(oracle_schema="DM"))

    after = cs.update_connection(c.id, fields={"oracle_password": "Nueva-1", "oracle_schema": None})

    assert after.fields["oracle_password"] == "Nueva-1" and after.fields["oracle_user"] == "usuario1"
    assert "oracle_schema" not in after.fields


def test_renaming_keeps_the_fields_and_refuses_a_name_already_taken():
    a = cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a"))
    cs.add_connection("sharepoint", "Material Industrial", sharepoint("b"))

    assert cs.update_connection(a.id, name="Sitio Logistico la planta").fields["sharepoint_site_url"] == "a"
    with pytest.raises(cs.ConfigStoreError):
        cs.update_connection(a.id, name="material industrial")


def test_changing_only_the_case_of_the_own_name_is_allowed():
    a = cs.add_connection("sharepoint", "Sitio Logistico", sharepoint("a"))

    assert cs.update_connection(a.id, name="Sitio logistico").name == "Sitio logistico"


def test_editing_or_deleting_something_that_does_not_exist_is_a_clear_error():
    with pytest.raises(cs.ConfigStoreError):
        cs.update_connection("no-existe", fields={})
    with pytest.raises(cs.ConfigStoreError):
        cs.delete_connection("no-existe")


def test_deleting_removes_it_and_returns_what_was_deleted(tmp_master_db):
    c = cs.add_connection("oracle", "Datamart", oracle_fields())

    gone = cs.delete_connection(c.id)

    assert gone.name == "Datamart" and cs.list_stored() == [] and raw_rows(tmp_master_db) == []


def test_the_test_result_is_recorded_for_the_card():
    c = cs.add_connection("oracle", "Datamart", oracle_fields())

    cs.record_test(c.id, ok=False)
    after = cs.get_stored(c.id)

    assert after.test_status == "error" and after.tested_at is not None
    cs.record_test(c.id, ok=True)
    assert cs.get_stored(c.id).test_status == "ok"


# ---------------------------------------------------------------------------------------------------------------------
# Configuracion general
# ---------------------------------------------------------------------------------------------------------------------
def test_general_settings_merge_partially_and_none_removes():
    cs.set_general({"https_proxy": "http://p:83", "fish_api_key": "k1"})

    cs.set_general({"fish_api_key": None, "app_port": "8021"})

    assert cs.get_general() == {"https_proxy": "http://p:83", "app_port": "8021"}


def test_saving_general_settings_activates_the_store():
    cs.set_general({"app_port": "8020"})

    assert cs.is_active() is True


def test_what_another_process_saves_shows_up_after_the_short_cache_expires(monkeypatch):
    """dev y prod comparten la base (decision del usuario): lo que guarda el OTRO proceso se ve en segundos, sin reiniciar."""
    now = [1000.0]
    monkeypatch.setattr(cs.time, "monotonic", lambda: now[0])
    cs.add_connection("oracle", "Datamart", oracle_fields())
    assert [c.name for c in cs.list_stored()] == ["Datamart"]

    master_index.insert_conn_store(  # escritura de OTRO proceso: no pasa por este config_store ni invalida su cache
        {"id": "otro", "type_id": "oracle", "name": "Planta", "is_default": False, "fields_blob": secret_store.encrypt_json({}), "created_at": 2e9}
    )
    assert [c.name for c in cs.list_stored()] == ["Datamart"], "dentro del vencimiento se usa la memoria"

    now[0] += cs._CACHE_TTL_S + 0.1

    assert [c.name for c in cs.list_stored()] == ["Datamart", "Planta"]


def test_own_writes_are_seen_immediately_without_waiting_for_the_cache(monkeypatch):
    monkeypatch.setattr(cs.time, "monotonic", lambda: 5.0)  # el reloj no avanza: solo la invalidacion explica que se vea
    cs.add_connection("oracle", "Datamart", oracle_fields())
    cs.list_stored()

    cs.add_connection("oracle", "Planta", oracle_fields())

    assert len(cs.list_stored()) == 2 and cs.is_active() is True


def test_activating_is_seen_at_once_even_with_the_inactive_state_cached(monkeypatch):
    """La importacion (A6) llama a `activate()` sola: tiene que verse en el acto, sin esperar el vencimiento del cache."""
    monkeypatch.setattr(cs.time, "monotonic", lambda: 7.0)
    assert cs.is_active() is False  # queda cacheado como inactivo

    cs.activate()

    assert cs.is_active() is True


def test_what_is_saved_is_read_back_after_forgetting_the_memory_cache():
    cs.add_connection("oracle", "Datamart", oracle_fields())
    cs.set_general({"app_port": "8021"})

    cs.invalidate()

    assert cs.list_stored()[0].fields == oracle_fields() and cs.get_general() == {"app_port": "8021"}
