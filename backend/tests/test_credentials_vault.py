"""spec gestor-conexiones (decision confirmada, spec.md:136 y REQ-011): con la configuracion ya en Vision, el `.env.enc` se importo y
"despues no se lee mas", y "Vision nunca vuelve a crearlo".

Hueco encontrado al cerrar la spec (2026-10-09): el arranque (`credentials.apply_credentials_to_settings`) seguia aplicando el
`.env.enc` ENCIMA de lo guardado en Vision (una clave vieja de Fish/BigQuery le ganaba a la nueva en cada reinicio) y volvia a
escribir la key de BigQuery en texto plano en `keys/`; y la pestaña vieja «Credenciales» seguia guardando en `.env.enc` (lo volvia
a crear). Con la base INACTIVA (antes de importar) todo sigue como siempre. `.env.enc`, deploy key y base maestra en `tmp_path`."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import config, credentials
from app import config_store as cs
from app import main

OLD_FISH = "Clave-Fish-Vieja-del-env-enc"
KEY_JSON = json.dumps({"type": "service_account", "project_id": "proyecto-de-prueba", "private_key": "x"})


@pytest.fixture
def enc(tmp_path, monkeypatch, tmp_master_db):
    """Un `.env.enc` con una clave de Fish y la key de BigQuery, como el que tiene un usuario que todavia no lo borro."""
    enc_file = tmp_path / ".env.enc"
    deploy_key = tmp_path / "fake_deploy_key"
    deploy_key.write_bytes(b"contenido de prueba, no es una key SSH real")
    monkeypatch.setattr(credentials, "ENC_FILE", enc_file)
    monkeypatch.setattr(credentials, "DEPLOY_KEY_PATH", deploy_key)
    credentials._save_encrypted({"fish_api_key": OLD_FISH, "bq_key_json": KEY_JSON})
    settings = config.get_settings()
    monkeypatch.setattr(settings, "google_application_credentials", str(tmp_path / "keys" / "bigquery_key.json"))
    monkeypatch.setattr(settings, "fish_api_key", "")
    cs.invalidate()
    yield enc_file
    cs.invalidate()


def test_before_importing_the_env_enc_still_applies_as_always(enc):
    assert cs.is_active() is False
    credentials.apply_credentials_to_settings()

    assert config.get_settings().fish_api_key == OLD_FISH
    assert config.get_settings().bq_credentials_path.is_file(), "la key de BigQuery se escribe donde la espera el SDK, como siempre"


def test_with_the_configuration_in_vision_the_env_enc_is_no_longer_read(enc, monkeypatch):
    cs.activate()
    monkeypatch.setattr(config.get_settings(), "fish_api_key", "Clave-Nueva-de-Vision")

    credentials.apply_credentials_to_settings()  # lo que corre al arrancar

    assert config.get_settings().fish_api_key == "Clave-Nueva-de-Vision", "la clave vieja del .env.enc ya no le gana a la de ULTRON"
    assert not config.get_settings().bq_credentials_path.exists(), "no se vuelve a escribir la key de BigQuery en texto plano"


def test_with_the_configuration_in_vision_the_old_tab_does_not_recreate_the_env_enc(enc):
    enc.unlink()  # el usuario ya toco «Borrar archivos viejos»
    cs.activate()

    resp = TestClient(main.app).post("/api/config/credentials", json={"fields": {"fish_api_key": "otra-clave"}})

    assert resp.status_code == 409
    assert "Configuración > Conexiones" in resp.json()["detail"]
    assert not enc.exists(), "ULTRON nunca vuelve a crear el .env.enc"


def test_before_importing_the_old_tab_still_saves(enc):
    resp = TestClient(main.app).post("/api/config/credentials", json={"fields": {"fish_api_key": "otra-clave"}})

    assert resp.status_code == 200
    assert credentials._load_encrypted()["fish_api_key"] == "otra-clave"


def test_with_the_configuration_in_vision_the_old_tab_status_does_not_read_the_env_enc(enc, monkeypatch):
    """Revision 2026-10-09: la pestaña vieja tampoco descifra el .env.enc para mostrar «Ya tiene un valor guardado»."""
    cs.activate()

    def no_decrypt():  # pragma: no cover - no debe llamarse
        raise AssertionError("con la base activa el .env.enc no se lee")

    monkeypatch.setattr(credentials, "_fernet", no_decrypt)

    status = TestClient(main.app).get("/api/config/credentials").json()

    assert status and not any(status.values())


def test_with_the_configuration_in_vision_remove_all_does_not_claim_it_removed_the_keys(enc, monkeypatch):
    """Revision 2026-10-09: «Quitar todas las credenciales» respondia 200 («Credenciales quitadas») pero las claves de Vision seguian
    en uso. Ahora responde 409 y dice donde se cambian; no toca nada."""
    cs.activate()
    monkeypatch.setattr(config.get_settings(), "fish_api_key", "Clave-de-Vision")

    resp = TestClient(main.app).delete("/api/config/credentials")

    assert resp.status_code == 409
    assert "Configuración > Conexiones" in resp.json()["detail"]
    assert config.get_settings().fish_api_key == "Clave-de-Vision"
    assert enc.exists(), "el .env.enc viejo se borra con «Borrar archivos viejos», no desde aca"


def test_before_importing_remove_all_still_works(enc):
    resp = TestClient(main.app).delete("/api/config/credentials")

    assert resp.status_code == 200 and not any(resp.json().values())
    assert not enc.exists()


# ---------------------------------------------------------------------------------------------------------------------
# Pedido del usuario 2026-10-09: con la configuracion en Vision la pestaña Credenciales se oculta, asi que los avisos que mandaban
# ahi mandan a la tarjeta del gestor. Antes de importar, siguen mandando a Credenciales.
# ---------------------------------------------------------------------------------------------------------------------
def test_the_messages_point_to_the_credentials_tab_before_importing(enc):
    from app.friendly_errors import describe_voice_engine_error

    assert credentials.where_to_load("Voz") == "Configuración > Credenciales"
    assert "Configuración > Credenciales" in describe_voice_engine_error("sin_clave", "Fish Audio").message


def test_with_the_configuration_in_vision_the_messages_point_to_the_connections_screen(enc):
    from app.friendly_errors import describe_voice_engine_error

    cs.activate()

    assert credentials.where_to_load("Voz") == "Configuración > Conexiones > Voz"
    for kind in ("sin_clave", "clave_invalida"):
        message = describe_voice_engine_error(kind, "Fish Audio").message
        assert "Configuración > Conexiones > Voz" in message and "Credenciales" not in message


def test_with_the_configuration_in_vision_the_voice_chain_says_where_the_key_goes(enc, monkeypatch):
    from app.voice import service as voice_service
    from app.voice.settings import VoiceConfig

    cs.activate()
    monkeypatch.setattr(config.get_settings(), "fish_api_key", "")

    reason = voice_service._link_ready({"engine": "fish", "model": "s1"}, VoiceConfig())

    assert reason == "falta la clave en Configuración > Conexiones > Voz"


def test_with_the_configuration_in_vision_a_missing_bigquery_key_points_to_the_connections_screen(enc, monkeypatch, tmp_path):
    from app.connectors.bigquery_connector import BigQueryConnector

    cs.activate()
    settings = config.get_settings()
    monkeypatch.setattr(settings, "bq_key_json", "")
    monkeypatch.setattr(settings, "google_application_credentials", str(tmp_path / "no-existe.json"))

    with pytest.raises(FileNotFoundError) as exc:
        BigQueryConnector(settings)._get_credentials()

    assert "Configuración > Conexiones > BigQuery" in str(exc.value) and ".env" not in str(exc.value)
