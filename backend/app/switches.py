"""Interruptores para activar y desactivar CONEXIONES y SKILLS (spec interruptores-conexiones-skills, REQ-001/002/004/005/009/011).

Por que existe: Vision le manda al modelo TODAS las herramientas en cada vuelta y lista todas las conexiones en el prompt. Cada conexion nueva suma tools y tokens, y el modelo
termina buscando «por todos lados» (ej. consultaba Confluence para cualquier pregunta). El usuario necesita elegir por donde busca Vision.

Diseno (cambio minimo, REQ-012): este modulo solo GUARDA y CONSULTA el estado; los filtros son de una linea en `ToolRegistry.definitions`, `describe_for_prompt` y
`list_available_skills_xml`. No se toca ninguna tool ni conector.

Persistencia (decision del usuario): SQLite GLOBAL de `~/.vision` (tabla `app_settings` de `vision.db`, la misma de «Skill Globales»), valido para toda la app y no por sesion.
Se guarda SOLO lo apagado: `connections_disabled` y `skills_disabled`, cada una una lista JSON. Prender = quitar de la lista. Asi una instalacion existente queda TODA activa (REQ-005)
y un reset borra una sola clave.

Seguridad: las claves que llegan del navegador se validan contra las conexiones/skills REALES; nunca se guarda una cadena arbitraria (REQ-011)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import connections
from app.persistence import master_index

logger = logging.getLogger(__name__)

CONNECTIONS_DISABLED_KEY = "connections_disabled"
SKILLS_DISABLED_KEY = "skills_disabled"
# spec office-outlook-teams-mcp REQ-M09: interruptor de Outlook y Teams. NO es una conexion del gestor (no tiene campos ni credenciales, tiene un login), asi que no entra en
# `describe_switches()` (su contrato lo fijan varios tests) sino que tiene su propia clave. Guarda un `1` SOLO cuando esta APAGADO: nada guardado = encendido, como en una instalacion limpia.
M365_DISABLED_KEY = "m365_disabled"
# Toda herramienta de Outlook y Teams se llama `m365_<algo>`: se filtran por PREFIJO, no por una lista que alguien tenga que acordarse de actualizar.
M365_TOOL_PREFIX = "m365_"
# spec office-com-automatizacion REQ-009: interruptor de Office (Excel, Word y conversion por COM). Mismo patron que el de Outlook y Teams: no es una conexion del gestor, guarda un `1` SOLO cuando esta
# APAGADO (nada guardado = encendido) y filtra por PREFIJO `office_` (una tool nueva queda cubierta sola, sin acordarse de anotarla en una lista).
OFFICE_DISABLED_KEY = "office_disabled"
OFFICE_TOOL_PREFIX = "office_"
_MAX_ITEMS = 500  # tope defensivo: la lista guardada nunca crece sin limite

# tipo de conexion de cada tool: el filtro de `ToolRegistry.definitions` se apoya en esto. Una tool que no figura aca nunca se filtra (read, bash, glob...).
TOOL_CONNECTION_TYPE: dict[str, str] = {
    "oracle_query": "oracle",
    "sharepoint_list_items": "sharepoint",
    "sharepoint_list_lists": "sharepoint",
    "sap_query": "sap",
    "graph_search": "graph",
    "graph_directory_lookup": "graph",
    "graph_directory_manager": "graph",
    "bigquery_query": "bigquery",
    "bigquery_list_tables": "bigquery",
    "confluence_list_spaces": "confluence",
    "confluence_search": "confluence",
    "confluence_get_page_tree": "confluence",
    "confluence_get_page": "confluence",
    "confluence_list_attachments": "confluence",
    "confluence_download_attachment": "confluence",
}

BIGQUERY_KEY = "bigquery:bigquery"


class DisabledConnectionError(Exception):
    """La conexion pedida existe pero esta desactivada. El texto le dice al modelo que hacer, sin mencionar ningun dato de la conexion."""


# ---------------------------------------------------------------------------------------------------------------------
# Claves
# ---------------------------------------------------------------------------------------------------------------------
def connection_key(type_id: str, name: str) -> str:
    """Clave estable de una conexion: `<tipo>:<nombre en minuscula>` (ej. `oracle:datamart`)."""
    return f"{type_id.strip().lower()}:{name.strip().lower()}"


def _read_list(key: str) -> list[str]:
    """Lista guardada bajo `key`. Cualquier valor roto o inesperado se trata como lista vacia (= todo activo): un valor corrupto NUNCA apaga cosas."""
    try:
        raw = master_index.get_app_setting(key)
    except Exception:  # noqa: BLE001 -- sin base maestra (ej. primer arranque) = todo activo
        logger.warning("No se pudo leer el interruptor '%s' (se asume todo activo)", key, exc_info=True)
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, str)][:_MAX_ITEMS]


def _write_list(key: str, items: list[str]) -> None:
    unique = sorted(set(items))[:_MAX_ITEMS]
    if unique:
        master_index.set_app_setting(key, json.dumps(unique, ensure_ascii=False))
    else:
        master_index.unset_app_setting(key)  # nada apagado = sin clave (instalacion «limpia»)


# ---------------------------------------------------------------------------------------------------------------------
# Conexiones
# ---------------------------------------------------------------------------------------------------------------------
def disabled_connection_keys() -> set[str]:
    return set(_read_list(CONNECTIONS_DISABLED_KEY))


def is_connection_enabled(type_id: str, name: str) -> bool:
    return connection_key(type_id, name) not in disabled_connection_keys()


def all_connection_keys() -> dict[str, dict[str, str]]:
    """Todas las conexiones REALES configuradas (incluida BigQuery, que no esta en el registro de tipos) con su tipo, nombre y titulo. Base de la validacion de claves."""
    out: dict[str, dict[str, str]] = {}
    for c in connections.list_connections():
        out[connection_key(c.type_id, c.name)] = {"type": c.type_id, "name": c.name, "display": c.display}
    from app.config import get_settings

    settings = get_settings()
    if getattr(settings, "enable_bigquery", True) and str(getattr(settings, "bq_project_id", "") or "").strip():
        out[BIGQUERY_KEY] = {"type": "bigquery", "name": "bigquery", "display": connections.bigquery_display()}
    return out


def set_connection_enabled(key: str, enabled: bool) -> None:
    """Prende o apaga UNA conexion. La clave tiene que ser de una conexion real (ValueError si no): nunca se guarda una cadena que mande el cliente sin validar."""
    if key not in all_connection_keys():
        raise ValueError("Esa conexion no existe.")
    current = disabled_connection_keys()
    if enabled:
        current.discard(key)
    else:
        current.add(key)
    _write_list(CONNECTIONS_DISABLED_KEY, list(current))


def forget_connection(key: str) -> None:
    """spec gestor-conexiones REQ-009: al BORRAR una conexion se olvida su interruptor (si una nueva se llama igual, nace encendida). No valida contra las conexiones
    reales porque la conexion ya no existe; solo quita esa clave de lo apagado."""
    current = disabled_connection_keys()
    if key in current:
        current.discard(key)
        _write_list(CONNECTIONS_DISABLED_KEY, list(current))


def describe_switches() -> list[dict[str, Any]]:
    """Lo que ve la pantalla: tipo, nombre, titulo y estado de CADA conexion. NUNCA campos ni credenciales (REQ-011).

    `enabled`: estado EFECTIVO = interruptor del usuario Y lo que permite el `.env`. Una conexion que el `.env` apaga (ENABLE_*) sale `enabled: false` aunque el usuario no la
    haya tocado: la pantalla la dibuja apagada y no la cuenta como activa (antes salia `true` y el interruptor se veia encendido y usable). Lo que el usuario dejo guardado NO
    se modifica: si el `.env` la vuelve a permitir, reaparece como el usuario la dejo.
    `env_disabled`: el `.env` la apaga: en ese caso no se puede prender desde la pantalla."""
    off = disabled_connection_keys()
    out: list[dict[str, Any]] = []
    for key, info in all_connection_keys().items():
        env_ok = _env_allows(info["type"])
        out.append(
            {
                "key": key,
                "type": info["type"],
                "name": info["name"],
                "display": info["display"],
                "enabled": key not in off and env_ok,
                "env_disabled": not env_ok,
            }
        )
    return sorted(out, key=lambda c: c["display"].lower())


def _env_allows(type_id: str) -> bool:
    if type_id == "bigquery":
        from app.config import get_settings

        return bool(getattr(get_settings(), "enable_bigquery", True))
    return connections.is_enabled(type_id)


def active_types() -> set[str]:
    """Tipos de conexion con AL MENOS una conexion activa (encendida por el usuario y permitida por el `.env`). Una tool de un tipo ausente de este conjunto no se envia."""
    types: set[str] = set()
    for key, info in all_connection_keys().items():
        if key not in disabled_connection_keys() and _env_allows(info["type"]):
            types.add(info["type"])
    return types


def is_m365_enabled() -> bool:
    """Outlook y Teams: APAGADO de fabrica en ULTRON (spec portar-desde-origen, decision del usuario 2026-10-25). Solo un `0` guardado a
    proposito (la persona lo prendio) lo enciende; nada guardado, un valor roto o una base ilegible = apagado."""
    try:
        return master_index.get_app_setting(M365_DISABLED_KEY) == "0"
    except Exception:  # noqa: BLE001 -- sin base maestra (ej. primer arranque) = apagado
        logger.warning("No se pudo leer el interruptor de Outlook y Teams (se asume apagado)", exc_info=True)
        return False


def set_m365_enabled(enabled: bool) -> None:
    """Prende o apaga Outlook y Teams. Apagarlo NO cierra la sesion ni borra el token: volver a prenderlo no obliga a iniciar sesion de nuevo."""
    master_index.set_app_setting(M365_DISABLED_KEY, "0" if enabled else "1")  # ULTRON: sin clave = apagado de fabrica


def is_office_enabled() -> bool:
    """Office (Excel y conversion): APAGADO de fabrica en ULTRON (spec portar-desde-origen, decision del usuario 2026-10-25). Solo un `0`
    guardado a proposito lo enciende; nada guardado, un valor roto o una base ilegible = apagado."""
    try:
        return master_index.get_app_setting(OFFICE_DISABLED_KEY) == "0"
    except Exception:  # noqa: BLE001 -- sin base maestra (ej. primer arranque) = apagado
        logger.warning("No se pudo leer el interruptor de Office (se asume apagado)", exc_info=True)
        return False


def set_office_enabled(enabled: bool) -> None:
    """Prende o apaga Office. Apagarlo no cierra ningun libro ni toca ningun archivo: solo saca las herramientas `office_*` del modelo."""
    master_index.set_app_setting(OFFICE_DISABLED_KEY, "0" if enabled else "1")  # ULTRON: sin clave = apagado de fabrica


def tool_is_available(tool_name: str) -> bool:
    """False si la tool es de un tipo de conexion SIN ninguna conexion activa. Las tools que no son de una conexion (read, bash...) siempre estan disponibles."""
    if tool_name.startswith(OFFICE_TOOL_PREFIX):
        return is_office_enabled()  # spec office-com-automatizacion REQ-009: apagado, ninguna herramienta de Office sale hacia el modelo
    if tool_name.startswith(M365_TOOL_PREFIX):
        return is_m365_enabled()  # spec office-outlook-teams-mcp REQ-M09: apagado, ninguna herramienta de Outlook y Teams sale hacia el modelo
    type_id = TOOL_CONNECTION_TYPE.get(tool_name)
    if type_id is None:
        return True
    off = disabled_connection_keys()
    if not off:
        return True  # camino rapido: nada apagado, nada que filtrar (la mayoria de las instalaciones)
    return type_id in active_types() or not _type_is_configured(type_id)


def availability_filter():
    """Lo mismo que `tool_is_available`, pero leyendo los interruptores UNA sola vez: para filtrar una lista entera de tools.

    Hallazgo de ULTRON (2026-10-25): `ToolRegistry.definitions()` se arma en CADA paso del chat y preguntaba por cada una de las 69
    tools; cada pregunta abria la base SQLite (`get_app_setting`). Con la PC cargada eran ~9 ms por apertura: un turno de 100 pasos
    pasaba 2 minutos solo en eso (`test_the_loop_cannot_run_forever` llego a tardar 194 s). Se decide en cada llamada (nada queda
    cacheado entre pasos), asi que prender o apagar algo se ve en el paso siguiente, como siempre."""
    office = is_office_enabled()
    m365 = is_m365_enabled()
    off = disabled_connection_keys()
    state: dict[str, object] = {}
    configured: dict[str, bool] = {}

    def available(tool_name: str) -> bool:
        if tool_name.startswith(OFFICE_TOOL_PREFIX):
            return office
        if tool_name.startswith(M365_TOOL_PREFIX):
            return m365
        type_id = TOOL_CONNECTION_TYPE.get(tool_name)
        if type_id is None or not off:
            return True
        if "active" not in state:
            state["active"] = active_types()
        if type_id in state["active"]:  # type: ignore[operator]
            return True
        if type_id not in configured:
            configured[type_id] = _type_is_configured(type_id)
        return not configured[type_id]

    return available


def _type_is_configured(type_id: str) -> bool:
    """Un tipo SIN conexiones configuradas no se filtra: el comportamiento de siempre (la tool explica que falta configurar) no cambia."""
    return any(info["type"] == type_id for info in all_connection_keys().values())


def assert_connection_enabled(type_id: str, name: str) -> None:
    """Defensa en profundidad (REQ-003): una tool llamada sobre una conexion apagada se rechaza ANTES de abrir ninguna conexion de red."""
    if not is_connection_enabled(type_id, name):
        ctype = connections.get_types().get(type_id)
        label = ctype.label if ctype is not None else type_id  # BigQuery no esta en el registro de tipos
        raise DisabledConnectionError(
            f"La conexion '{name}' de tipo {label} esta desactivada. Pedile al usuario que la active en Configuracion > Conexiones (o en el panel rapido)."
        )


def resolve_enabled(type_id: str, name: str | None = None) -> connections.Connection:
    """Como `connections.resolve`, pero SOLO entre las conexiones ACTIVAS (REQ-002/003).

    Con nombre: si esa conexion esta apagada se rechaza con `DisabledConnectionError` (no se abre nada). Sin nombre: la predeterminada si esta activa y, si no, la primera
    activa del tipo; si TODAS estan apagadas, `DisabledConnectionError`. Un tipo sin ninguna conexion configurada sigue dando `ConnectionLookupError`, como siempre."""
    conn = connections.resolve(type_id, name)  # valida el nombre y lista los validos si no existe
    if name is not None and str(name).strip():
        assert_connection_enabled(conn.type_id, conn.name)
        return conn
    off = disabled_connection_keys()
    if connection_key(conn.type_id, conn.name) not in off:
        return conn
    for other in connections.list_connections():
        if other.type_id == type_id and connection_key(other.type_id, other.name) not in off:
            return other
    assert_connection_enabled(conn.type_id, conn.name)  # todas apagadas: lanza el aviso claro
    return conn


def enabled_names_for(type_id: str) -> list[str]:
    """Nombres de las conexiones ACTIVAS de un tipo (para el prompt y para el parametro `connection`)."""
    off = disabled_connection_keys()
    return [c.name for c in connections.list_connections() if c.type_id == type_id and connection_key(c.type_id, c.name) not in off]


# ---------------------------------------------------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------------------------------------------------
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")


def disabled_skill_names() -> set[str]:
    return set(_read_list(SKILLS_DISABLED_KEY))


def is_skill_enabled(name: str) -> bool:
    return name not in disabled_skill_names()


def set_skill_enabled(name: str, enabled: bool, known_names: set[str]) -> None:
    """Prende o apaga UNA skill. El nombre tiene que ser de una skill REAL (`known_names`): ValueError si no."""
    if name not in known_names or not _SKILL_NAME_RE.match(name):
        raise ValueError("Esa skill no existe.")
    current = disabled_skill_names()
    if enabled:
        current.discard(name)
    else:
        current.add(name)
    _write_list(SKILLS_DISABLED_KEY, list(current))
