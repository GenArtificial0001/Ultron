"""Almacen del token de Microsoft 365 (spec office-outlook-teams-mcp REQ-M03, REQ-M11, REQ-M12).

Lo que se vigila, con la base maestra en `tmp_path` (fixture autouse `tmp_master_db`) y el DPAPI REAL de la PC: lo que queda en disco, no un mock del cifrado.
1. El token y el registro del cliente quedan CIFRADOS: ni el texto del token ni el `refresh_token` aparecen en el archivo `vision.db`.
2. Se leen de vuelta tal cual (el SDK `mcp` los pide por su interfaz `TokenStorage`).
3. Cerrar sesion borra el token pero NO el registro del cliente (no hace falta volver a registrar uno).
4. Un blob que este usuario/PC no puede leer (copiado de otra PC) se trata como «sin sesion», nunca rompe ni devuelve basura (REQ-M12).
5. Nada del token sale en el estado que ve la pantalla ni en los mensajes de error."""

from __future__ import annotations

import asyncio

import pytest

from app import secret_store
from app.m365 import token_store
from app.persistence import master_index

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

ACCESS = "eyJhbGciOiJSUzI1NiJ9.ACCESS-TOKEN-MUY-SECRETO-4471"
REFRESH = "REFRESH-TOKEN-MUY-SECRETO-9931"


def _token():
    from mcp.shared.auth import OAuthToken

    return OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token=REFRESH)


def _client_info():
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id="cliente-vision-123",
        redirect_uris=["http://127.0.0.1:8020/api/m365/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _disk_bytes(db) -> bytes:
    return db.read_bytes() if db.exists() else b""


def test_without_anything_saved_there_is_no_session():
    store = token_store.M365TokenStorage()

    assert asyncio.run(store.get_tokens()) is None
    assert asyncio.run(store.get_client_info()) is None
    assert token_store.has_session() is False


def test_the_token_is_saved_encrypted_and_read_back_identical(tmp_master_db):
    store = token_store.M365TokenStorage()

    asyncio.run(store.set_tokens(_token()))
    on_disk = _disk_bytes(tmp_master_db)
    back = asyncio.run(store.get_tokens())

    assert back is not None and back.access_token == ACCESS and back.refresh_token == REFRESH
    assert ACCESS.encode() not in on_disk and REFRESH.encode() not in on_disk, (
        "el token quedo en texto plano en vision.db"
    )
    assert token_store.has_session() is True


def test_the_client_registration_is_saved_encrypted_and_read_back(tmp_master_db):
    store = token_store.M365TokenStorage()

    asyncio.run(store.set_client_info(_client_info()))
    back = asyncio.run(store.get_client_info())

    assert back is not None and back.client_id == "cliente-vision-123"
    assert b"cliente-vision-123" not in _disk_bytes(tmp_master_db)


def test_signing_out_deletes_the_token_but_keeps_the_client_registration():
    store = token_store.M365TokenStorage()
    asyncio.run(store.set_tokens(_token()))
    asyncio.run(store.set_client_info(_client_info()))

    token_store.sign_out()

    assert asyncio.run(store.get_tokens()) is None
    assert token_store.has_session() is False
    kept = asyncio.run(store.get_client_info())
    assert kept is not None and kept.client_id == "cliente-vision-123", (
        "cerrar sesion no debe obligar a registrar otro cliente"
    )


def test_a_blob_this_user_cannot_read_counts_as_no_session_and_never_breaks():
    """Caso real: se copia vision.db de otra PC u otro perfil de Windows (REQ-M12)."""
    master_index.set_secure_setting(token_store.TOKEN_KEY, b"esto-no-lo-cifro-este-usuario")
    master_index.set_secure_setting(token_store.CLIENT_KEY, b"tampoco")
    store = token_store.M365TokenStorage()

    assert asyncio.run(store.get_tokens()) is None
    assert asyncio.run(store.get_client_info()) is None
    assert token_store.has_session() is False


def test_a_blob_with_the_wrong_shape_is_ignored_not_trusted():
    """Descifra bien pero no es un token: no se le entrega nada raro al SDK."""
    master_index.set_secure_setting(token_store.TOKEN_KEY, secret_store.encrypt_json({"cualquier": "cosa"}))
    store = token_store.M365TokenStorage()

    assert asyncio.run(store.get_tokens()) is None
    assert token_store.has_session() is False


def test_the_status_shown_on_screen_never_carries_token_text():
    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    shown = repr(token_store.public_status())

    assert ACCESS not in shown and REFRESH not in shown
    assert "connected" in shown or "conectado" in shown.lower()


# ---------------------------------------------------------------------------------------------------------------------
# Hallazgo con el uso REAL (2026-10-17): el token guardado tiene que recordar CUANDO se emitio
# ---------------------------------------------------------------------------------------------------------------------
# El SDK decide si un token vale por `token_expiry_time`, NO por `expires_in`; al leerlo del disco `token_expiry_time` queda vacio y lo da por valido PARA SIEMPRE: manda la peticion con un
# token vencido, el servidor responde 401 y recien ahi pide un login NUEVO en vez de renovar con el `refresh_token`. Medido: el token de la persona tenia 256 min y duraba 68.
# Por eso se guarda la HORA de emision (aca) y el proveedor calcula el vencimiento al arrancar (`test_m365_auth.py`).
def test_the_saved_token_remembers_when_it_was_issued(monkeypatch):
    monkeypatch.setattr(token_store.time, "time", lambda: 1_000_000.0)

    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    assert token_store.issued_at() == 1_000_000.0


def test_a_token_saved_before_this_fix_has_no_issue_time():
    """Los tokens que ya estan guardados en las PC (de las pruebas de hoy) no traen hora: no se puede saber cuando nacieron."""
    old = {"access_token": ACCESS, "token_type": "Bearer", "expires_in": 4471, "refresh_token": REFRESH}
    master_index.set_secure_setting(token_store.TOKEN_KEY, secret_store.encrypt_json(old))

    assert token_store.issued_at() is None
    assert token_store.has_session() is True, "sigue habiendo sesion: se renueva con el refresh_token, no se pierde"


def test_nothing_saved_means_no_issue_time():
    assert token_store.issued_at() is None


def test_the_issue_time_is_not_part_of_the_token_the_sdk_receives_nor_of_the_screen_status():
    """La hora es un detalle interno del almacen: el SDK recibe un `OAuthToken` normal y la pantalla solo ve si hay sesion."""
    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    back = asyncio.run(token_store.M365TokenStorage().get_tokens())

    assert back is not None and back.access_token == ACCESS and back.expires_in == 4471
    assert "issued_at" not in back.model_dump() and "issued_at" not in repr(token_store.public_status())


def test_the_issue_time_is_encrypted_with_the_rest_not_kept_in_plain_text(tmp_master_db):
    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    assert b"issued_at" not in _disk_bytes(tmp_master_db)


def test_saving_a_renewed_token_updates_the_issue_time(monkeypatch):
    """El SDK renueva el token solo y lo vuelve a guardar: la hora de emision tiene que ser la de la RENOVACION, no la del primer login."""
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(token_store.time, "time", lambda: now["t"])
    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    now["t"] += 5000
    asyncio.run(token_store.M365TokenStorage().set_tokens(_token()))

    assert token_store.issued_at() == 1_005_000.0
