"""Configuracion general desde la base cifrada (spec gestor-conexiones A4: REQ-002/003/006, categoria 9).

Con la base ACTIVA, `get_settings()` no lee el `.env` (aunque exista) y aplica lo guardado; las variables de entorno del sistema siguen mandando; guardar se ve en el
`Settings` vivo sin reiniciar. Base maestra en `tmp_path` (autouse) y DPAPI real. El `.env` de prueba se apunta con `model_config["env_file"]`."""

from __future__ import annotations

import os

import pytest

from app import config
from app import config_store as cs
from app import general_settings as gs
from app import secret_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=9999\nFISH_API_KEY=clave-del-env\nORACLE_HOST=host-del-env\n", encoding="utf-8")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(env_file))
    for var in ("APP_PORT", "FISH_API_KEY", "ORACLE_HOST", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_VISION_SET_ENV", {})
    cs.invalidate()
    config.get_settings.cache_clear()
    yield
    cs.invalidate()
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------------------------------
# REQ-012 / REQ-003: inactiva lee el .env; activa NO
# ---------------------------------------------------------------------------------------------------------------------
def test_while_the_store_is_inactive_the_env_file_is_read_as_always():
    s = config.get_settings()

    assert s.app_port == 9999 and s.fish_api_key == "clave-del-env"


def test_once_active_the_env_file_is_ignored_even_if_it_exists():
    cs.set_general({"llm_model": "gpt-otro"})
    config.get_settings.cache_clear()

    s = config.get_settings()

    assert s.app_port == 8030 and s.fish_api_key == "" and s.oracle_host == "", "nada del .env"
    assert s.llm_model == "gpt-otro", "lo guardado en la base si"


def test_stored_numbers_are_applied_as_numbers():
    gs.save({"app_port": "8021", "export_row_threshold": "200"})
    config.get_settings.cache_clear()

    s = config.get_settings()

    assert s.app_port == 8021 and s.export_row_threshold == 200


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006: guardar se ve sin reiniciar
# ---------------------------------------------------------------------------------------------------------------------
def test_saving_updates_the_live_settings_without_restarting():
    live = config.get_settings()

    gs.save({"fish_api_key": "nueva-clave-fish", "llm_model": "modelo-nuevo"})

    assert live.fish_api_key == "nueva-clave-fish" and live.llm_model == "modelo-nuevo", "la MISMA instancia que ya tienen todos los modulos"


def test_clearing_a_field_returns_it_to_its_default():
    live = config.get_settings()
    gs.save({"llm_model": "modelo-nuevo"})

    gs.save({"llm_model": ""})

    assert live.llm_model == config.Settings.model_fields["llm_model"].default
    assert "llm_model" not in cs.get_general()


def test_what_is_saved_is_encrypted_and_survives_a_rebuild():
    gs.save({"elevenlabs_api_key": "sk-eleven-123"})
    cs.invalidate()
    config.get_settings.cache_clear()

    assert config.get_settings().elevenlabs_api_key == "sk-eleven-123"


# ---------------------------------------------------------------------------------------------------------------------
# Validacion: solo claves de la configuracion general, con valores validos
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["oracle_password", "enable_oracle", "app_log_path", "cors_origins", "no_existe"])
def test_keys_that_are_not_general_settings_are_refused(key):
    with pytest.raises(cs.ConfigStoreError):
        gs.save({key: "x"})
    assert cs.get_general() == {}


@pytest.mark.parametrize("value", ["ocho mil", "-1", "1.5"])
def test_a_number_field_refuses_text(value):
    with pytest.raises(cs.ConfigStoreError) as err:
        gs.save({"app_port": value})
    assert "número" in str(err.value)


@pytest.mark.parametrize("value", ["0", "70000"])
def test_the_port_must_be_a_real_port(value):
    with pytest.raises(cs.ConfigStoreError):
        gs.save({"app_port": value})


def test_graph_source_is_kept_but_is_not_a_settings_attribute():
    gs.save({"graph_source": "Material Industrial"})

    assert cs.get_general()["graph_source"] == "Material Industrial"
    assert not hasattr(config.get_settings(), "graph_source")


# ---------------------------------------------------------------------------------------------------------------------
# Variables de entorno del sistema mandan (regla 6)
# ---------------------------------------------------------------------------------------------------------------------
def test_a_system_environment_variable_wins_over_the_stored_value(monkeypatch):
    cs.set_general({"llm_model": "de-la-base"})
    monkeypatch.setenv("LLM_MODEL", "del-sistema")
    config.get_settings.cache_clear()

    assert config.get_settings().llm_model == "del-sistema"
    assert gs.env_overrides("llm_model") is True


def test_saving_does_not_override_a_system_environment_variable(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "del-sistema")
    live = config.get_settings()

    gs.save({"llm_model": "de-la-pantalla"})

    assert live.llm_model == "del-sistema"


# ---------------------------------------------------------------------------------------------------------------------
# Proxy: el que pone Vision se actualiza; el del sistema nunca se toca
# ---------------------------------------------------------------------------------------------------------------------
def test_changing_the_proxy_from_the_screen_updates_the_process_environment():
    config.get_settings()

    gs.save({"https_proxy": "http://proxy-nuevo:83"})
    assert os.environ["HTTPS_PROXY"] == "http://proxy-nuevo:83" and "127.0.0.1" in os.environ["NO_PROXY"]

    gs.save({"https_proxy": ""})
    assert "HTTPS_PROXY" not in os.environ and "NO_PROXY" not in os.environ, "borrar el proxy de la pantalla lo quita del proceso"


def test_a_proxy_vision_set_but_someone_changed_afterwards_is_no_longer_vision_s(monkeypatch):
    """Vision solo es duena de una variable mientras tenga el valor que Vision puso (un registro viejo no alcanza para pisar lo de otro)."""
    config.get_settings()
    gs.save({"https_proxy": "http://de-vision:83"})
    monkeypatch.setenv("HTTPS_PROXY", "http://de-otro:80")

    gs.save({"https_proxy": "http://de-vision-nuevo:83"})

    assert os.environ["HTTPS_PROXY"] == "http://de-otro:80"
    assert gs.env_overrides("https_proxy") is True


def test_a_system_proxy_is_never_touched(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-del-sistema:80")
    config.get_settings()

    gs.save({"https_proxy": "http://proxy-de-la-pantalla:83"})

    assert os.environ["HTTPS_PROXY"] == "http://proxy-del-sistema:80"


# ---------------------------------------------------------------------------------------------------------------------
# Lo que ve la pantalla: nunca un secreto
# ---------------------------------------------------------------------------------------------------------------------
def test_the_description_shows_plain_values_but_never_secrets():
    gs.save({"fish_api_key": "clave-secreta-fish", "fish_voice_id": "voz-1", "bq_key_json": '{"type": "service_account"}'})

    rows = {r["key"]: r for r in gs.describe()}

    assert rows["fish_voice_id"]["value"] == "voz-1"
    assert "value" not in rows["fish_api_key"] and rows["fish_api_key"]["set"] is True
    assert "value" not in rows["bq_key_json"] and rows["fish_api_key"]["secret"] is True
    assert "clave-secreta-fish" not in repr(gs.describe())


def test_every_general_field_exists_in_settings_except_graph_source():
    missing = [f.key for f in gs.GENERAL_FIELDS if f.key not in config.Settings.model_fields and f.key != "graph_source"]

    assert missing == []


def test_no_connection_field_or_enable_flag_is_a_general_setting():
    from app import connections

    connection_keys = {f.key for t in connections.get_types().values() for f in (*t.fields, *t.personal_fields)}
    general = {f.key for f in gs.GENERAL_FIELDS}

    assert not (general & connection_keys), "los datos de una conexion van en su tarjeta, no en la configuracion general"
    assert not {k for k in general if k.startswith("enable_")}, "los ENABLE_* los reemplazan los interruptores"
