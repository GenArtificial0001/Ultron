"""Conexiones y configuracion general CIFRADAS en `~/.vision/vision.db` (spec gestor-conexiones REQ-001/003/004/004b/006).

Por que existe: el usuario decidio (2026-10-08) que ni el `.env` ni `connections.json` deben existir: todo se configura desde la pantalla de Vision y queda cifrado en
la base global. Este modulo es la UNICA puerta a ese almacen: valida, cifra (app/secret_store.py, DPAPI del usuario de Windows) y guarda filas en
`conn_store`/`secure_settings` (app/persistence/master_index.py).

Ciclo de vida: mientras el usuario no importe ni cargue nada, el almacen esta INACTIVO y Vision se comporta como siempre (REQ-012). La primera alta o importacion lo
ACTIVA (`app_settings.config_source = "vision_db"`): desde ahi la base es la unica fuente y el `.env`/`connections.json` ya no se leen (REQ-003).

Garantias:
- Nunca se guarda una conexion sin nombre valido ni una cadena que no sea un campo real de su tipo (REQ-004b, REQ-011 de la spec).
- Un blob que este usuario no puede descifrar (otra PC u otro perfil) NO rompe nada: la conexion figura como ilegible y el resto sigue (regla 6 de la spec).
- Los mensajes de error son para personas y nunca llevan valores."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app import connections, secret_store
from app.persistence import master_index

logger = logging.getLogger(__name__)

CONFIG_SOURCE_KEY = "config_source"
CONFIG_SOURCE_VISION_DB = "vision_db"
GENERAL_KEY = "general"
MAX_VALUE_CHARS = 20_000  # una key de BigQuery o las secciones de Confluence entran sobradas; corta un pegado accidental enorme


class ConfigStoreError(ValueError):
    """Lo que el usuario intento guardar no es valido. El texto es para mostrarlo tal cual en la pantalla (sin valores)."""


@dataclass(frozen=True)
class StoredConnection:
    """Una conexion guardada. `fields` tiene secretos: no se imprime ni se loguea; vacio (y `undecryptable=True`) si este usuario/PC no puede leerla."""

    id: str
    type_id: str
    name: str
    default: bool
    created_at: float
    updated_at: float
    tested_at: float | None
    test_status: str
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    undecryptable: bool = False


_lock = threading.RLock()
# Caches en memoria: (ruta de la base, momento de la lectura, dato). Se invalidan con cada escritura de ESTE proceso; el vencimiento corto hace que lo que guarde OTRO
# proceso (dev y prod comparten la base, decision del usuario) se vea en segundos. Sin cache, cada `list_connections()` (decenas por turno) abriria la base.
_CACHE_TTL_S = 2.0
_cache: tuple[str, float, list[StoredConnection]] | None = None
_general_cache: tuple[str, float, dict[str, str]] | None = None
_active_cache: tuple[str, float, bool] | None = None


def _db_key() -> str:
    return str(master_index.master_db_path())


def _fresh(entry: tuple[Any, ...] | None) -> bool:
    return entry is not None and entry[0] == _db_key() and time.monotonic() - entry[1] < _CACHE_TTL_S


def invalidate() -> None:
    """Olvida lo descifrado en memoria (despues de cada escritura y en los tests)."""
    global _cache, _general_cache, _active_cache
    with _lock:
        _cache = None
        _general_cache = None
        _active_cache = None


# ---------------------------------------------------------------------------------------------------------------------
# Activo / inactivo (REQ-003, REQ-012)
# ---------------------------------------------------------------------------------------------------------------------
def is_active() -> bool:
    """True cuando la base es la unica fuente de la configuracion. Una base que no se puede abrir cuenta como inactiva: Vision sigue como siempre."""
    global _active_cache
    with _lock:
        if _fresh(_active_cache):
            return _active_cache[2]  # type: ignore[index]
        try:
            active = master_index.get_app_setting(CONFIG_SOURCE_KEY) == CONFIG_SOURCE_VISION_DB
        except Exception:  # noqa: BLE001 -- sin base maestra (permisos, disco) Vision arranca con lo de siempre
            logger.warning("No se pudo leer el origen de la configuracion (se usa el de siempre)", exc_info=True)
            return False  # no se cachea: se reintenta en la proxima consulta
        _active_cache = (_db_key(), time.monotonic(), active)
        return active


def activate() -> None:
    master_index.set_app_setting(CONFIG_SOURCE_KEY, CONFIG_SOURCE_VISION_DB)
    invalidate()


# ---------------------------------------------------------------------------------------------------------------------
# Conexiones
# ---------------------------------------------------------------------------------------------------------------------
def _storable_type(type_id: str) -> connections.ConnectionType:
    ctype = connections.get_types().get(type_id)
    if ctype is None:
        raise ConfigStoreError("Ese tipo de conexión no existe.")
    if ctype.derived_from is not None:
        raise ConfigStoreError(f"{ctype.label} no lleva datos propios: usa las credenciales de otra conexión.")
    return ctype


def allowed_field_keys(type_id: str) -> frozenset[str]:
    """Campos que una conexion de este tipo puede guardar: los del tipo y los personales (en la base, cada conexion lleva los suyos)."""
    ctype = _storable_type(type_id)
    return frozenset(f.key for f in ctype.fields) | frozenset(f.key for f in ctype.personal_fields)


def _clean_fields(type_id: str, raw: dict[str, Any]) -> dict[str, str]:
    allowed = allowed_field_keys(type_id)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigStoreError(f"Campos que no son de este tipo de conexión: {', '.join(unknown)}.")
    out: dict[str, str] = {}
    for key, value in raw.items():
        text = "" if value is None else str(value)
        if len(text) > MAX_VALUE_CHARS:
            raise ConfigStoreError(f"El campo {key} es demasiado largo.")
        out[key] = text
    return out


def check_fields(type_id: str, raw: dict[str, Any]) -> dict[str, str]:
    """Las mismas reglas que al guardar (tipo existente, campos de ese tipo, largo), sin guardar nada: para «Probar» antes de guardar."""
    _storable_type(type_id)
    return _clean_fields(type_id, raw)


def _check_name(name: Any) -> str:
    """REQ-004b (decision del usuario: «no dejes crear una conexion si no tiene nombre»)."""
    text = str(name or "")
    if not text.strip():
        raise ConfigStoreError("Poné un nombre a la conexión antes de guardarla.")
    if not connections.valid_name(text):
        raise ConfigStoreError(
            f"El nombre no es válido: de 1 a {connections.MAX_NAME_LENGTH} caracteres, solo letras, números, espacios y guion, sin espacios al principio ni al final."
        )
    return text


def list_stored() -> list[StoredConnection]:
    """Conexiones guardadas, descifradas (cacheadas hasta la proxima escritura)."""
    global _cache
    with _lock:
        if _fresh(_cache):
            return list(_cache[2])  # type: ignore[index]
        out: list[StoredConnection] = []
        for row in master_index.list_conn_store():
            fields: dict[str, str] = {}
            undecryptable = False
            try:
                fields = {str(k): "" if v is None else str(v) for k, v in secret_store.decrypt_json(bytes(row["fields_blob"])).items()}
            except secret_store.SecretStoreError:
                undecryptable = True  # otra PC u otro perfil: la conexion figura, sin datos, y se pide volver a cargarla
                logger.warning("La conexion %s - %s no se puede descifrar con este usuario de Windows", row["type_id"], row["name"])
            out.append(
                StoredConnection(
                    row["id"], row["type_id"], row["name"], bool(row["is_default"]), row["created_at"], row["updated_at"], row["tested_at"],
                    row["test_status"] or "", fields, undecryptable,
                )
            )
        _cache = (_db_key(), time.monotonic(), out)
        return list(out)


def get_stored(conn_id: str) -> StoredConnection:
    for c in list_stored():
        if c.id == conn_id:
            return c
    raise ConfigStoreError("Esa conexión no existe.")


def add_connection(type_id: str, name: Any, fields: dict[str, Any], *, default: bool = False) -> StoredConnection:
    """Alta de una conexion (REQ-004). Valida tipo, nombre (obligatorio), campos y tope por tipo; cifra los campos y ACTIVA el almacen."""
    _storable_type(type_id)
    clean_name = _check_name(name)
    clean = _clean_fields(type_id, fields)
    same_type = [c for c in list_stored() if c.type_id == type_id]
    if any(c.name.lower() == clean_name.lower() for c in same_type):
        raise ConfigStoreError(f"Ya hay una conexión de este tipo que se llama «{clean_name}».")
    if len(same_type) >= connections.MAX_PER_TYPE:
        raise ConfigStoreError(f"Ya hay {connections.MAX_PER_TYPE} conexiones de este tipo, que es el máximo.")
    now = time.time()
    row = {
        "id": uuid.uuid4().hex,
        "type_id": type_id,
        "name": clean_name,
        "is_default": default or not same_type,  # la primera de un tipo es la predeterminada
        "fields_blob": secret_store.encrypt_json(clean),
        "created_at": now,
    }
    with _lock:
        if default:
            master_index.clear_default_conn_store(type_id)
        try:
            master_index.insert_conn_store(row)
        except sqlite3.IntegrityError as exc:  # carrera con otra alta del mismo nombre
            raise ConfigStoreError(f"Ya hay una conexión de este tipo que se llama «{clean_name}».") from exc
        activate()
        invalidate()
    return get_stored(row["id"])


def update_connection(conn_id: str, *, name: Any = None, fields: dict[str, Any] | None = None, default: bool | None = None) -> StoredConnection:
    """Edita una conexion. `fields` es PARCIAL: lo que no viene queda igual; un valor None borra ese campo. Renombrar respeta las mismas reglas que el alta."""
    current = get_stored(conn_id)
    changes: dict[str, Any] = {}
    if name is not None:
        clean_name = _check_name(name)
        if clean_name.lower() != current.name.lower() and any(
            c.type_id == current.type_id and c.name.lower() == clean_name.lower() for c in list_stored()
        ):
            raise ConfigStoreError(f"Ya hay una conexión de este tipo que se llama «{clean_name}».")
        changes["name"] = clean_name
    if fields is not None:
        if current.undecryptable:
            # no se puede mezclar con datos que no se leen: se reemplaza todo con lo que el usuario vuelve a cargar
            merged = {}
        else:
            merged = dict(current.fields)
        for key, value in _clean_fields(current.type_id, {k: v for k, v in fields.items() if v is not None}).items():
            merged[key] = value
        for key in [k for k, v in fields.items() if v is None]:
            _clean_fields(current.type_id, {key: ""})  # valida que el campo exista en el tipo
            merged.pop(key, None)
        changes["fields_blob"] = secret_store.encrypt_json(merged)
    with _lock:
        if default:
            master_index.clear_default_conn_store(current.type_id)
            changes["is_default"] = True
        elif default is False:
            changes["is_default"] = False
        try:
            if not master_index.update_conn_store(conn_id, **changes):
                raise ConfigStoreError("Esa conexión no existe.")
        except sqlite3.IntegrityError as exc:
            raise ConfigStoreError("Ya hay una conexión de este tipo con ese nombre.") from exc
        invalidate()
    return get_stored(conn_id)


def delete_connection(conn_id: str) -> StoredConnection:
    """Baja de una conexion (REQ-009). Devuelve lo que se borro (para quitar su interruptor)."""
    current = get_stored(conn_id)
    with _lock:
        master_index.delete_conn_store(conn_id)
        invalidate()
    return current


def record_test(conn_id: str, ok: bool) -> None:
    """Anota el resultado de «Probar» (insignia y fecha de la tarjeta, REQ-005)."""
    with _lock:
        master_index.update_conn_store(conn_id, tested_at=time.time(), test_status="ok" if ok else "error")
        invalidate()


# ---------------------------------------------------------------------------------------------------------------------
# Configuracion general (voz, el proveedor de IA, proxy, BigQuery, puerto...)
# ---------------------------------------------------------------------------------------------------------------------
def get_general() -> dict[str, str]:
    """La configuracion general guardada, descifrada. Ilegible (otra PC/perfil) = vacia, con aviso en el log."""
    global _general_cache
    with _lock:
        if _fresh(_general_cache):
            return dict(_general_cache[2])  # type: ignore[index]
        data: dict[str, str] = {}
        blob = master_index.get_secure_setting(GENERAL_KEY)
        if blob:
            try:
                data = {str(k): "" if v is None else str(v) for k, v in secret_store.decrypt_json(blob).items()}
            except secret_store.SecretStoreError:
                logger.warning("La configuracion general guardada no se puede descifrar con este usuario de Windows")
        _general_cache = (_db_key(), time.monotonic(), data)
        return dict(data)


def set_general(updates: dict[str, Any]) -> dict[str, str]:
    """Cambia claves de la configuracion general (parcial; None borra). QUE claves se admiten lo decide quien llama (la capa de `Settings`, paso A4 de la spec)."""
    merged = get_general()
    for key, value in updates.items():
        if value is None:
            merged.pop(str(key), None)
            continue
        text = str(value)
        if len(text) > MAX_VALUE_CHARS:
            raise ConfigStoreError(f"El campo {key} es demasiado largo.")
        merged[str(key)] = text
    with _lock:
        master_index.set_secure_setting(GENERAL_KEY, secret_store.encrypt_json(merged))
        _mirror_for_launcher(merged)
        activate()
        invalidate()
    return get_general()


LAUNCHER_PORT_KEY = "launcher_app_port"  # la misma clave que lee launch_vision.py (no puede importar `app`)


def _mirror_for_launcher(general: dict[str, str]) -> None:
    """spec gestor-conexiones C2: el lanzador elige el puerto ANTES de arrancar el servidor y no puede descifrar (corre fuera de la app). El puerto no es secreto:
    se deja una copia EN CLARO en `app_settings`. Ningun otro dato sale del blob cifrado."""
    port = str(general.get("app_port", "") or "").strip()
    if port:
        master_index.set_app_setting(LAUNCHER_PORT_KEY, port)
    else:
        master_index.unset_app_setting(LAUNCHER_PORT_KEY)
