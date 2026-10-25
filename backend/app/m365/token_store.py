"""Almacen cifrado del token de Microsoft 365 (spec office-outlook-teams-mcp REQ-M03, REQ-M11, REQ-M12).

Por que existe: Vision se conecta al conector de la empresa (EAKC `mcp-msgraph`, Outlook y Teams) con un login OAuth. El token que devuelve da acceso al buzon y a los chats de la
persona, asi que NUNCA puede quedar en texto plano: se cifra con DPAPI (`app/secret_store.py`, el mismo cifrado de las contrasenas del gestor de conexiones) y se guarda en
`secure_settings` de `~/.vision/vision.db`. DPAPI deriva la clave del usuario de Windows: copiar `vision.db` a otra PC u otro perfil no revela nada (REQ-M12), y aca eso
se trata como «sin sesion», nunca como un error que rompa el arranque.

Se guardan DOS cosas por separado, a proposito:
- el TOKEN (acceso + renovacion): se borra al cerrar sesion (REQ-M11);
- el REGISTRO DEL CLIENTE (el `client_id` que el servidor de la empresa entrego al registrarse): se conserva al cerrar sesion, para no crear un cliente nuevo en el servidor
  cada vez que la persona entra (cada registro queda a su nombre en graph.eakc.empresa.com).

`M365TokenStorage` implementa la interfaz `TokenStorage` del paquete `mcp`: el cliente OAuth oficial le pide y le entrega estos dos objetos. Nada de este modulo imprime ni
devuelve el texto de un token fuera del SDK: `public_status()` es lo unico que ve la pantalla y solo dice si hay sesion."""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError

from app import secret_store
from app.persistence import master_index

logger = logging.getLogger(__name__)

# Claves de `secure_settings` (prefijo propio: no chocan con las de `config_store`).
TOKEN_KEY = "m365.token"
CLIENT_KEY = "m365.client"
# Campo propio dentro del bloque cifrado del token: la HORA (epoch, segundos) en que se guardo. El SDK de `mcp` guarda `expires_in` («dura 4471 s») pero NO desde cuando, y decide si un token
# vale por `token_expiry_time`, que queda VACIO al leerlo del disco: lo da por valido para siempre, manda la peticion vencida, recibe un 401 y recien ahi pide un login NUEVO en vez de renovar
# con el `refresh_token` (medido el 2026-10-17: el token tenia 256 min y duraba 68). Con la hora, `client.py` y `auth.py` le calculan el vencimiento al SDK al arrancar.
_ISSUED_AT = "_issued_at"


def _read(key: str) -> dict[str, Any] | None:
    """Descifra lo guardado en `key`. Cualquier problema (nada guardado, de otra PC/usuario, danado) es None: el llamador lo trata como «sin sesion»."""
    blob = master_index.get_secure_setting(key)
    if not blob:
        return None
    try:
        return secret_store.decrypt_json(blob)
    except secret_store.SecretStoreError:
        # REQ-M12: el log dice QUE paso, nunca contenido (ni el blob).
        logger.warning(
            "Microsoft 365: lo guardado en '%s' no se puede leer en esta PC o usuario; se ignora y hay que iniciar sesion de nuevo",
            key,
        )
        return None


def _write(key: str, data: dict[str, Any]) -> None:
    master_index.set_secure_setting(key, secret_store.encrypt_json(data))


def _delete(key: str) -> None:
    # `set_secure_setting` con un blob vacio es lo que lee `get_secure_setting` como «nada» (devuelve None si no hay bytes).
    master_index.set_secure_setting(key, b"")


def load_token() -> OAuthToken | None:
    """El token guardado, o None si no hay, es ilegible o no tiene la forma de un token (nunca se le entrega al SDK algo a medias)."""
    data = _read(TOKEN_KEY)
    if data is None:
        return None
    try:
        return OAuthToken.model_validate({k: v for k, v in data.items() if k != _ISSUED_AT})  # la hora es del almacen: el SDK recibe un OAuthToken normal
    except ValidationError:
        logger.warning("Microsoft 365: el token guardado no tiene la forma esperada; se ignora")
        return None


def load_client_info() -> OAuthClientInformationFull | None:
    data = _read(CLIENT_KEY)
    if data is None:
        return None
    try:
        return OAuthClientInformationFull.model_validate(data)
    except ValidationError:
        logger.warning("Microsoft 365: el registro de cliente guardado no tiene la forma esperada; se ignora")
        return None


def issued_at() -> float | None:
    """Hora (epoch, segundos) en que se guardo el token, o None si no hay token o se guardo antes de que existiera este dato (los de las primeras pruebas)."""
    data = _read(TOKEN_KEY)
    value = data.get(_ISSUED_AT) if isinstance(data, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def has_session() -> bool:
    """Hay un token que este usuario puede leer. No dice que siga vigente: eso lo decide el servidor (el SDK renueva con el `refresh_token`)."""
    return load_token() is not None


def sign_out() -> None:
    """Cierra la sesion (REQ-M11): borra el token cifrado. El registro del cliente se conserva a proposito (ver el docstring del modulo)."""
    _delete(TOKEN_KEY)


def forget_client() -> None:
    """Borra tambien el registro del cliente (solo para «empezar de cero»; cerrar sesion NO lo usa)."""
    _delete(CLIENT_KEY)


def public_status() -> dict[str, Any]:
    """Lo unico que sale hacia la pantalla: SI hay sesion y si hay un cliente registrado. Jamas el texto del token ni del `client_id`."""
    return {"connected": has_session(), "registered": load_client_info() is not None}


class M365TokenStorage(TokenStorage):
    """Interfaz que el cliente OAuth del paquete `mcp` usa para guardar y leer el token y el registro del cliente."""

    async def get_tokens(self) -> OAuthToken | None:
        return load_token()

    async def set_tokens(self, tokens: OAuthToken) -> None:
        # Cada vez que el SDK guarda un token (login o RENOVACION) se anota la hora: la renovacion tiene que contar desde ahora, no desde el primer login.
        _write(TOKEN_KEY, {**tokens.model_dump(mode="json", exclude_none=True), _ISSUED_AT: time.time()})

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return load_client_info()

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        _write(CLIENT_KEY, client_info.model_dump(mode="json", exclude_none=True))
