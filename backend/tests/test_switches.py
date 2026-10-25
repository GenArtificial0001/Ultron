"""Nucleo de interruptores de conexiones y skills (spec interruptores-conexiones-skills REQ-001/002/004/005/009/011).

Se usa una base maestra TEMPORAL (nunca el `~/.vision/vision.db` real). Lo que se verifica, por orden de riesgo:
  * un valor guardado roto o inesperado NUNCA apaga nada (se asume todo activo);
  * lo que llega del cliente se valida contra lo REAL: no se guarda una cadena arbitraria;
  * el estado sobrevive a «reiniciar» (se vuelve a leer de la base) y una instalacion sin claves queda toda activa;
  * una tool solo se quita si TODAS las conexiones de su tipo estan apagadas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import connections as conns
from app import switches as sw
from app.config import Settings
from app.persistence import master_index


def _base(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Base maestra temporal + sin archivo de conexiones + conexiones armadas desde un `.env` falso."""
    store: dict[str, str] = {}
    monkeypatch.setattr(master_index, "get_app_setting", lambda k: store.get(k))
    monkeypatch.setattr(master_index, "set_app_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(master_index, "unset_app_setting", lambda k: store.pop(k, None))
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    env = _base(
        oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s",
        sharepoint_site_url="https://x/sites/l", azure_client_id="c", azure_client_secret="s", azure_tenant_id="t",
        sap_base_url="https://sap", sap_user="su", sap_password="sp",
        confluence_email="a@b.com", confluence_api_token="tok-123456",
        bq_project_id="proyecto-bq",
    )
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    conns.reset_for_tests()
    monkeypatch.setattr(sw, "_store", store, raising=False)
    yield store
    conns.reset_for_tests()


def write_connections(monkeypatch, tmp_path, blocks):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": blocks}), encoding="utf-8")
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", path)
    conns.reset_for_tests()


def oracle_block(name):
    return {"type": "oracle", "name": name, "fields": {"oracle_user": "u", "oracle_password": "p", "oracle_host": "h", "oracle_service_name": "s"}}


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005 arranque como hoy
# ---------------------------------------------------------------------------------------------------------------------
def test_a_fresh_install_has_everything_enabled():
    assert all(c["enabled"] for c in sw.describe_switches())
    assert sw.disabled_connection_keys() == set() and sw.disabled_skill_names() == set()


def test_every_configured_connection_is_listed_including_bigquery():
    keys = {c["key"] for c in sw.describe_switches()}

    assert {"oracle:datamart", "sharepoint:sitio logistico", "sap:principal", "confluence:principal", "bigquery:bigquery"} <= keys


def test_a_new_connection_with_no_saved_key_is_enabled(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Planta")])

    assert sw.is_connection_enabled("oracle", "Planta") is True


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001 / REQ-004 apagar, prender y persistir
# ---------------------------------------------------------------------------------------------------------------------
def test_turning_a_connection_off_and_on_again():
    sw.set_connection_enabled("confluence:principal", False)
    assert sw.is_connection_enabled("confluence", "Principal") is False

    sw.set_connection_enabled("confluence:principal", True)
    assert sw.is_connection_enabled("confluence", "Principal") is True


def test_the_switch_is_stored_in_the_global_app_settings_not_in_memory(_isolated):
    sw.set_connection_enabled("oracle:datamart", False)

    assert json.loads(_isolated[sw.CONNECTIONS_DISABLED_KEY]) == ["oracle:datamart"]


def test_the_state_survives_a_restart_because_it_is_reread_from_the_store(_isolated):
    sw.set_connection_enabled("sap:principal", False)
    snapshot = dict(_isolated)  # lo unico que sobrevive a un reinicio es la base

    _isolated.clear()
    _isolated.update(snapshot)

    assert sw.is_connection_enabled("sap", "Principal") is False


def test_only_what_is_off_is_stored_and_turning_everything_on_leaves_no_key(_isolated):
    sw.set_connection_enabled("sap:principal", False)
    sw.set_connection_enabled("sap:principal", True)

    assert sw.CONNECTIONS_DISABLED_KEY not in _isolated, "sin nada apagado no queda ninguna clave"


def test_turning_one_off_does_not_touch_the_others():
    sw.set_connection_enabled("oracle:datamart", False)

    states = {c["key"]: c["enabled"] for c in sw.describe_switches()}

    assert states["oracle:datamart"] is False and states["sap:principal"] is True and states["confluence:principal"] is True


def test_the_key_is_case_insensitive_by_construction():
    assert sw.connection_key("Oracle", "Datamart") == sw.connection_key("oracle", "datamart") == "oracle:datamart"


def test_each_connection_of_the_same_type_has_its_own_switch(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])

    sw.set_connection_enabled("oracle:planta", False)

    assert sw.is_connection_enabled("oracle", "Datamart") is True and sw.is_connection_enabled("oracle", "Planta") is False


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011 validacion: no se guarda lo que manda el cliente sin validar
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "oracle", "oracle:inexistente", "'; DROP TABLE app_settings; --", "../../etc/passwd", "x" * 5000, "oracle:datamart\n", "ORACLE:DATAMART "])
def test_an_unknown_key_is_refused_and_nothing_is_stored(bad, _isolated):
    with pytest.raises(ValueError):
        sw.set_connection_enabled(bad, False)

    assert sw.CONNECTIONS_DISABLED_KEY not in _isolated


def test_describe_never_carries_credentials():
    dumped = json.dumps(sw.describe_switches(), ensure_ascii=False)

    for secret in ("tok-123456", "a@b.com", "https://sap", "https://x/sites/l", "proyecto-bq", "oracle_password", "azure_client_secret"):
        assert secret not in dumped, f"salio un dato de la conexion: {secret}"
    assert set(sw.describe_switches()[0]) == {"key", "type", "name", "display", "enabled", "env_disabled"}


# ---------------------------------------------------------------------------------------------------------------------
# Un valor guardado roto NUNCA apaga nada
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("garbage", ["no es json", "{\"a\": 1}", "42", "null", "", "[\"oracle:datamart\""])
def test_a_corrupt_stored_value_is_treated_as_everything_enabled(garbage, _isolated):
    _isolated[sw.CONNECTIONS_DISABLED_KEY] = garbage

    assert all(c["enabled"] for c in sw.describe_switches())


def test_a_list_with_non_text_items_keeps_only_the_text_ones(_isolated):
    """`[1, 2, {...}, "oracle:datamart"]`: lo que no es texto se descarta y lo valido sigue funcionando (no se apaga nada por error)."""
    _isolated[sw.CONNECTIONS_DISABLED_KEY] = json.dumps([1, 2, {"x": 3}, "oracle:datamart"])

    states = {c["key"]: c["enabled"] for c in sw.describe_switches()}

    assert states["oracle:datamart"] is False and states["sap:principal"] is True


def test_an_unreadable_master_database_means_everything_enabled(monkeypatch):
    def boom(key):
        raise RuntimeError("base maestra no disponible")

    monkeypatch.setattr(master_index, "get_app_setting", boom)

    assert sw.disabled_connection_keys() == set() and all(c["enabled"] for c in sw.describe_switches())


def test_the_stored_list_is_capped_so_it_cannot_grow_forever(_isolated):
    _isolated[sw.CONNECTIONS_DISABLED_KEY] = json.dumps([f"x:{i}" for i in range(5000)])

    assert len(sw.disabled_connection_keys()) <= 500


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002 una tool solo se quita si TODAS las conexiones de su tipo estan apagadas
# ---------------------------------------------------------------------------------------------------------------------
def test_with_nothing_off_every_tool_is_available():
    assert all(sw.tool_is_available(name) for name in sw.TOOL_CONNECTION_TYPE)


def test_turning_confluence_off_removes_all_its_tools_and_only_them():
    sw.set_connection_enabled("confluence:principal", False)

    for name, type_id in sw.TOOL_CONNECTION_TYPE.items():
        assert sw.tool_is_available(name) is (type_id != "confluence"), name


def test_with_two_oracle_and_one_off_the_oracle_tool_stays(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    sw.set_connection_enabled("oracle:planta", False)

    assert sw.tool_is_available("oracle_query") is True and sw.enabled_names_for("oracle") == ["Datamart"]


def test_with_two_oracle_and_both_off_the_oracle_tool_goes_away(monkeypatch, tmp_path):
    write_connections(monkeypatch, tmp_path, [oracle_block("Datamart"), oracle_block("Planta")])
    sw.set_connection_enabled("oracle:planta", False)
    sw.set_connection_enabled("oracle:datamart", False)

    assert sw.tool_is_available("oracle_query") is False and sw.enabled_names_for("oracle") == []


def test_tools_that_are_not_connections_are_never_filtered():
    sw.set_connection_enabled("confluence:principal", False)

    for name in ("read", "write", "bash", "glob", "grep", "webfetch", "question", "skill", "todowrite"):
        assert sw.tool_is_available(name) is True


def test_a_type_with_no_connections_configured_is_not_filtered_even_if_something_else_is_off(monkeypatch, tmp_path):
    """Sin ninguna conexion de SAP configurada, `sap_query` conserva su comportamiento de siempre (explicar que falta configurar): apagar OTRA cosa no la afecta."""
    env = _base(oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s")
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    conns.reset_for_tests()
    sw.set_connection_enabled("oracle:datamart", False)

    assert sw.tool_is_available("sap_query") is True


def test_the_env_switch_also_takes_the_tools_away_even_if_the_user_left_it_on(monkeypatch):
    off = _base(
        enable_confluence=False, confluence_email="a@b.com", confluence_api_token="tok-123456",
        oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s",
    )
    monkeypatch.setattr(conns, "get_settings", lambda: off)
    monkeypatch.setattr("app.config.get_settings", lambda: off)
    conns.reset_for_tests()

    flags = {c["key"]: c["env_disabled"] for c in sw.describe_switches()}

    assert flags["confluence:principal"] is True and sw.tool_is_available("confluence_search") is True
    sw.set_connection_enabled("oracle:datamart", False)  # con algo apagado, el filtro se activa y el `.env` tambien cuenta
    assert sw.tool_is_available("confluence_search") is False


def test_every_mapped_tool_exists_in_the_registry():
    from app.tools import get_registry

    registry = get_registry()
    assert [n for n in sw.TOOL_CONNECTION_TYPE if registry.get(n) is None] == []


def test_every_connection_tool_of_the_registry_is_mapped():
    """Una tool de conexion nueva que se olvide de mapear nunca se filtraria: este test obliga a mapearla."""
    from app.tools import get_registry

    connection_tools = {t.name for t in get_registry().all() if "connection" in t.parameters.get("properties", {})}

    assert connection_tools <= set(sw.TOOL_CONNECTION_TYPE), connection_tools - set(sw.TOOL_CONNECTION_TYPE)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003 defensa en profundidad
# ---------------------------------------------------------------------------------------------------------------------
def test_asserting_a_disabled_connection_raises_a_clear_message_without_any_data():
    sw.set_connection_enabled("confluence:principal", False)

    with pytest.raises(sw.DisabledConnectionError) as err:
        sw.assert_connection_enabled("confluence", "Principal")

    text = str(err.value)
    assert "desactivada" in text and "Principal" in text and "tok-123456" not in text and "a@b.com" not in text


def test_asserting_an_enabled_connection_does_nothing():
    sw.assert_connection_enabled("oracle", "Datamart")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009 skills
# ---------------------------------------------------------------------------------------------------------------------
KNOWN = {"auditoria", "slotting", "Mi Skill 2"}


def test_a_skill_is_enabled_until_turned_off(_isolated):
    assert sw.is_skill_enabled("slotting") is True

    sw.set_skill_enabled("slotting", False, KNOWN)

    assert sw.is_skill_enabled("slotting") is False and json.loads(_isolated[sw.SKILLS_DISABLED_KEY]) == ["slotting"]


def test_turning_a_skill_back_on_removes_the_key(_isolated):
    sw.set_skill_enabled("slotting", False, KNOWN)
    sw.set_skill_enabled("slotting", True, KNOWN)

    assert sw.SKILLS_DISABLED_KEY not in _isolated and sw.is_skill_enabled("slotting")


@pytest.mark.parametrize("bad", ["", "no-existe", "../../x", "'; DROP", "slotting\n", "Slotting "])
def test_an_unknown_skill_name_is_refused(bad, _isolated):
    with pytest.raises(ValueError):
        sw.set_skill_enabled(bad, False, KNOWN)

    assert sw.SKILLS_DISABLED_KEY not in _isolated


def test_skills_and_connections_use_independent_keys(_isolated):
    sw.set_skill_enabled("slotting", False, KNOWN)
    sw.set_connection_enabled("sap:principal", False)

    assert set(_isolated) == {sw.SKILLS_DISABLED_KEY, sw.CONNECTIONS_DISABLED_KEY}
    assert sw.is_connection_enabled("sap", "Principal") is False and sw.is_skill_enabled("slotting") is False and sw.is_skill_enabled("auditoria") is True


def test_a_corrupt_skills_value_means_every_skill_enabled(_isolated):
    _isolated[sw.SKILLS_DISABLED_KEY] = "{roto"

    assert sw.disabled_skill_names() == set()


# ---------------------------------------------------------------------------------------------------------------------
# `enabled` es el estado EFECTIVO: lo que el `.env` apaga (ENABLE_*) no puede salir como activo
# ---------------------------------------------------------------------------------------------------------------------
def _env_with_sap_off(monkeypatch):
    env = _base(
        oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s",
        sap_base_url="https://sap", sap_user="su", sap_password="sp", enable_sap=False,
    )
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    conns.reset_for_tests()


def test_a_connection_the_env_switches_off_is_reported_as_not_enabled_even_if_the_user_never_touched_it(monkeypatch):
    """Antes salia `enabled: true`: la pantalla dibujaba el interruptor ENCENDIDO y usable para una conexion que en realidad no se usaba."""
    _env_with_sap_off(monkeypatch)

    row = {c["key"]: c for c in sw.describe_switches()}["sap:principal"]

    assert row["enabled"] is False and row["env_disabled"] is True


def test_the_other_connections_keep_reporting_enabled_when_only_one_is_locked_by_the_env(monkeypatch):
    _env_with_sap_off(monkeypatch)

    row = {c["key"]: c for c in sw.describe_switches()}["oracle:datamart"]

    assert row["enabled"] is True and row["env_disabled"] is False


def test_the_users_saved_choice_is_not_modified_by_the_env_lock(monkeypatch, _isolated):
    """Lo guardado es solo lo que el USUARIO apago. Que el `.env` la bloquee no escribe nada: si el `.env` vuelve a permitirla, reaparece tal como el usuario la dejo."""
    _env_with_sap_off(monkeypatch)
    sw.set_connection_enabled("oracle:datamart", False)
    saved = json.loads(_isolated[sw.CONNECTIONS_DISABLED_KEY])

    sw.describe_switches()

    assert saved == ["oracle:datamart"] and json.loads(_isolated[sw.CONNECTIONS_DISABLED_KEY]) == saved, "describir no escribe nada"


def test_when_the_env_allows_it_again_the_connection_returns_as_the_user_left_it(monkeypatch):
    _env_with_sap_off(monkeypatch)
    assert {c["key"]: c["enabled"] for c in sw.describe_switches()}["sap:principal"] is False
    env_on = _base(sap_base_url="https://sap", sap_user="su", sap_password="sp", enable_sap=True)
    monkeypatch.setattr(conns, "get_settings", lambda: env_on)
    monkeypatch.setattr("app.config.get_settings", lambda: env_on)
    conns.reset_for_tests()

    assert {c["key"]: c["enabled"] for c in sw.describe_switches()}["sap:principal"] is True


def test_a_user_off_connection_stays_off_when_the_env_also_locks_it(monkeypatch):
    _env_with_sap_off(monkeypatch)
    sw.set_connection_enabled("sap:principal", False)

    row = {c["key"]: c for c in sw.describe_switches()}["sap:principal"]

    assert row["enabled"] is False and row["env_disabled"] is True


def test_bigquery_switched_off_by_the_env_is_not_reported_as_enabled(monkeypatch):
    env = _base(bq_project_id="proyecto-bq", enable_bigquery=False, oracle_host="db", oracle_user="u", oracle_password="p", oracle_service_name="s")
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr("app.config.get_settings", lambda: env)
    conns.reset_for_tests()

    assert {c["key"]: c["enabled"] for c in sw.describe_switches()}.get("bigquery:bigquery", False) is False
