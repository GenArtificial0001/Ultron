"""Conexiones CON NOMBRE y tipos de conexion extensibles (spec conexiones-con-nombre, REQ-001/002/003/004/011/012).

Por que existe: Vision tenia UNA conexion por tipo (un Oracle, un SharePoint, un SAP) leida de variables globales de `Settings`. El usuario necesita varias del
mismo tipo con nombre ("Sharepoint - Sitio Logistico", "Oracle - Datamart") y poder sumar OTRO TIPO mas adelante sin tocar el nucleo (REQ-012).

Piezas:
  * `ConnectionType` + `register_type()`: el REGISTRO de tipos. Oracle, SharePoint, SAP y Graph se registran por esta misma via (ninguno es un caso especial
    del nucleo); un tipo nuevo es un `register_type(...)` y ya aparece en el listado, en `resolve()` y en `describe_all()`.
  * `connections.json` en `~/.vision/` (carpeta del usuario) (un bloque por conexion, decision del usuario): `{"connections": [{"type": "oracle", "name": "Datamart", "default": true,
    "fields": {"oracle_user": "...", ...}}]}`. Los secretos van DENTRO del bloque (decision explicita del usuario); por eso el archivo vive FUERA
    del repo (carpeta del usuario) y nunca viaja con el codigo, con prod ni con el .exe. Las claves de `fields` son las MISMAS de `Settings` (oracle_user, azure_client_secret, ...).
  * Compatibilidad (REQ-001): si el archivo no trae ninguna conexion de un tipo, se arma UNA con el `.env` de hoy y el nombre acordado
    (Oracle="Datamart", SharePoint="Sitio Logistico", SAP="Principal"); Graph usa las credenciales Azure de "Sitio Logistico" y no tiene bloque propio.
  * Relectura sin reiniciar (REQ-011): cada consulta compara mtime/tamano del archivo; un archivo roto NO tumba las conexiones que ya andaban: se
    conserva la ultima lectura valida y el error queda en `last_error`.

Decisiones de seguridad:
  * Una conexion del archivo es AUTOCONTENIDA: `settings_for()` parte de los valores por defecto de la clase `Settings`, no del `.env`. Si "Oracle - Otra"
    omite la contrasena NO hereda la de "Datamart": quedaria usando en silencio las credenciales de otro sistema.
  * `describe_all()` (lo que llega a la API, al sidebar y al system prompt) NUNCA devuelve valores de campos: solo tipo, nombre, estado y NOMBRES de campos
    que faltan (REQ-004).

BigQuery queda FUERA del alcance (decision del usuario): sigue usando `Settings` global y su key de archivo."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Decision del usuario 2026-10-05: el archivo vive en la carpeta de DATOS de Vision (junto al indice maestro `vision.db`), no junto al codigo:
# sobrevive a reinstalar Vision, no viaja con el codigo ni con el .exe, y cada persona tiene el suyo. `VISION_CONNECTIONS_FILE` lo mueve (tests, otro perfil).
def _default_connections_file() -> Path:
    import os

    from app.config import BASE_DIR  # Vision-personal: nunca `~/.vision`, que es de otro Vision instalado en la misma PC

    override = os.environ.get("VISION_CONNECTIONS_FILE", "").strip()
    return Path(override) if override else BASE_DIR / "connections.json"


CONNECTIONS_FILE: Path = _default_connections_file()

MAX_NAME_LENGTH = 60  # decision del usuario 2026-10-04 (antes se habia propuesto 40)
MAX_PER_TYPE = 10  # decision del usuario 2026-10-04
# letras (con tildes), numeros, espacios y guion; sin guion bajo ni signos. Empieza con letra o numero.
_NAME_RE = re.compile(r"^[^\W_][^\W_ \-]*(?:[ \-]+[^\W_]+)*$", re.UNICODE)


class ConnectionLookupError(Exception):
    """El nombre pedido no existe (o el tipo no tiene ninguna conexion). El texto lista los nombres validos para que el modelo corrija."""


# ---------------------------------------------------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ConnectionField:
    """Un campo de un tipo. `key` coincide con el atributo de `Settings` (asi `settings_for()` no necesita traduccion)."""

    key: str
    label: str
    secret: bool = False
    required: bool = False
    structured: bool = False  # el valor en el archivo es una lista o un bloque (no un texto): se guarda como JSON dentro de `fields`


@dataclass(frozen=True)
class ConnectionType:
    """Define un tipo de conexion. Agregar uno = `register_type(ConnectionType(...))` (REQ-012).

    `legacy_name`/`legacy_probe`: como armar la conexion unica de hoy desde el `.env` (None = el tipo no tiene forma legada).
    `derived_from`: (tipo, nombre) de otra conexion de la que toma sus credenciales; el tipo no lleva bloque propio (Graph).
    `tester`: funcion `Settings -> bool` que hace una llamada minima real; None = el tipo no se puede probar.
    `enabled_by`: nombre del interruptor de `Settings` que lo apaga (ej. `enable_oracle`); vacio = siempre encendido (un tipo nuevo no necesita interruptor).
    `personal_fields`: campos PERSONALES de cada usuario que salen de SU `.env` (via `Settings`), nunca del archivo compartido (ej. el token de Confluence).
    Si aparecen en `connections.json` se ignoran con un aviso. `required` cuenta para el estado y `redact` los enmascara.
    `normalize`: valida y limpia los campos de un bloque del archivo; devuelve (campos limpios, mensajes de error sin valores).
    `public_info`: informacion NO secreta de una conexion para la pantalla (ej. las secciones de Confluence); nunca debe devolver credenciales.
    `legacy_fields`: arma campos EXTRA de la conexion legada a partir del `.env` (ej. la seccion de Confluence que viene de CONFLUENCE_ROOT_PAGE_ID).
    `name_setting`: atributo de `Settings` (ej. `oracle_conexion_name`, variable ORACLE_CONEXION_NAME) con el NOMBRE de la conexion armada desde el `.env`; vacio o
    invalido = `legacy_name`."""

    type_id: str
    label: str
    fields: tuple[ConnectionField, ...] = ()
    legacy_name: str = "Principal"
    legacy_probe: tuple[str, ...] = ()
    derived_from: tuple[str, str] | None = None
    tester: Callable[[Settings], bool] | None = None
    enabled_by: str = ""
    personal_fields: tuple[ConnectionField, ...] = ()
    normalize: Callable[[dict[str, str]], tuple[dict[str, str], list[str]]] | None = None
    public_info: Callable[[Connection], dict[str, Any]] | None = None
    legacy_fields: Callable[[Settings], dict[str, str]] | None = None
    name_setting: str = ""


@dataclass(frozen=True)
class Connection:
    """Una conexion concreta. `fields` puede tener secretos: no se imprime, no se loguea y NO sale de este modulo salvo por `settings_for()`."""

    type_id: str
    name: str
    default: bool = False
    from_file: bool = True
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    # spec gestor-conexiones: «vision» = viene de la base cifrada (`config_store`). Se comporta como una del archivo (`from_file=True`: sus datos propios via
    # `settings_for`). `unreadable` = este usuario de Windows no pudo descifrarla (otra PC u otro perfil): figura sin datos y se pide volver a cargarla.
    origin: str = ""
    unreadable: bool = False

    @property
    def display(self) -> str:
        """Nombre visible: `<Tipo> - <nombre>`, ej. «SharePoint - Sitio Logistico»."""
        return (
            f"{_TYPES[self.type_id].label} - {self.name}" if self.type_id in _TYPES else f"{self.type_id} - {self.name}"
        )


_TYPES: dict[str, ConnectionType] = {}
_types_lock = threading.Lock()


def register_type(conn_type: ConnectionType) -> None:
    """Registra (o reemplaza) un tipo de conexion. Es la UNICA puerta para sumar un tipo nuevo (REQ-012)."""
    with _types_lock:
        _TYPES[conn_type.type_id] = conn_type
    _invalidate()


def unregister_type(type_id: str) -> None:
    """Quita un tipo (lo usan los tests para no dejar un tipo de prueba registrado)."""
    with _types_lock:
        _TYPES.pop(type_id, None)
    _invalidate()


def get_types() -> dict[str, ConnectionType]:
    return dict(_TYPES)


def _register_builtin_types() -> None:
    # import diferido de los conectores: solo se necesitan al probar la conexion, y asi este modulo no arrastra pandas/oracledb al importarse
    def _oracle(s: Settings) -> bool:
        from app.connectors.oracle_connector import OracleConnector

        return OracleConnector(s).test_connection()

    def _sharepoint(s: Settings) -> bool:
        from app.connectors.sharepoint_connector import SharePointConnector

        return SharePointConnector(s).test_connection()

    def _graph(s: Settings) -> bool:
        from app.connectors.graph_connector import GraphConnector

        return GraphConnector(s).test_connection()

    def _sap(s: Settings) -> bool:
        from app.connectors.sap_connector import SapConnector

        return SapConnector(s).test_connection()

    register_type(
        ConnectionType(
            "oracle",
            "Oracle",
            (
                ConnectionField("oracle_user", "Usuario", required=True),
                ConnectionField("oracle_password", "Contrasena", secret=True, required=True),
                ConnectionField("oracle_host", "Host", required=True),
                ConnectionField("oracle_port", "Puerto"),
                ConnectionField("oracle_service_name", "Service name", required=True),
                ConnectionField("oracle_schema", "Schema"),
            ),
            legacy_name="Datamart",
            legacy_probe=("oracle_host", "oracle_user"),
            tester=_oracle,
            enabled_by="enable_oracle",
            name_setting="oracle_conexion_name",
        )
    )
    register_type(
        ConnectionType(
            "sharepoint",
            "SharePoint",
            (
                ConnectionField("sharepoint_site_url", "Site URL", required=True),
                ConnectionField("azure_client_id", "Client ID", required=True),
                ConnectionField("azure_client_secret", "Client secret", secret=True, required=True),
                ConnectionField("azure_tenant_id", "Tenant ID", required=True),
                ConnectionField("sharepoint_default_list_name", "Lista por defecto"),
            ),
            legacy_name="Sitio Logistico",
            legacy_probe=("sharepoint_site_url", "azure_client_id"),
            tester=_sharepoint,
            enabled_by="enable_sharepoint",
            name_setting="sharepoint_conexion_name",
        )
    )
    register_type(
        ConnectionType(
            "sap",
            "SAP",
            (
                ConnectionField("sap_base_url", "URL base", required=True),
                ConnectionField("sap_client", "Mandante"),
                ConnectionField("sap_user", "Usuario", required=True),
                ConnectionField("sap_password", "Contrasena", secret=True, required=True),
                ConnectionField("sap_language", "Idioma"),
            ),
            legacy_name="Principal",
            legacy_probe=("sap_base_url",),
            tester=_sap,
            enabled_by="enable_sap",
            name_setting="sap_conexion_name",
        )
    )
    # Confluence (Fase B): su tipo se define en su propio modulo y se registra por la MISMA puerta que los otros (REQ-012).
    from app.connectors.confluence_connector import register_confluence_type

    register_confluence_type()
    # Graph no tiene bloque propio (decision del usuario 2026-10-04): usa las credenciales Azure de «Sitio Logistico».
    register_type(
        ConnectionType(
            "graph",
            "Microsoft Graph",
            (),
            legacy_name="Principal",
            derived_from=("sharepoint", "Sitio Logistico"),
            tester=_graph,
            enabled_by="enable_sharepoint",  # Graph vive y muere con SharePoint, como siempre
            name_setting="graph_conexion_name",
        )
    )


# ---------------------------------------------------------------------------------------------------------------------
# Lectura del archivo (con relectura sin reiniciar)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class _Snapshot:
    connections: list[Connection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_lock = threading.RLock()
_file_state: tuple[int, int] | None = None  # (mtime_ns, tamano) de la ultima lectura que se TRAJO (buena o mala)
_good_file_connections: list[Connection] | None = None  # ultima lectura VALIDA del archivo
_last_error: str | None = None


def _invalidate() -> None:
    global _file_state
    with _lock:
        _file_state = None


def reset_for_tests() -> None:
    global _file_state, _good_file_connections, _last_error
    with _lock:
        _file_state = None
        _good_file_connections = None
        _last_error = None


def display_path() -> str:
    """Ruta del archivo para MOSTRAR en la pantalla: si esta bajo la carpeta del usuario sale como `~/...` (no se expone el nombre de usuario de Windows)."""
    path = CONNECTIONS_FILE
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


_FORBIDDEN_LABEL_CHARS = frozenset("{}<>" + chr(34) + chr(39) + chr(96) + chr(92))


def valid_label(text: str) -> bool:
    """Etiqueta libre (ej. el nombre de una seccion de Confluence como «MP&L Procedures»): 1 a 60 caracteres imprimibles, sin espacios en los extremos y
    sin llaves, comillas, acentos graves, barras invertidas ni signos < >. Admite `&`, `.` y `,`, que `valid_name` no admite."""
    if not text or text != text.strip() or len(text) > MAX_NAME_LENGTH:
        return False
    return all(c.isprintable() and c not in _FORBIDDEN_LABEL_CHARS for c in text)


def valid_name(name: str) -> bool:
    """Reglas del nombre: 1 a 60 caracteres, letras/numeros/espacios/guion, sin espacios en los extremos (spec, categoria 6)."""
    return bool(name) and name == name.strip() and len(name) <= MAX_NAME_LENGTH and bool(_NAME_RE.match(name))


def _parse_file_text(text: str) -> tuple[list[Connection], list[str]]:
    """Convierte el JSON en conexiones. Devuelve (conexiones validas, errores). Un bloque malo se descarta SIN tumbar a los demas."""
    data = json.loads(text)
    items = data.get("connections") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('el archivo debe ser {"connections": [ ... ]} o una lista de conexiones')
    out: list[Connection] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    per_type: dict[str, int] = {}
    for i, raw in enumerate(items, start=1):
        label = f"conexion #{i}"
        if not isinstance(raw, dict):
            errors.append(f"{label}: no es un bloque valido")
            continue
        type_id = str(raw.get("type", "")).strip().lower()
        name = str(raw.get("name", ""))
        ctype = _TYPES.get(type_id)
        if ctype is None:
            errors.append(f"{label}: tipo desconocido '{type_id}'")
            continue
        if ctype.derived_from is not None:
            errors.append(
                f"{label}: el tipo '{type_id}' no lleva bloque propio (usa las credenciales de otra conexion)"
            )
            continue
        if not valid_name(name):
            errors.append(
                f"{label} ({type_id}): nombre invalido (1 a {MAX_NAME_LENGTH} caracteres: letras, numeros, espacios y guion)"
            )
            continue
        key = (type_id, name.lower())
        if key in seen:
            errors.append(f"{label}: ya existe {ctype.label} - {name} (los nombres son unicos por tipo)")
            continue
        if per_type.get(type_id, 0) >= MAX_PER_TYPE:
            errors.append(f"{label}: se supero el tope de {MAX_PER_TYPE} conexiones de tipo {ctype.label}")
            continue
        raw_fields = raw.get("fields") or {}
        if not isinstance(raw_fields, dict):
            errors.append(f"{label}: 'fields' debe ser un bloque de campos")
            continue
        known = {f.key for f in ctype.fields}
        personal = {f.key for f in ctype.personal_fields}
        in_file_personal = sorted(set(raw_fields) & personal)
        if in_file_personal:
            # solo el NOMBRE del campo: nunca se lee ni se muestra su valor
            errors.append(
                f"{ctype.label} - {name}: {', '.join(in_file_personal)} es personal de cada usuario: va en tu .env, NO en connections.json (se ignora)"
            )
        unknown = sorted(set(raw_fields) - known - personal)
        if unknown:
            errors.append(f"{ctype.label} - {name}: campos desconocidos ignorados: {', '.join(unknown)}")
        structured = {f.key for f in ctype.fields if f.structured}
        fields: dict[str, str] = {}
        for k in raw_fields:
            if k not in known:
                continue
            value = raw_fields[k]
            if k in structured and not isinstance(value, str):
                fields[k] = json.dumps(value, ensure_ascii=False)
            else:
                fields[k] = "" if value is None else str(value)
        if ctype.normalize is not None:
            fields, normalize_errors = ctype.normalize(fields)
            errors.extend(f"{ctype.label} - {name}: {message}" for message in normalize_errors)
        seen.add(key)
        per_type[type_id] = per_type.get(type_id, 0) + 1
        out.append(Connection(type_id, name, bool(raw.get("default", False)), True, fields))
    return out, errors


def _read_file_if_changed() -> None:
    """Relee el archivo si cambio (mtime/tamano). Un archivo roto conserva la ultima lectura valida y deja el error en `last_error`."""
    global _file_state, _good_file_connections, _last_error
    path = CONNECTIONS_FILE
    try:
        st = path.stat()
        state: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        state = None
    if state == _file_state:
        return
    _file_state = state
    if state is None:  # no hay archivo: solo vale el .env de hoy
        _good_file_connections = None
        _last_error = None
        return
    try:
        conns, errors = _parse_file_text(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:  # JSONDecodeError es ValueError
        _last_error = f"connections.json no se pudo leer: {exc}"
        logger.warning("%s (se conserva la ultima lectura valida)", _last_error)
        return
    _good_file_connections = conns
    _last_error = "; ".join(errors) if errors else None
    if errors:
        logger.warning("connections.json: %s", _last_error)


def last_error() -> str | None:
    """Ultimo problema al leer el archivo o los nombres del `.env` (None si esta todo bien). Sin valores de campos."""
    if _store_active():
        return None  # spec gestor-conexiones REQ-003: con la base activa ni connections.json ni los nombres del .env se usan; no hay nada que avisar de ellos
    with _lock:
        _read_file_if_changed()
        file_error = _last_error
    return "; ".join(p for p in [file_error, *env_name_problems()] if p) or None


def _env_name(type_id: str, settings: Settings | None = None) -> str:
    """Nombre configurado en el `.env` para la conexion de este tipo (ORACLE_CONEXION_NAME, ...), o "" si esta vacio o no es valido."""
    ctype = _TYPES.get(type_id)
    if ctype is None or not ctype.name_setting:
        return ""
    raw = str(getattr(settings if settings is not None else get_settings(), ctype.name_setting, "") or "").strip()
    return raw if raw and valid_name(raw) else ""


def configured_name(type_id: str, settings: Settings | None = None) -> str:
    """Nombre de la conexion armada desde el `.env`: el de la variable *_CONEXION_NAME si es valido, y si no el nombre por defecto del tipo.
    Es lo que se muestra despues del guion («Oracle - Datamart»). Un valor invalido nunca llega a la pantalla ni al prompt: se usa el de siempre."""
    ctype = _TYPES.get(type_id)
    return (_env_name(type_id, settings) or ctype.legacy_name) if ctype is not None else ""


def env_name_problems() -> list[str]:
    """Avisos sobre los *_CONEXION_NAME del `.env` que no son validos. Solo nombra la VARIABLE, nunca su valor."""
    settings = get_settings()
    out: list[str] = []
    checks = [(c.name_setting, c.legacy_name) for c in _TYPES.values() if c.name_setting] + [("bigquery_conexion_name", "")]
    for attr, default in checks:
        raw = str(getattr(settings, attr, "") or "").strip()
        if raw and not valid_name(raw):
            fallback = f"se usa «{default}»" if default else "se muestra solo «BigQuery»"
            out.append(f"{attr.upper()} no es un nombre valido (1 a {MAX_NAME_LENGTH} caracteres: letras, numeros, espacios y guion); {fallback}")
    return out


def bigquery_display() -> str:
    """Titulo de BigQuery para el panel: «BigQuery - nombre» si BIGQUERY_CONEXION_NAME es valido, y «BigQuery» si no. BigQuery no tiene conexiones con nombre."""
    raw = str(getattr(get_settings(), "bigquery_conexion_name", "") or "").strip()
    return f"BigQuery - {raw}" if raw and valid_name(raw) else "BigQuery"


def _legacy_connections(base: Settings, covered: set[str]) -> list[Connection]:
    """Una conexion por tipo con el `.env` de hoy, para los tipos que el archivo NO cubre (REQ-001)."""
    out: list[Connection] = []
    for ctype in _TYPES.values():
        if ctype.type_id in covered or ctype.derived_from is not None or not ctype.legacy_probe:
            continue
        if not any(str(getattr(base, attr, "") or "").strip() for attr in ctype.legacy_probe):
            continue
        fields = {f.key: str(getattr(base, f.key, "") or "") for f in ctype.fields}
        if ctype.legacy_fields is not None:
            fields.update(ctype.legacy_fields(base))
        out.append(Connection(ctype.type_id, configured_name(ctype.type_id, base), True, False, fields))
    return out


def _derived_connections(existing: list[Connection]) -> list[Connection]:
    """Graph: una conexion que toma las credenciales de otra (por nombre; si no esta, la predeterminada de ese tipo)."""
    settings = get_settings()
    out: list[Connection] = []
    for ctype in _TYPES.values():
        if ctype.derived_from is None:
            continue
        src_type, src_name = ctype.derived_from
        src_name = _env_name(src_type, settings) or src_name  # la SharePoint que Graph usa es la que se llama como SHAREPOINT_CONEXION_NAME
        chosen = _store_derived_source(ctype.type_id)
        if chosen:
            src_name = chosen  # spec gestor-conexiones (regla 6): con la base activa, la SharePoint que el usuario eligio en la tarjeta de Graph
        candidates = [c for c in existing if c.type_id == src_type]
        source = (
            next((c for c in candidates if c.name.lower() == src_name.lower()), None)
            or next((c for c in candidates if c.default), None)
            or (candidates[0] if candidates else None)
        )
        if source is not None:
            out.append(
                Connection(
                    ctype.type_id, configured_name(ctype.type_id, settings), True, source.from_file, dict(source.fields), source.origin, source.unreadable
                )
            )
    return out


def _store_active() -> bool:
    from app import config_store  # import diferido: config_store importa este modulo

    return config_store.is_active()


def _store_derived_source(type_id: str) -> str:
    """Nombre de la conexion de origen que el usuario eligio para un tipo derivado (Graph), guardado en la configuracion general; "" = la de siempre."""
    if not _store_active():
        return ""
    from app import config_store  # import diferido: config_store importa este modulo

    return config_store.get_general().get(f"{type_id}_source", "").strip()


def _store_connections() -> list[Connection]:
    """spec gestor-conexiones REQ-003/004: las conexiones de la base cifrada. Un tipo que ya no esta registrado se omite (no rompe la lista)."""
    from app import config_store  # import diferido: config_store importa este modulo

    return [
        Connection(c.type_id, c.name, c.default, True, dict(c.fields), "vision", c.undecryptable)
        for c in config_store.list_stored()
        if c.type_id in _TYPES and _TYPES[c.type_id].derived_from is None
    ]


def list_connections() -> list[Connection]:
    """Todas las conexiones vigentes, con los secretos DENTRO: uso interno.

    Con la base cifrada ACTIVA (spec gestor-conexiones REQ-003) es la UNICA fuente: ni `connections.json` ni el `.env` se leen. Si no, lo de siempre: archivo + `.env`
    para lo no cubierto. En los dos casos se agregan las derivadas (Graph)."""
    if _store_active():
        conns = _with_one_default_per_type(_store_connections())
        return conns + _derived_connections(conns)
    with _lock:
        _read_file_if_changed()
        file_conns = list(_good_file_connections or [])
    covered = {c.type_id for c in file_conns}
    base = get_settings()
    conns = file_conns + _legacy_connections(base, covered)
    conns = _with_one_default_per_type(conns)
    return conns + _derived_connections(conns)


def _with_one_default_per_type(conns: list[Connection]) -> list[Connection]:
    """Garantiza UNA predeterminada por tipo: la marcada (la primera si hay varias marcadas) o, si ninguna, la primera del tipo."""
    chosen: dict[str, str] = {}
    for c in conns:
        if c.default and c.type_id not in chosen:
            chosen[c.type_id] = c.name
    for c in conns:
        chosen.setdefault(c.type_id, c.name)
    return [replace(c, default=chosen[c.type_id] == c.name) for c in conns]


def names_for(type_id: str) -> list[str]:
    return [c.name for c in list_connections() if c.type_id == type_id]


def resolve(type_id: str, name: str | None = None) -> Connection:
    """Devuelve la conexion pedida o la predeterminada del tipo (REQ-003). Un nombre inexistente da un error claro con los nombres validos."""
    candidates = [c for c in list_connections() if c.type_id == type_id]
    ctype = _TYPES.get(type_id)
    label = ctype.label if ctype else type_id
    if not candidates:
        raise ConnectionLookupError(f"No hay ninguna conexion de tipo {label} configurada.")
    if name is None or not str(name).strip():
        return next((c for c in candidates if c.default), candidates[0])
    wanted = str(name).strip().lower()
    for c in candidates:
        if c.name.lower() == wanted:
            return c
    valid = ", ".join(f"'{c.name}'" for c in candidates)
    raise ConnectionLookupError(f"No existe la conexion '{name}' de tipo {label}. Las disponibles son: {valid}.")


# ---------------------------------------------------------------------------------------------------------------------
# Settings por conexion y descripcion sin secretos
# ---------------------------------------------------------------------------------------------------------------------
def _coerce(attr: str, value: str) -> Any:
    if attr not in Settings.model_fields:
        return value  # campo de un tipo registrado despues: no existe en Settings, se entrega tal cual
    default = Settings.model_fields[attr].default
    if isinstance(default, bool):
        return value.strip().lower() in ("1", "true", "si", "yes")
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            return default
    return value


def settings_for(conn: Connection) -> Settings:
    """`Settings` para usar con los conectores existentes. Parte de los valores por defecto de la CLASE, no del `.env`, para que la conexion sea
    autocontenida (ver docstring del modulo); lo que no es de este tipo (proxy, flags, rutas) se hereda del `.env`."""
    ctype = _TYPES[conn.type_id]
    if ctype.derived_from is not None:
        # una conexion derivada (Graph) no tiene campos propios: se arma con los del tipo de origen, cuyos valores ya trae `conn.fields`
        ctype = _TYPES[ctype.derived_from[0]]
    updates: dict[str, Any] = {}
    for f in ctype.fields:
        value = conn.fields.get(f.key, "")
        if f.key in Settings.model_fields:
            updates[f.key] = _coerce(f.key, value) if value != "" else Settings.model_fields[f.key].default
        else:
            updates[f.key] = value  # tipo registrado despues (REQ-012): sus campos viajan en el objeto de settings aunque Settings no los declare
    if conn.origin == "vision":
        # spec gestor-conexiones (decision del usuario: cada conexion con SUS credenciales): en la base, los campos personales (correo y token de Confluence) viajan
        # dentro de la conexion. Las de connections.json no los traen (se descartan al leer el archivo) y siguen saliendo del `.env` de cada usuario, como siempre.
        for f in ctype.personal_fields:
            value = conn.fields.get(f.key, "")
            if value:
                updates[f.key] = value
    return get_settings().model_copy(update=updates)


def is_enabled(type_id: str) -> bool:
    """False si el interruptor `ENABLE_*` del tipo esta apagado en el `.env` (sin interruptor = encendido)."""
    ctype = _TYPES.get(type_id)
    if ctype is None or not ctype.enabled_by:
        return True
    return bool(getattr(get_settings(), ctype.enabled_by, True))


def missing_fields(conn: Connection) -> list[str]:
    """NOMBRES de los campos obligatorios que faltan (nunca valores)."""
    ctype = _TYPES.get(conn.type_id)
    if ctype is None:
        return []
    missing = [f.key for f in ctype.fields if f.required and not conn.fields.get(f.key, "").strip()]
    settings = get_settings()
    for f in ctype.personal_fields:
        if not f.required:
            continue
        own = conn.fields.get(f.key, "").strip() if conn.origin == "vision" else ""  # gestor-conexiones: en la base, los personales son de la conexion
        if not own and not str(getattr(settings, f.key, "") or "").strip():
            missing.append(f.key)
    return missing


def parse_confluence_sections(conn: Connection) -> list[dict[str, str]]:
    """Secciones (nombre, pagina raiz) de una conexion de Confluence, ya validadas por el normalizador del tipo."""
    from app.connectors.confluence_connector import parse_sections  # import diferido: el conector importa este modulo

    return parse_sections(conn.fields.get("sections", ""))


def personal_field_keys(type_id: str) -> frozenset[str]:
    """Claves de los campos personales de un tipo (los que se completan en el `.env`, no en connections.json)."""
    ctype = _TYPES.get(type_id)
    return frozenset(f.key for f in ctype.personal_fields) if ctype is not None else frozenset()


_MASK = "***"
_MIN_REDACT_LEN = 3  # un valor de 1-2 caracteres (un puerto, un idioma) reemplazaria letras sueltas de cualquier mensaje: no es un secreto util de enmascarar


def redact(conn: Connection, text: str) -> str:
    """Quita de `text` TODO valor de campo de la conexion (REQ-004). Un conector que falla puede meter la contrasena, el usuario o el host en el texto de su
    excepcion (los drivers arman el DSN o la URL dentro del mensaje); ese texto llega al navegador, asi que se enmascara antes. Los valores mas largos
    primero, para que un valor que contiene a otro no deje restos."""
    candidates = {v for v in conn.fields.values() if len(v) >= _MIN_REDACT_LEN}
    ctype = _TYPES.get(conn.type_id)
    if ctype is not None and ctype.personal_fields:
        settings = get_settings()  # el token y el correo viven en el .env del usuario, no en `conn.fields`: tambien se enmascaran
        for f in ctype.personal_fields:
            personal_value = str(getattr(settings, f.key, "") or "")
            if len(personal_value) >= _MIN_REDACT_LEN:
                candidates.add(personal_value)
    for value in sorted(candidates, key=len, reverse=True):
        text = text.replace(value, _MASK)
    return text


def describe_all() -> list[dict[str, Any]]:
    """Lo que ve la API, el sidebar y el system prompt: tipo, nombre, estado y campos que faltan. NUNCA valores de campos (REQ-004)."""
    out: list[dict[str, Any]] = []
    for c in list_connections():
        ctype = _TYPES[c.type_id]
        missing = missing_fields(c)
        entry: dict[str, Any] = {
            "type": c.type_id,
            "type_label": ctype.label,
            "name": c.name,
            "display": c.display,
            "default": c.default,
            "source": "vision" if c.origin == "vision" else ("archivo" if c.from_file else "env"),
            "status": "incomplete" if missing else "configured",
            "unreadable": c.unreadable,  # gestor-conexiones: otra PC u otro perfil de Windows; la pantalla pide volver a cargarla
            "missing": missing,
            "testable": ctype.tester is not None,
        }
        if ctype.public_info is not None:
            info = ctype.public_info(c)
            if info:
                entry["info"] = info  # solo datos NO secretos (ej. secciones de Confluence)
        out.append(entry)
    return out


def describe_for_prompt() -> str:
    """Bloque de texto para el system prompt (REQ-005): que conexiones hay y como elegirlas. SOLO tipo, nombre y cual es la predeterminada: nada de
    campos ni valores (REQ-004). Vacio si hay una sola conexion por tipo (no hay nada que elegir) o ninguna.

    El nombre lo escribe una persona en un archivo y entra en un prompt: se pasa por `valid_name` (que ya prohibe saltos de linea, comillas y llaves) y
    cualquier nombre que no lo cumpla se omite, para que no pueda inyectar instrucciones ni romper el `str.format` de la plantilla."""
    from app import switches  # import diferido: `switches` importa este modulo

    off = switches.disabled_connection_keys()
    by_type: dict[str, list[Connection]] = {}
    for conn in list_connections():
        ctype = _TYPES.get(conn.type_id)
        if ctype is None or ctype.derived_from is not None or not valid_name(conn.name):
            continue
        if switches.connection_key(conn.type_id, conn.name) in off:
            continue  # una conexion apagada no existe para el modelo (REQ-001): ni su nombre figura en el prompt
        by_type.setdefault(conn.type_id, []).append(conn)
    lines = []
    for type_id, items in by_type.items():
        if len(items) < 2:
            continue  # con una sola conexion el modelo no tiene nada que elegir
        names = ", ".join(f"{c.name}{' (predeterminada)' if c.default else ''}" for c in items)
        lines.append(f"- {_TYPES[type_id].label}: {names}")
    if not lines:
        return ""
    return (
        "Conexiones disponibles (varias del mismo tipo). Las tools de ese tipo aceptan el argumento `connection` con el nombre exacto; "
        "si el usuario no dice cual, usa la predeterminada, y si el pedido es ambiguo entre varias, preguntale con la tool `question`:\n"
        + "\n".join(lines)
    )


_register_builtin_types()
