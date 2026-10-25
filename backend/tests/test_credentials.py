"""spec configuracion-limpieza-credenciales-servidor REQ-103: esquema
data-driven de credenciales (`app/credentials.py`). Aisla `ENC_FILE` y
`DEPLOY_KEY_PATH` en `tmp_path` (via monkeypatch) para nunca tocar el
`.env.enc`/deploy key REAL del usuario -- mismo criterio de aislamiento que
`tmp_master_db` en conftest.py."""

from __future__ import annotations

import pytest

from app import credentials


@pytest.fixture
def isolated_credentials(tmp_path, monkeypatch):
    """Redirige ENC_FILE a una ruta temporal y crea una deploy key falsa
    (cualquier contenido de bytes sirve -- solo se usa para derivar la
    clave Fernet vía SHA-256, no se valida como SSH real)."""
    enc_file = tmp_path / ".env.enc"
    deploy_key = tmp_path / "fake_deploy_key"
    deploy_key.write_bytes(b"contenido de prueba, no es una key SSH real")
    monkeypatch.setattr(credentials, "ENC_FILE", enc_file)
    monkeypatch.setattr(credentials, "DEPLOY_KEY_PATH", deploy_key)
    monkeypatch.setattr(credentials, "LOCAL_KEY_PATH", tmp_path / "keys" / "credentials.key")
    return enc_file


@pytest.fixture
def new_install(tmp_path, monkeypatch):
    """Instalacion nueva: sin deploy key ni clave local (pendiente 2 de
    vision-generico)."""
    monkeypatch.setattr(credentials, "ENC_FILE", tmp_path / ".env.enc")
    monkeypatch.setattr(credentials, "DEPLOY_KEY_PATH", tmp_path / "keys" / "jarvis_deploy_key")
    monkeypatch.setattr(credentials, "LOCAL_KEY_PATH", tmp_path / "keys" / "credentials.key")
    return tmp_path


def test_connector_schema_has_the_ai_group_and_4_connectors():
    """spec vision-generico: primero el proveedor de IA y el dictado, despues
    la voz (spec voz-fish-audio REQ-009) y los 4 conectores."""
    schema = credentials.get_connector_schema()
    assert list(schema.keys()) == ["ia", "voz", "oracle", "bigquery", "sharepoint", "sap"]


def test_connector_schema_never_leaks_real_values():
    """El schema es PURA definicion -- ningun field-dict debe tener una
    key llamada 'value' ni nada que sugiera un valor real, solo
    metadata (key/label/type/secret)."""
    schema = credentials.get_connector_schema()
    for connector in schema.values():
        for field in connector["fields"]:
            assert set(field.keys()) <= {"key", "label", "type", "secret", "default"}


def test_credential_fields_covers_the_ai_provider_and_the_22_connector_fields():
    """REQ-103: los 22 campos de los conectores, mas los 8 del proveedor de IA
    y el dictado (spec vision-generico) y las 2 claves de voz (spec
    voz-fish-audio REQ-009)."""
    expected = {
        "llm_provider",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "stt_base_url",
        "stt_api_key",
        "stt_model",
        "vision_company_name",
        "fish_api_key",
        "elevenlabs_api_key",
        "oracle_user",
        "oracle_password",
        "oracle_host",
        "oracle_port",
        "oracle_service_name",
        "oracle_schema",
        "bq_project_id",
        "bq_dataset_id",
        "bq_table_id",
        "bq_location",
        "bq_key_json",
        "sharepoint_site_url",
        "azure_client_id",
        "azure_client_secret",
        "azure_tenant_id",
        "sharepoint_default_list_name",
        "https_proxy",
        "sap_base_url",
        "sap_client",
        "sap_user",
        "sap_password",
        "sap_language",
    }
    assert set(credentials.CREDENTIAL_FIELDS) == expected
    assert len(credentials.CREDENTIAL_FIELDS) == 32


def test_get_credentials_status_all_false_when_empty(isolated_credentials):
    status = credentials.get_credentials_status()
    assert len(status) == 32
    assert all(v is False for v in status.values())


def test_save_and_status_reflects_saved_fields(isolated_credentials):
    credentials.save_credentials({"oracle_host": "10.0.0.5", "oracle_port": "1521"})
    status = credentials.get_credentials_status()
    assert status["oracle_host"] is True
    assert status["oracle_port"] is True
    assert status["sap_user"] is False  # no tocado, sigue en False


def test_save_credentials_ignores_unknown_fields(isolated_credentials):
    """Mismo criterio de seguridad ya existente: un campo que no esta en
    CREDENTIAL_FIELDS se ignora silenciosamente, nunca se persiste."""
    credentials.save_credentials({"campo_inventado": "algo"})
    status = credentials.get_credentials_status()
    assert "campo_inventado" not in status


def test_save_credentials_merge_does_not_erase_other_fields(isolated_credentials):
    credentials.save_credentials({"oracle_host": "10.0.0.5"})
    credentials.save_credentials({"oracle_port": "1521"})
    status = credentials.get_credentials_status()
    assert status["oracle_host"] is True
    assert status["oracle_port"] is True


def test_save_credentials_empty_value_does_not_erase(isolated_credentials):
    credentials.save_credentials({"oracle_host": "10.0.0.5"})
    credentials.save_credentials({"oracle_host": ""})  # vacio -- no debe borrar
    status = credentials.get_credentials_status()
    assert status["oracle_host"] is True


def test_clear_credentials_removes_everything(isolated_credentials):
    credentials.save_credentials({"oracle_host": "10.0.0.5"})
    credentials.clear_credentials()
    status = credentials.get_credentials_status()
    assert all(v is False for v in status.values())


def test_apply_credentials_to_settings_casts_oracle_port(isolated_credentials):
    """Bug potencial detectado durante el diseño: oracle_port es el unico
    campo numerico del schema -- Settings.oracle_port es `int`, asi que el
    string guardado debe castearse antes de asignar."""
    credentials.save_credentials({"oracle_port": "1522"})
    settings = credentials.get_settings()
    assert settings.oracle_port == 1522
    assert isinstance(settings.oracle_port, int)


def test_apply_credentials_to_settings_ignores_invalid_oracle_port(
    isolated_credentials,
):
    """Si el usuario escribe algo no numerico en oracle_port, no debe
    romper el arranque -- se ignora (mejor esfuerzo)."""
    original_port = credentials.get_settings().oracle_port
    credentials.save_credentials({"oracle_port": "no-es-un-numero"})
    settings = credentials.get_settings()
    assert settings.oracle_port == original_port  # sin cambios


def test_apply_credentials_to_settings_direct_string_fields(isolated_credentials):
    credentials.save_credentials(
        {
            "oracle_host": "10.0.0.5",
            "sharepoint_site_url": "https://empresa.sharepoint.com/sites/logistico",
        }
    )
    settings = credentials.get_settings()
    assert settings.oracle_host == "10.0.0.5"
    assert settings.sharepoint_site_url == "https://empresa.sharepoint.com/sites/logistico"


def test_ai_keys_saved_from_the_app_are_secret_and_reach_the_settings(isolated_credentials, monkeypatch):
    """spec vision-generico REQ-001: la clave del proveedor cargada desde
    Configuracion > Credenciales se usa sin reiniciar y nunca se muestra."""
    from app.config import get_settings

    fields = {f["key"]: f for f in credentials.get_connector_schema()["ia"]["fields"]}
    assert fields["llm_api_key"]["secret"] is True and fields["stt_api_key"]["secret"] is True
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "")
    credentials.save_credentials({"llm_api_key": "clave-de-prueba"})
    assert settings.llm_api_key == "clave-de-prueba"
    assert credentials.get_credentials_status()["llm_api_key"] is True


# ---------------------------------------------------------------------------
# Pendiente 2 de vision-generico (REQ-006): clave de cifrado propia de cada
# instalacion cuando no existe la deploy key de la PC original.
# ---------------------------------------------------------------------------
def test_new_install_can_save_credentials_and_generates_its_own_key(new_install, monkeypatch):
    settings = credentials.get_settings()
    monkeypatch.setattr(settings, "oracle_host", "")
    credentials.save_credentials({"oracle_host": "10.0.0.5"})

    key_file = new_install / "keys" / "credentials.key"
    assert key_file.exists()
    assert credentials.get_credentials_status()["oracle_host"] is True
    assert settings.oracle_host == "10.0.0.5"
    # El archivo cifrado no guarda el valor en claro.
    assert "10.0.0.5" not in (new_install / ".env.enc").read_text(encoding="utf-8")


def test_reading_without_saved_credentials_does_not_create_a_key(new_install):
    credentials.get_credentials_status()
    credentials.apply_credentials_to_settings()
    assert not (new_install / "keys" / "credentials.key").exists()


def test_the_generated_key_is_reused_and_never_overwritten(new_install):
    credentials.save_credentials({"oracle_host": "10.0.0.5"})
    key_file = new_install / "keys" / "credentials.key"
    first_key = key_file.read_bytes()
    credentials.save_credentials({"sap_user": "usuario"})
    assert key_file.read_bytes() == first_key
    status = credentials.get_credentials_status()
    assert status["oracle_host"] is True and status["sap_user"] is True


def test_existing_deploy_key_keeps_decrypting_files_saved_with_it(new_install):
    """Una instalacion que ya cifro con la deploy key no pierde lo guardado:
    mientras no exista `credentials.key`, se sigue usando la deploy key."""
    deploy_key = new_install / "keys" / "jarvis_deploy_key"
    deploy_key.parent.mkdir(parents=True)
    deploy_key.write_bytes(b"deploy key de prueba")
    credentials.save_credentials({"oracle_host": "10.0.0.5"})

    assert not (new_install / "keys" / "credentials.key").exists()
    assert credentials.get_credentials_status()["oracle_host"] is True


def test_a_damaged_local_key_gives_a_clear_error(new_install):
    key_file = new_install / "keys" / "credentials.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_bytes(b"no es una clave fernet")
    with pytest.raises(credentials.CredentialsError, match="dañada"):
        credentials.save_credentials({"oracle_host": "10.0.0.5"})


def test_voice_keys_are_secret_and_reach_the_settings(isolated_credentials, monkeypatch):
    """spec voz-fish-audio REQ-009: las claves de Fish Audio y ElevenLabs se
    cargan desde Configuracion > Credenciales, cifradas, y se usan sin reiniciar."""
    fields = {f["key"]: f for f in credentials.get_connector_schema()["voz"]["fields"]}
    assert fields["fish_api_key"]["secret"] is True
    assert fields["elevenlabs_api_key"]["secret"] is True
    settings = credentials.get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    credentials.save_credentials({"fish_api_key": "clave-fish", "elevenlabs_api_key": "clave-eleven"})
    assert settings.fish_api_key == "clave-fish"
    assert settings.elevenlabs_api_key == "clave-eleven"
