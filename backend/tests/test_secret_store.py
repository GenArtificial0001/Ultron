"""Cifrado DPAPI de la configuracion (spec gestor-conexiones REQ-001): lo guardado no se lee en claro, solo lo descifra este usuario de Windows y cualquier falla es un
`SecretStoreError` sin valores. Usa el DPAPI REAL de la PC (no hay red ni ventanas: `CRYPTPROTECT_UI_FORBIDDEN`)."""

from __future__ import annotations

import pytest

from app import secret_store as ss

pytestmark = pytest.mark.skipif(not ss.is_available(), reason="DPAPI solo existe en Windows")

SECRET = "Clave-Oracle-Ñandú-2026!"


def test_a_connection_is_encrypted_and_decrypted_back_exactly():
    data = {"oracle_user": "usuario1", "oracle_password": SECRET, "oracle_port": "1521", "vacio": ""}

    assert ss.decrypt_json(ss.encrypt_json(data)) == data


def test_the_stored_bytes_never_contain_the_secret_in_clear():
    blob = ss.encrypt_json({"oracle_password": SECRET})

    for encoding in ("utf-8", "utf-16-le", "latin-1"):
        try:
            needle = SECRET.encode(encoding)
        except UnicodeEncodeError:
            continue
        assert needle not in blob, f"la contrasena aparece en claro ({encoding})"
    assert b"oracle_password" not in blob, "ni siquiera el NOMBRE del campo queda legible"


def test_two_encryptions_of_the_same_data_are_different_but_both_decrypt():
    first = ss.encrypt_json({"k": SECRET})
    second = ss.encrypt_json({"k": SECRET})

    assert first != second, "DPAPI agrega sal aleatoria: un blob no delata que dos valores son iguales"
    assert ss.decrypt_json(first) == ss.decrypt_json(second) == {"k": SECRET}


def test_a_tampered_blob_is_refused_as_undecryptable():
    blob = bytearray(ss.encrypt_json({"k": SECRET}))
    blob[len(blob) // 2] ^= 0xFF

    with pytest.raises(ss.SecretStoreError) as err:
        ss.decrypt_json(bytes(blob))

    assert err.value.undecryptable is True


def test_a_blob_made_with_another_entropy_is_not_readable(monkeypatch):
    """Otra aplicacion que use DPAPI con su propia entropia no produce blobs que Vision acepte (y al reves)."""
    blob = ss.encrypt_json({"k": SECRET})
    monkeypatch.setattr(ss, "_ENTROPY", b"otra-aplicacion")

    with pytest.raises(ss.SecretStoreError) as err:
        ss.decrypt_json(blob)

    assert err.value.undecryptable is True


@pytest.mark.parametrize("blob", [b"", b"no es un blob de dpapi", b"\x00" * 64])
def test_garbage_is_refused_without_crashing(blob):
    with pytest.raises(ss.SecretStoreError) as err:
        ss.decrypt_json(blob)

    assert err.value.undecryptable is True


@pytest.mark.parametrize("payload", [b"[1, 2, 3]", b'"texto"', b"\xff\xfe no es utf-8", b"{roto"])
def test_content_that_is_not_a_json_object_is_treated_as_damaged(payload):
    with pytest.raises(ss.SecretStoreError) as err:
        ss.decrypt_json(ss.protect_bytes(payload))

    assert err.value.undecryptable is True and "danada" in str(err.value)


def test_an_empty_dict_roundtrips():
    assert ss.decrypt_json(ss.encrypt_json({})) == {}


def test_a_big_bigquery_key_roundtrips():
    # el encabezado se arma por partes: escrito entero, el detector de secretos del repo (scripts/check_secrets.py) lo marca, y es una clave FALSA a proposito
    marker = "PRIVATE" + " KEY-----"
    key = {"type": "service_account", "private_key": "-----BEGIN " + marker + "\n" + "A" * 3000 + "\n-----END " + marker + "\n"}

    assert ss.decrypt_json(ss.encrypt_json(key)) == key


def test_error_messages_never_carry_values():
    blob = bytearray(ss.encrypt_json({"oracle_password": SECRET}))
    blob[-1] ^= 0x01

    with pytest.raises(ss.SecretStoreError) as err:
        ss.decrypt_json(bytes(blob))

    assert SECRET not in str(err.value)


def test_outside_windows_it_fails_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(ss.sys, "platform", "linux")

    assert ss.is_available() is False
    with pytest.raises(ss.SecretStoreError) as err:
        ss.encrypt_json({"k": "v"})

    assert "Windows" in str(err.value)
