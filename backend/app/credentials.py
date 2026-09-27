"""
Credenciales de conectores (Oracle/SAP/Azure-SharePoint/BigQuery)
CIFRADAS en disco -- reemplaza la edicion manual de `backend/.env` (texto
plano) por un flujo desde la UI de Configuracion (pestaña "Credenciales").

Cifrado: Fernet (AES-128-CBC + HMAC-SHA256, `cryptography.fernet`) con una
clave simetrica DERIVADA de la clave privada SSH que ya existe en
`backend/keys/jarvis_deploy_key` (deploy key del repo de skills, ver
app/skills_repo.py) -- pedido explicito del usuario ("cifrado SSH"): en
vez de generar una clave de cifrado nueva que gestionar/rotar por
separado, se reusa la key SSH que YA es un secreto bien resguardado
(nunca se commitea, ver .gitignore) y ya sirve como "identidad" del
backend. La clave Fernet se deriva con SHA-256 sobre los bytes crudos del
archivo .pem (nunca se envia la key SSH en si a ningun lado) + `HKDF` para
expandir a los 32 bytes exactos que Fernet exige, urlsafe-base64.

Si `jarvis_deploy_key` no existe todavia (instalacion nueva sin el repo
de skills configurado), esta feature queda deshabilitada -- se avisa al
usuario que primero genere esa key (`ssh-keygen -t ed25519 -f
backend/keys/jarvis_deploy_key`) antes de poder guardar credenciales
cifradas.

Persistencia: JSON con valores cifrados en `backend/.env.enc` (nunca
`.env` en texto plano) -- separado del `.env` real que sigue existiendo
para variables NO sensibles (APP_PORT, LLM_MODEL, feature flags,
etc.) y para el fallback inicial si el usuario prefiere seguir editando
el archivo a mano.

REQ-103 (spec configuracion-limpieza-credenciales-servidor, 2026-09-19):
esquema ampliado de 6 a 21 campos reales (Oracle 6, BigQuery 5, SharePoint/
Azure 6, SAP 5), definidos UNA sola vez en `CONNECTOR_SCHEMA` (data-driven
-- el frontend arma el acordeon de Credenciales llamando a
`GET /api/config/credentials/schema` en vez de tener HTML hardcodeado por
campo). Decision explicita del usuario: TODOS los campos (incluidos los no
sensibles como host/puerto/project_id, que antes solo se editaban en
`.env` texto plano) se guardan en el MISMO archivo cifrado -- un solo
mecanismo de persistencia para toda la pestaña, en vez de separar
secretos/no-secretos en dos sistemas distintos.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

ENC_FILE = BASE_DIR / ".env.enc"
DEPLOY_KEY_PATH = BASE_DIR / "keys" / "jarvis_deploy_key"

# ---------------------------------------------------------------------------
# REQ-103 (spec configuracion-limpieza-credenciales-servidor, 2026-09-19):
# esquema DATA-DRIVEN de credenciales -- antes de esto, `CREDENTIAL_FIELDS`
# solo cubria 6 campos "secretos" (usuario/password/secret/json-key), pero
# los conectores realmente necesitan ~21 campos (host, puerto, project_id,
# site_url, etc.) que hasta ahora solo se podian completar editando
# `backend/.env` a mano -- pedido explicito del usuario tras notar que
# probar "cambiar y volver a poner credenciales correctas" no funcionaba
# bien porque la UI ni siquiera exponia la mitad de los campos necesarios.
#
# CONNECTOR_SCHEMA es la UNICA fuente de verdad: el frontend arma el
# acordeon de la pestaña Credenciales llamando a GET /api/config/credentials/
# schema en vez de tener HTML hardcodeado por campo -- agregar un campo
# nuevo a un conector existente (o, a futuro, extender a mas conectores) es
# extender esta lista, no escribir HTML/JS a mano cada vez (decision
# explicita del usuario: "hagamosla dinamica" en vez de hardcodear).
#
# Cada campo declara:
#   key      -- nombre usado en el JSON cifrado y en el payload de la API
#               (coincide 1:1 con el atributo de Settings en app/config.py,
#               salvo bq_key_json que se maneja aparte via archivo).
#   label    -- texto mostrado en el formulario.
#   type     -- "text" | "password" | "number" | "textarea" (determina el
#               tipo de <input>/<textarea> que genera el frontend).
#   secret   -- True si el valor nunca debe mostrarse en claro (mismo
#               criterio que ya regia para los 6 campos originales).
#   default  -- valor sugerido/placeholder (calcado de los defaults reales
#               de Settings en config.py) -- nunca se persiste solo, es
#               una ayuda visual.
# ---------------------------------------------------------------------------
CONNECTOR_SCHEMA: dict[str, dict[str, Any]] = {
    # spec vision-generico REQ-001/003/005: el proveedor de IA y el dictado
    # tambien se pueden cargar desde la app (las claves quedan cifradas).
    "ia": {
        "label": "Inteligencia artificial (proveedor y dictado)",
        "fields": [
            {
                "key": "llm_provider",
                "label": "Proveedor (anthropic u openai_compatible)",
                "type": "text",
                "secret": False,
                "default": "anthropic",
            },
            {"key": "llm_base_url", "label": "Dirección (solo openai_compatible)", "type": "text", "secret": False},
            {"key": "llm_api_key", "label": "Clave del proveedor", "type": "password", "secret": True},
            {
                "key": "llm_model",
                "label": "Modelo por defecto",
                "type": "text",
                "secret": False,
                "default": "claude-sonnet-5",
            },
            {"key": "stt_base_url", "label": "Dictado: dirección", "type": "text", "secret": False},
            {"key": "stt_api_key", "label": "Dictado: clave", "type": "password", "secret": True},
            {"key": "stt_model", "label": "Dictado: modelo", "type": "text", "secret": False, "default": "whisper-1"},
            {"key": "vision_company_name", "label": "Nombre de la empresa", "type": "text", "secret": False},
        ],
    },
    "oracle": {
        "label": "Oracle",
        "fields": [
            {"key": "oracle_user", "label": "Usuario", "type": "text", "secret": False},
            {
                "key": "oracle_password",
                "label": "Contraseña",
                "type": "password",
                "secret": True,
            },
            {"key": "oracle_host", "label": "Host", "type": "text", "secret": False},
            {
                "key": "oracle_port",
                "label": "Puerto",
                "type": "number",
                "secret": False,
                "default": "1521",
            },
            {
                "key": "oracle_service_name",
                "label": "Service name",
                "type": "text",
                "secret": False,
            },
            {
                "key": "oracle_schema",
                "label": "Schema",
                "type": "text",
                "secret": False,
            },
        ],
    },
    "bigquery": {
        "label": "Google BigQuery",
        "fields": [
            {
                "key": "bq_project_id",
                "label": "Project ID",
                "type": "text",
                "secret": False,
            },
            {
                "key": "bq_dataset_id",
                "label": "Dataset ID",
                "type": "text",
                "secret": False,
            },
            {
                "key": "bq_table_id",
                "label": "Table ID",
                "type": "text",
                "secret": False,
            },
            {
                "key": "bq_location",
                "label": "Location",
                "type": "text",
                "secret": False,
                "default": "US",
            },
            {
                "key": "bq_key_json",
                "label": "Service account (JSON completo)",
                "type": "textarea",
                "secret": True,
            },
        ],
    },
    "sharepoint": {
        "label": "SharePoint / Azure AD (Microsoft Graph)",
        "fields": [
            {
                "key": "sharepoint_site_url",
                "label": "Site URL",
                "type": "text",
                "secret": False,
            },
            {
                "key": "azure_client_id",
                "label": "Client ID",
                "type": "text",
                "secret": False,
            },
            {
                "key": "azure_client_secret",
                "label": "Client secret",
                "type": "password",
                "secret": True,
            },
            {
                "key": "azure_tenant_id",
                "label": "Tenant ID",
                "type": "text",
                "secret": False,
            },
            {
                "key": "sharepoint_default_list_name",
                "label": "Lista por defecto",
                "type": "text",
                "secret": False,
            },
            {
                "key": "https_proxy",
                "label": "HTTPS proxy",
                "type": "text",
                "secret": False,
            },
        ],
    },
    "sap": {
        "label": "SAP (Fiori/OData v2)",
        "fields": [
            {
                "key": "sap_base_url",
                "label": "Base URL",
                "type": "text",
                "secret": False,
            },
            {
                "key": "sap_client",
                "label": "Client",
                "type": "text",
                "secret": False,
                "default": "100",
            },
            {"key": "sap_user", "label": "Usuario", "type": "text", "secret": False},
            {
                "key": "sap_password",
                "label": "Contraseña",
                "type": "password",
                "secret": True,
            },
            {
                "key": "sap_language",
                "label": "Idioma",
                "type": "text",
                "secret": False,
                "default": "EN",
            },
        ],
    },
}


def _all_schema_fields() -> list[dict[str, Any]]:
    """Aplana CONNECTOR_SCHEMA a una lista unica de field-dicts -- helper
    tipado (`Any`) para que el resto del modulo no tenga que lidiar con
    que `CONNECTOR_SCHEMA` mezcla valores str (label) y list (fields) en
    el mismo dict."""
    fields: list[dict[str, Any]] = []
    for connector in CONNECTOR_SCHEMA.values():
        fields.extend(connector["fields"])
    return fields


# Aplanado de CONNECTOR_SCHEMA -- todos los `key` de todos los conectores,
# en un solo tuple. Reemplaza al viejo CREDENTIAL_FIELDS fijo de 6 campos;
# se sigue llamando igual (mismo nombre) para no romper el resto del
# modulo, que ya lo referencia en varios lugares.
CREDENTIAL_FIELDS = tuple(field["key"] for field in _all_schema_fields())


class CredentialsError(Exception):
    """Errores de negocio de este modulo (deploy key faltante, archivo
    cifrado corrupto, etc.) -- el endpoint FastAPI los traduce a
    HTTPException con el mensaje tal cual, ya pensado para el usuario."""


def _derive_fernet_key() -> bytes:
    """SHA-256 sobre los bytes crudos del archivo de la deploy key SSH +
    codificacion urlsafe-base64 (formato exacto que Fernet exige para su
    clave de 32 bytes)."""
    if not DEPLOY_KEY_PATH.exists():
        raise CredentialsError(
            f"No existe la deploy key SSH en '{DEPLOY_KEY_PATH}' -- "
            f"generala con 'ssh-keygen -t ed25519 -f \"{DEPLOY_KEY_PATH}\"' "
            "antes de guardar credenciales cifradas."
        )
    raw = DEPLOY_KEY_PATH.read_bytes()
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def _load_encrypted() -> dict[str, str]:
    """Devuelve {campo: valor_descifrado} -- [] si el archivo no existe
    todavia (primera vez) o esta corrupto/con otra clave (se loguea pero
    no se lanza, misma filosofia 'mejor esfuerzo' del resto del proyecto)."""
    if not ENC_FILE.exists():
        return {}
    try:
        fernet = _fernet()
        raw = json.loads(ENC_FILE.read_text(encoding="utf-8"))
        result: dict[str, str] = {}
        for field, token in raw.items():
            try:
                result[field] = fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except InvalidToken:
                logger.warning(
                    "No se pudo descifrar el campo '%s' de .env.enc (clave SSH "
                    "distinta a la que se uso para cifrarlo?) -- se omite.",
                    field,
                )
        return result
    except (OSError, json.JSONDecodeError, CredentialsError) as exc:
        logger.warning("No se pudo leer .env.enc: %s", exc, exc_info=True)
        return {}


def _save_encrypted(values: dict[str, str]) -> None:
    fernet = _fernet()
    encrypted = {
        field: fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        for field, value in values.items()
        if value  # nunca persiste strings vacios -- equivale a "no seteado"
    }
    ENC_FILE.write_text(json.dumps(encrypted, indent=2), encoding="utf-8")


def get_credentials_status() -> dict[str, bool]:
    """Para la UI: {campo: True/False} segun si HAY un valor guardado --
    NUNCA devuelve el valor real (ni siquiera cifrado) al frontend."""
    stored = _load_encrypted()
    return {field: bool(stored.get(field)) for field in CREDENTIAL_FIELDS}


def get_connector_schema() -> dict[str, Any]:
    """REQ-103: definicion PURA (nombres/tipos/labels, sin ningun valor
    real) de los 4 conectores -- consumida por
    `GET /api/config/credentials/schema` para que el frontend arme el
    acordeon de la pestaña Credenciales de forma dinamica. Nunca expone
    contenido sensible, solo la ESTRUCTURA del formulario (mismo criterio
    de `get_credentials_status`: la UI nunca ve un valor real guardado)."""
    return CONNECTOR_SCHEMA


def save_credentials(values: dict[str, str]) -> None:
    """Actualiza (merge, no reemplaza) los campos presentes en `values`
    con valor no vacio -- un campo vacio en el payload NO borra el valor
    ya guardado (para eso esta `clear_credentials`), simplemente no se
    toca (permite guardar solo el campo que se esta editando sin tener
    que re-mandar todos los demas)."""
    current = _load_encrypted()
    for field, value in values.items():
        if field not in CREDENTIAL_FIELDS:
            continue
        if value:
            current[field] = value
    _save_encrypted(current)
    apply_credentials_to_settings()


def clear_credentials() -> None:
    """Borra TODAS las credenciales guardadas -- vuelve a depender
    exclusivamente de lo que haya en `backend/.env` (texto plano, para
    quien prefiera seguir editandolo a mano)."""
    if ENC_FILE.exists():
        ENC_FILE.unlink()
    apply_credentials_to_settings()


# Mapeo campo -> atributo real de Settings (app/config.py) que se
# sobreescribe en memoria al aplicar credenciales -- Settings es
# `@lru_cache`, por eso se mutan los atributos de la instancia ya cacheada
# en vez de tratar de invalidar/recrear el cache en caliente.
#
# REQ-103: generado a partir de CONNECTOR_SCHEMA (todo campo cuyo `key`
# coincide 1:1 con un atributo real de Settings) -- se excluye
# "oracle_port" (necesita cast a int antes de asignar, ver el loop de
# apply_credentials_to_settings mas abajo) y "bq_key_json" (no es un
# atributo de Settings, se maneja aparte via archivo en disco).
_NON_DIRECT_ATTR_FIELDS = {"oracle_port", "bq_key_json"}
_FIELD_TO_SETTINGS_ATTR = {
    field["key"]: field["key"]
    for field in _all_schema_fields()
    if field["key"] not in _NON_DIRECT_ATTR_FIELDS
}


def apply_credentials_to_settings() -> None:
    """Aplica las credenciales cifradas guardadas SOBRE la instancia ya
    cacheada de `Settings` (pisa lo que venga de `.env` para esos campos
    puntuales) -- se llama al arrancar el proceso (ver app/main.py
    startup) y cada vez que se guardan/borran credenciales, para que el
    cambio tenga efecto inmediato sin reiniciar el backend."""
    stored = _load_encrypted()
    settings = get_settings()
    for field, attr in _FIELD_TO_SETTINGS_ATTR.items():
        value = stored.get(field)
        if value:
            setattr(settings, attr, value)

    # REQ-103: "oracle_port" es el unico campo numerico del schema --
    # Settings.oracle_port es `int` (usado directo en oracle_dsn), asi que
    # el string guardado en el .env.enc se castea antes de asignar. Si el
    # usuario escribio algo no numerico, se ignora (mejor esfuerzo, mismo
    # criterio del resto del modulo) en vez de romper el arranque.
    oracle_port_raw = stored.get("oracle_port")
    if oracle_port_raw:
        try:
            settings.oracle_port = int(oracle_port_raw)
        except ValueError:
            logger.warning(
                "oracle_port guardado ('%s') no es un numero valido -- se ignora.",
                oracle_port_raw,
            )

    bq_key_json = stored.get("bq_key_json")
    if bq_key_json:
        _write_bq_key_file(bq_key_json)


def _write_bq_key_file(key_json: str) -> None:
    """El SDK de BigQuery espera una RUTA de archivo (`GOOGLE_APPLICATION_
    CREDENTIALS`), no el JSON inline -- se escribe a la ruta ya configurada
    (`settings.bq_credentials_path`, default `backend/keys/bigquery_key.
    json`) en vez de inventar un mecanismo distinto de carga."""
    try:
        json.loads(key_json)  # valida que sea JSON antes de escribir a disco
    except json.JSONDecodeError as exc:
        raise CredentialsError(
            f"El JSON de la service account de BigQuery no es valido: {exc}"
        ) from exc
    settings = get_settings()
    path = settings.bq_credentials_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key_json, encoding="utf-8")
