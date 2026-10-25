"""Configuracion GENERAL guardada cifrada en `vision.db` y aplicada sobre `Settings` (spec gestor-conexiones A4: REQ-002/006, categoria 9).

Por que existe: sin `.env` (decision del usuario 2026-10-08), lo que no es una conexion con nombre (proveedor de IA, voz, BigQuery, proxy, puerto, modelo, carpetas) tambien
tiene que guardarse y editarse desde Vision. Este modulo define QUE claves son configuracion general (`GENERAL_FIELDS`, la clasificacion que aprobo el usuario), las valida,
las guarda via `config_store` y las aplica sobre el `Settings` vivo SIN reiniciar.

Reglas:
- Solo se aceptan claves de `GENERAL_FIELDS`: los datos de Oracle/SharePoint/SAP/Confluence van en su conexion (`conn_store`), los `ENABLE_*` los reemplazan los
  interruptores (v0.26.0) y las rutas de logs quedan con su valor de siempre.
- Las variables de ENTORNO del sistema (no archivos: tests, `VISION_DATA_DIR`, casos avanzados) siguen pisando a la base. Las que pone el propio Vision para el proxy
  (`config._apply_proxy`) no cuentan como del sistema.
- Se aplica EN EL LUGAR sobre la instancia cacheada de `Settings` (mismo criterio que `credentials.apply_credentials_to_settings`): todo modulo que ya la tiene ve el cambio."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app import config_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeneralField:
    """Un campo de la configuracion general. `section` agrupa en la pantalla; `kind` = text | int | json (json: la key de BigQuery)."""

    key: str
    label: str
    section: str
    secret: bool = False
    kind: str = "text"


# Clasificacion aprobada por el usuario el 2026-10-08 (categoria 9 de la spec). El orden es el de la pantalla.
GENERAL_FIELDS: tuple[GeneralField, ...] = (
    # spec vision-generico: el proveedor de IA principal y el dictado (los demas proveedores se activan con su clave en el `.env`,
    # spec varios-proveedores; las IA locales se detectan solas, spec ia-local).
    GeneralField("llm_provider", "Tipo de conexión (anthropic u openai_compatible)", "ia"),
    GeneralField("llm_base_url", "Dirección (solo openai_compatible)", "ia"),
    GeneralField("llm_api_key", "Clave del proveedor", "ia", secret=True),
    GeneralField("llm_models", "Modelos a mostrar (separados por coma; vacío = los del proveedor)", "ia"),
    GeneralField("stt_base_url", "Dictado: dirección", "ia"),
    GeneralField("stt_api_key", "Dictado: clave", "ia", secret=True),
    GeneralField("stt_model", "Dictado: modelo", "ia"),
    GeneralField("vision_company_name", "Nombre de la empresa", "ia"),
    GeneralField("m365_connector_url", "Outlook y Teams: dirección del conector MCP de Microsoft 365", "ia"),
    GeneralField("elevenlabs_api_key", "Clave de ElevenLabs", "voz", secret=True),
    GeneralField("elevenlabs_voice_id", "Voz de ElevenLabs", "voz"),
    GeneralField("fish_api_key", "Clave de Fish Audio", "voz", secret=True),
    GeneralField("fish_voice_id", "Voz de Fish Audio", "voz"),
    GeneralField("bigquery_conexion_name", "Nombre", "bigquery"),
    GeneralField("bq_project_id", "Proyecto", "bigquery"),
    GeneralField("bq_dataset_id", "Dataset", "bigquery"),
    GeneralField("bq_table_id", "Tabla", "bigquery"),
    GeneralField("bq_location", "Ubicacion", "bigquery"),
    GeneralField("bq_key_json", "Archivo de clave de la cuenta de servicio", "bigquery", secret=True, kind="json"),
    GeneralField("bq_cost_guardrail_bytes", "Tope de costo por consulta (bytes)", "bigquery", kind="int"),
    GeneralField("bq_dry_run_cache_ttl_seconds", "Segundos que se recuerda una estimacion de costo", "bigquery", kind="int"),
    GeneralField("graph_conexion_name", "Nombre", "graph"),
    GeneralField("graph_source", "SharePoint de la que toma las credenciales", "graph"),
    GeneralField("https_proxy", "Proxy HTTPS", "red"),
    GeneralField("http_proxy", "Proxy HTTP", "red"),
    GeneralField("no_proxy", "Sin proxy para", "red"),
    GeneralField("confluence_https_proxy", "Proxy solo para Confluence", "red"),
    GeneralField("confluence_timeout_seconds", "Segundos de espera de Confluence", "red", kind="int"),
    GeneralField("app_host", "Direccion del servidor", "general"),
    GeneralField("app_port", "Puerto", "general", kind="int"),
    GeneralField("access_token", "Token para usar ULTRON desde otro equipo de la red", "general", secret=True),
    GeneralField("llm_model", "Modelo", "general"),
    GeneralField("workspace_dir", "Carpeta de trabajo por defecto", "general"),
    GeneralField("skills_dir", "Carpeta de skills de ULTRON", "general"),
    GeneralField("global_skills_dir", "Carpeta de skills globales", "general"),
    GeneralField("export_row_threshold", "Filas a partir de las que se exporta a CSV", "general", kind="int"),
    GeneralField("export_preview_rows", "Filas que se muestran antes de exportar", "general", kind="int"),
)
_BY_KEY = {f.key: f for f in GENERAL_FIELDS}
# Claves que guardan algo que `Settings` no declara (Graph elige su SharePoint en la tarjeta): se guardan pero no se aplican como atributo.
_NOT_A_SETTING = frozenset({"graph_source"})


def field_for(key: str) -> GeneralField | None:
    return _BY_KEY.get(key)


def env_overrides(key: str) -> bool:
    """True si una variable de entorno del SISTEMA fija esta clave (y por lo tanto manda sobre la base). Las que puso Vision para el proxy no cuentan."""
    from app import config  # import diferido: config importa este modulo al construir Settings

    var = key.upper()
    return var in os.environ and var not in config.vision_set_env()


def _coerce(field: GeneralField, value: str) -> Any:
    if field.kind == "int":
        return int(value)
    return value


def validate(updates: dict[str, Any]) -> dict[str, str | None]:
    """Valida un cambio de la pantalla. Devuelve los valores como texto (None = borrar). `ConfigStoreError` con un mensaje para la persona si algo no va."""
    clean: dict[str, str | None] = {}
    for key, value in updates.items():
        field = _BY_KEY.get(str(key))
        if field is None:
            raise config_store.ConfigStoreError(f"«{key}» no es un dato de la configuración general.")
        if value is None:
            clean[field.key] = None
            continue
        text = str(value).strip() if field.kind != "json" else str(value)
        if field.kind == "int" and text:
            try:
                if int(text) < 0:
                    raise ValueError
            except ValueError as exc:
                raise config_store.ConfigStoreError(f"«{field.label}» tiene que ser un número entero positivo.") from exc
        if field.key == "app_port" and text and not 1 <= int(text) <= 65_535:
            raise config_store.ConfigStoreError("El puerto tiene que estar entre 1 y 65535.")
        clean[field.key] = text if text else None  # vacio = volver al valor por defecto
    return clean


def apply_to(settings: Any, general: dict[str, str]) -> None:
    """Aplica lo guardado sobre `settings` (en el lugar). Una clave que fija el entorno del sistema no se toca; un valor que no se puede convertir se ignora con aviso."""
    for key, value in general.items():
        field = _BY_KEY.get(key)
        if field is None or key in _NOT_A_SETTING or env_overrides(key) or not hasattr(settings, key):
            continue
        try:
            setattr(settings, key, _coerce(field, value))
        except (TypeError, ValueError):
            logger.warning("La configuracion guardada '%s' no tiene un valor valido; se deja el de siempre", key)


def reset_to_default(settings: Any, key: str) -> None:
    """Vuelve una clave a su valor por defecto (cuando el usuario la borra en la pantalla), salvo que la fije el entorno del sistema."""
    from app.config import Settings  # import diferido: config importa este modulo

    if key in _NOT_A_SETTING or env_overrides(key) or key not in Settings.model_fields:
        return
    setattr(settings, key, Settings.model_fields[key].default)


def save(updates: dict[str, Any]) -> dict[str, str]:
    """Guarda (cifrado) y aplica EN EL ACTO sobre el `Settings` vivo (REQ-006: sin reiniciar). Devuelve lo guardado."""
    from app import config  # import diferido: config importa este modulo

    clean = validate(updates)
    stored = config_store.set_general(clean)
    settings = config.get_settings()
    apply_to(settings, {k: v for k, v in stored.items() if k in clean})
    for key, value in clean.items():
        if value is None:
            reset_to_default(settings, key)
    if {"https_proxy", "http_proxy", "no_proxy"} & set(clean):
        config.reapply_proxy(settings)
    return stored


def describe() -> list[dict[str, Any]]:
    """Lo que ve la pantalla: cada campo con su seccion y etiqueta. Los NO secretos con su valor; los secretos solo dicen si estan cargados (`set`). Nunca un secreto."""
    stored = config_store.get_general()
    out: list[dict[str, Any]] = []
    for f in GENERAL_FIELDS:
        value = stored.get(f.key, "")
        entry: dict[str, Any] = {"key": f.key, "label": f.label, "section": f.section, "secret": f.secret, "kind": f.kind, "set": bool(value),
                                 "env_locked": env_overrides(f.key)}
        if not f.secret:
            entry["value"] = value
        out.append(entry)
    return out
