"""Importar UNA vez el `.env`, `connections.json` y `.env.enc` a la base cifrada (spec gestor-conexiones A6: REQ-003, regla 6).

Por que existe: el usuario decidio (2026-10-08) que ni el `.env` ni `connections.json` deben existir. Quien ya tiene Vision andando con esos archivos no tiene que
volver a tipear nada: un boton importa todo a `vision.db` (cifrado con DPAPI) y desde ahi la base es la unica fuente.

Reglas (de la spec):
- «si existe en .env tiene que existir en Vision»: se importan las conexiones del `.env` Y las de `connections.json` (si un nombre se repite dentro de un tipo, la
  primera gana y se informa). El nombre es el de `*_CONEXION_NAME` si es valido, o el de siempre (Datamart, Sitio Logistico, Principal): ninguna queda sin nombre.
- Nunca pisa lo que ya esta en la base: una conexion que ya existe (mismo tipo y nombre) o un dato general ya cargado se informan como «ya estaba».
- Los `ENABLE_*` en false se convierten en interruptores APAGADOS de esas conexiones (los interruptores de la 0.26.0 los reemplazan).
- La key de BigQuery (el archivo de `GOOGLE_APPLICATION_CREDENTIALS` o la guardada en `.env.enc`) pasa a la base: el archivo deja de hacer falta.
- Los archivos NO se tocan: borrarlos es otra accion, con confirmacion (REQ-011).
- El informe lleva NOMBRES (de conexiones y de campos), nunca valores."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app import config, config_store, connections, general_settings, switches

logger = logging.getLogger(__name__)

MAX_KEY_FILE_BYTES = 100_000  # una key de cuenta de servicio pesa ~2-3 KB; algo enorme no es una key


@dataclass
class ImportReport:
    """Lo que se importo, por NOMBRE. Lo consume la pantalla tal cual."""

    sources: dict[str, bool] = field(default_factory=dict)
    connections: list[dict[str, str]] = field(default_factory=list)  # {"display", "result": "importada" | "ya estaba"}
    general: list[str] = field(default_factory=list)  # etiquetas de los datos generales importados
    general_kept: list[str] = field(default_factory=list)  # ya estaban en la base: no se pisaron
    switched_off: list[str] = field(default_factory=list)  # conexiones que el .env tenia apagadas (ENABLE_*=false)
    problems: list[str] = field(default_factory=list)  # sin valores

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "connections": self.connections,
            "general": self.general,
            "general_kept": self.general_kept,
            "switched_off": self.switched_off,
            "problems": self.problems,
        }


def env_path() -> Path:
    return config.BASE_DIR / ".env"


def legacy_sources() -> dict[str, bool]:
    """Que archivos viejos existen (para la pantalla: ofrecer importar y, despues, borrar). Solo existencia, nunca contenido."""
    from app import credentials  # import diferido: credentials importa config y conectores

    return {
        "env": env_path().is_file(),
        "connections_json": Path(connections.CONNECTIONS_FILE).is_file(),
        "env_enc": Path(credentials.ENC_FILE).is_file(),
        # revision de seguridad 2026-10-08: la key de BigQuery en `keys/` es un secreto en texto plano; tambien cuenta como archivo viejo
        "bq_key_file": bq_key_path().is_file(),
    }


def bq_key_path(values: dict[str, str] | None = None) -> Path:
    """Ruta del archivo de la key de BigQuery: la de GOOGLE_APPLICATION_CREDENTIALS del `.env` (si esta) o la de siempre, `keys/bigquery_key.json` en BASE_DIR."""
    if values is None:
        path = env_path()
        values = {k.strip().lower(): (v or "").strip() for k, v in dotenv_values(path, encoding="utf-8").items() if k} if path.is_file() else {}
    raw = values.get("google_application_credentials") or str(config.Settings.model_fields["google_application_credentials"].default)
    key_path = Path(raw)
    return key_path if key_path.is_absolute() else config.BASE_DIR / key_path


def _read_env_values() -> dict[str, str]:
    """Valores del `.env` (solo lo escrito en el archivo, no las variables del sistema), con las claves en minuscula como los atributos de `Settings`. Despues se
    superponen los del `.env.enc`, igual que en tiempo de ejecucion (`credentials.apply_credentials_to_settings`)."""
    from app import credentials  # import diferido: credentials importa config y conectores

    values: dict[str, str] = {}
    path = env_path()
    if path.is_file():
        for key, value in dotenv_values(path, encoding="utf-8").items():
            if key and value is not None:
                values[key.strip().lower()] = value.strip()
    for key, value in credentials._load_encrypted().items():  # vacio si no hay archivo o no se puede descifrar (se loguea alla)
        if value:
            values[key.lower()] = value
    return values


def _is_false(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("0", "false", "no", "off")


def _bigquery_key(values: dict[str, str], report: ImportReport) -> str:
    """Contenido de la key de BigQuery: la guardada en `.env.enc` (`bq_key_json`) o el archivo de `GOOGLE_APPLICATION_CREDENTIALS`. "" si no hay."""
    if values.get("bq_key_json"):
        return values["bq_key_json"]
    path = bq_key_path(values)
    if not path.is_file():
        return ""
    try:
        if path.stat().st_size > MAX_KEY_FILE_BYTES:
            report.problems.append("El archivo de clave de BigQuery es demasiado grande para ser una clave: no se importó.")
            return ""
        text = path.read_text(encoding="utf-8-sig")
        if not isinstance(json.loads(text), dict):
            raise ValueError
        return text
    except (OSError, ValueError):
        report.problems.append("El archivo de clave de BigQuery no se pudo leer como una clave válida: no se importó.")
        return ""


def _legacy_blocks(values: dict[str, str]) -> list[tuple[str, str, dict[str, str]]]:
    """Una conexion por tipo armada desde el `.env` (misma regla que `connections._legacy_connections`): (tipo, nombre, campos)."""
    out: list[tuple[str, str, dict[str, str]]] = []
    for ctype in connections.get_types().values():
        if ctype.derived_from is not None or not ctype.legacy_probe:
            continue
        if not any(values.get(attr, "").strip() for attr in ctype.legacy_probe):
            continue
        fields = {f.key: values[f.key] for f in (*ctype.fields, *ctype.personal_fields) if values.get(f.key)}
        if ctype.legacy_fields is not None:
            fields.update({k: v for k, v in ctype.legacy_fields(_values_as_settings(values)).items() if v})
        raw_name = values.get(ctype.name_setting, "").strip() if ctype.name_setting else ""
        name = raw_name if raw_name and connections.valid_name(raw_name) else ctype.legacy_name
        out.append((ctype.type_id, name, fields))
    return out


def _values_as_settings(values: dict[str, str]) -> Any:
    """Un objeto con los atributos que leen los `legacy_fields` de un tipo (ej. la pagina raiz de Confluence). Sin variables del sistema."""
    known = {k: v for k, v in values.items() if k in config.Settings.model_fields}
    try:
        return config.Settings(_env_file=None, **known)
    except Exception:  # noqa: BLE001 -- un valor raro del .env (ej. un puerto con letras) no debe frenar la importacion
        return config.Settings(_env_file=None)


def _file_blocks(values: dict[str, str], report: ImportReport) -> list[tuple[str, str, dict[str, str]]]:
    """Las conexiones de `connections.json`. Las de Confluence no traen el correo ni el token (son personales): se completan con los del `.env`, como en ejecucion."""
    path = Path(connections.CONNECTIONS_FILE)
    if not path.is_file():
        return []
    try:
        parsed, errors = connections._parse_file_text(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        report.problems.append("connections.json no se pudo leer: sus conexiones no se importaron.")
        return []
    report.problems.extend(f"connections.json: {e}" for e in errors)  # los errores del parser ya vienen sin valores
    out = []
    for conn in parsed:
        fields = dict(conn.fields)
        for key in connections.personal_field_keys(conn.type_id):
            if values.get(key) and not fields.get(key):
                fields[key] = values[key]
        out.append((conn.type_id, conn.name, fields))
    return out


def import_legacy() -> ImportReport:
    """Importa todo lo que haya en los archivos viejos a la base cifrada y la ACTIVA. Se puede repetir: nunca duplica ni pisa (REQ-003)."""
    report = ImportReport(sources=legacy_sources())
    values = _read_env_values()

    existing = {(c.type_id, c.name.lower()) for c in config_store.list_stored()}
    imported: list[tuple[str, str]] = []
    for type_id, name, fields in _file_blocks(values, report) + _legacy_blocks(values):
        display = f"{connections.get_types()[type_id].label} - {name}"
        if (type_id, name.lower()) in existing:
            report.connections.append({"display": display, "result": "ya estaba"})
            continue
        try:
            config_store.add_connection(type_id, name, fields)
        except config_store.ConfigStoreError as exc:
            report.problems.append(f"{display}: {exc}")
            continue
        existing.add((type_id, name.lower()))
        imported.append((type_id, name))
        report.connections.append({"display": display, "result": "importada"})

    stored_general = config_store.get_general()
    updates: dict[str, str] = {}
    for gf in general_settings.GENERAL_FIELDS:
        value = _bigquery_key(values, report) if gf.key == "bq_key_json" else values.get(gf.key, "")
        if not value:
            continue
        if stored_general.get(gf.key):
            report.general_kept.append(gf.label)
            continue
        updates[gf.key] = value
    if updates:
        try:
            clean = general_settings.validate(updates)
        except config_store.ConfigStoreError as exc:
            # un valor invalido (ej. un puerto con letras) no frena el resto: se importa campo por campo
            report.problems.append(str(exc))
            clean = {}
            for key, value in updates.items():
                try:
                    clean.update(general_settings.validate({key: value}))
                except config_store.ConfigStoreError as one:
                    report.problems.append(str(one))
        if clean:
            config_store.set_general(clean)
            report.general = [general_settings.field_for(k).label for k in clean]  # type: ignore[union-attr]

    config_store.activate()  # aunque no hubiera nada que importar: el usuario eligio la base como unica fuente
    _apply_now()

    for type_id, name in imported:
        ctype = connections.get_types()[type_id]
        if ctype.enabled_by and _is_false(values.get(ctype.enabled_by)):
            try:
                switches.set_connection_enabled(switches.connection_key(type_id, name), False)
                report.switched_off.append(f"{ctype.label} - {name}")
            except ValueError:
                logger.warning("No se pudo apagar la conexion importada %s - %s", ctype.label, name)
    if updates.get("bq_project_id") and _is_false(values.get("enable_bigquery")):
        try:
            switches.set_connection_enabled(switches.BIGQUERY_KEY, False)
            report.switched_off.append(connections.bigquery_display())
        except ValueError:
            logger.warning("No se pudo apagar BigQuery importada")
    return report


def delete_legacy_files() -> dict[str, list[str]]:
    """spec gestor-conexiones REQ-011 (Fase C): «ni el .env ni connections.json deben existir». Borra los archivos viejos YA importados. Solo lo llama la API con la
    base ACTIVA (si no, se perderian esas conexiones) y despues de que la pantalla pidio confirmacion. No deja copias: la base ya tiene todo, cifrado.
    Devuelve los NOMBRES borrados y los problemas (sin rutas completas ni valores)."""
    from app import credentials  # import diferido: credentials importa config y conectores

    deleted: list[str] = []
    problems: list[str] = []
    key_path = bq_key_path()  # antes de borrar el .env, que puede decir donde esta
    for label, path in ((".env", env_path()), ("connections.json", Path(connections.CONNECTIONS_FILE)), (".env.enc", Path(credentials.ENC_FILE))):
        if not path.is_file():
            continue
        try:
            path.unlink()
            deleted.append(label)
        except OSError:
            logger.warning("No se pudo borrar %s", label, exc_info=True)
            problems.append(f"No se pudo borrar {label}: puede estar abierto en otro programa. Cerralo y probá de nuevo.")
    if key_path.is_file():
        _delete_bq_key_file(key_path, deleted, problems)
    return {"deleted": deleted, "problems": problems}


def _delete_bq_key_file(key_path: Path, deleted: list[str], problems: list[str]) -> None:
    """Borra el archivo de la key de BigQuery SOLO si es la misma key que ya esta guardada (cifrada) en Vision: nunca se borra una key distinta o nueva."""
    label = "la clave de BigQuery (.json)"
    stored = config_store.get_general().get("bq_key_json", "")
    try:
        same = bool(stored) and key_path.stat().st_size <= MAX_KEY_FILE_BYTES and json.loads(key_path.read_text(encoding="utf-8-sig")) == json.loads(stored)
    except (OSError, ValueError):
        same = False
    if not same:
        problems.append("El archivo de clave de BigQuery no es igual al guardado en ULTRON: no se borró. Si es una clave nueva, importala en la tarjeta de BigQuery.")
        return
    try:
        key_path.unlink()
        deleted.append(label)
    except OSError:
        logger.warning("No se pudo borrar %s", label, exc_info=True)
        problems.append(f"No se pudo borrar {label}: puede estar abierto en otro programa. Cerralo y probá de nuevo.")


def _apply_now() -> None:
    """Lo importado vale YA, sin reiniciar: el `Settings` vivo queda como arrancaria Vision ahora (sin `.env`; ej. un ENABLE_* en false del `.env` deja de pesar y
    pasa a ser un interruptor apagado). Las conexiones ya salen de la base."""
    config.reload_settings_in_place()
