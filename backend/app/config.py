"""
Configuracion central de Vision, cargada desde variables de entorno (.env).

Sigue el mismo patron que DBManager/app/config.py (pydantic-settings,
BASE_DIR relativo a este archivo, ninguna credencial hardcodeada). Vision
tiene conectores PROPIOS e independientes (Oracle, BigQuery, SharePoint,
SAP) -- no llama a DBManager por HTTP -- por eso repite el mismo esquema
de variables en su propio .env en vez de reusar el servicio.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# En desarrollo, los recursos y los datos viven en el árbol normal del repo.
# En el EXE onefile, PyInstaller extrae los recursos de solo lectura en
# ``sys._MEIPASS``; los datos modificables deben persistir fuera de esa carpeta
# temporal y separados por usuario bajo %LOCALAPPDATA%\Vision-personal.
IS_FROZEN = bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))
RESOURCE_ROOT = (
    Path(sys._MEIPASS).resolve()
    if IS_FROZEN
    else Path(__file__).resolve().parent.parent.parent
)

# Bug real (unificacion de Vision, 2026-09-19): el lanzador liviano
# (launch_vision.py / Vision.exe / Iniciar-Vision.bat de `prod/`) arranca
# uvicorn como SUBPROCESO NO congelado (python.exe normal, no el propio
# .exe de PyInstaller) -- ese subproceso ve `IS_FROZEN=False` aunque haya
# sido lanzado por Vision.exe, y antes calculaba mal BASE_DIR (buscaba
# `.env`/`keys/` al lado del codigo extraido en %LOCALAPPDATA%\Vision-personal\app\
# backend\, nunca en %LOCALAPPDATA%\Vision-personal\ donde el usuario realmente
# pone sus credenciales) -- por eso Oracle/BigQuery/SharePoint dejaban de
# conectar pese a que el `.env` real SI estaba completo.
#
# Fix: BASE_DIR = %LOCALAPPDATA%\Vision-personal SIEMPRE (sea o no el proceso
# frozen), salvo que se pida explicitamente el modo desarrollo con
# VISION_DEV_MODE=1 -- asi seteado por el propio comando de arranque de
# `dev` (uvicorn --reload contra backend/.env local), para no tener que
# gestionar credenciales de desarrollo en %LOCALAPPDATA% mientras se itera
# codigo. `prod/Iniciar-Vision.bat` y `Vision.exe` NUNCA setean esa
# variable, asi que ahi siempre usan %LOCALAPPDATA%\Vision-personal -- exactamente
# el mismo lugar fijo que ya usaba el viejo `Jarvis.exe` (build_portable_
# exe.bat, ejecucion 100% congelada en un solo proceso).
# Carpeta de datos propia (spec vision-generico): distinta de la de otras
# instalaciones de Vision en la misma PC, para no compartir `.env` ni sesiones.
DATA_FOLDER_NAME = "Vision-personal"

_DEV_MODE = os.environ.get("VISION_DEV_MODE") == "1"
BASE_DIR = (
    Path(__file__).resolve().parent.parent
    if _DEV_MODE and not IS_FROZEN
    else (
        Path(os.environ.get("VISION_DATA_DIR", "")).resolve()
        if os.environ.get("VISION_DATA_DIR")
        else Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / DATA_FOLDER_NAME
    )
)

# Toda ruta usada para escritura debe existir antes de que otros módulos la
# evalúen durante sus imports.
for _runtime_dir in ("keys", "data", "logs", "workspace", "browser"):
    (BASE_DIR / _runtime_dir).mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------
    # SERVIDOR WEB
    # -------------------------------------------------------------------
    app_host: str = "127.0.0.1"
    app_port: int = 8030
    cors_origins: str = "*"
    # spec jarvis-mobile-pwa: token compartido simple para proteger el API
    # cuando el backend escucha en 0.0.0.0 (accesible desde otros
    # dispositivos de la LAN, ej. celulares). Vacio = auth deshabilitada
    # (comportamiento identico al de antes, para no romper el uso actual
    # en 127.0.0.1). Ver app/auth.py.
    access_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # -------------------------------------------------------------------
    # PROVEEDOR DE IA (spec vision-generico REQ-001/002)
    # -------------------------------------------------------------------
    # "anthropic": Claude con clave de Anthropic, via su capa compatible con
    # OpenAI (el loop habla el formato de OpenAI). "openai_compatible":
    # OpenAI, Azure OpenAI o el gateway propio de una empresa (LLM_BASE_URL
    # obligatorio: nunca se asume un servidor por defecto).
    llm_provider: str = "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""
    # Modelo por defecto (el selector de la UI puede elegir otro).
    llm_model: str = "claude-sonnet-5"
    # Lista para el selector si el proveedor no publica GET /models,
    # separada por comas.
    llm_models: str = ""

    @property
    def llm_model_list(self) -> list[str]:
        return [m.strip() for m in self.llm_models.split(",") if m.strip()]

    # -------------------------------------------------------------------
    # DICTADO POR VOZ (spec vision-generico REQ-003): endpoint de audio
    # compatible con OpenAI (/audio/transcriptions). Sin configurar, el
    # microfono explica que el dictado no esta disponible.
    # -------------------------------------------------------------------
    stt_base_url: str = ""
    stt_api_key: str = ""
    stt_model: str = "whisper-1"

    # -------------------------------------------------------------------
    # VOZ DE VISION (spec voz-fish-audio REQ-005/006/009): claves de los
    # motores de voz en la nube. Que motor habla, en que orden y con que
    # voz se elige en Configuracion > Audio (ver app/voice/settings.py);
    # los ID de voz de aca son solo el valor inicial de esa pantalla.
    # -------------------------------------------------------------------
    fish_api_key: str = ""
    fish_voice_id: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # -------------------------------------------------------------------
    # MARCA (spec vision-generico REQ-005): nombre de la empresa que usa
    # Vision, en la pantalla de inicio y en el prompt. Vacio = sin nombre.
    # -------------------------------------------------------------------
    vision_company_name: str = ""
    # spec portar-desde-origen: direccion del conector MCP de Microsoft 365 (Outlook y Teams). Vacia = sin configurar: las herramientas
    # `m365_*` avisan que falta en vez de llamar a un servidor que no existe. Ademas el interruptor viene apagado de fabrica.
    m365_connector_url: str = ""

    # -------------------------------------------------------------------
    # WORKSPACE (raiz por defecto para tools bash/read/write/edit/glob/grep)
    # -------------------------------------------------------------------
    workspace_dir: str = "."

    @property
    def workspace_path(self) -> Path:
        p = Path(self.workspace_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p.resolve()

    # Raiz de las carpetas de vista previa en vivo por sesion
    # (browser/{session_id}/). Cada sesion tiene su propia subcarpeta donde
    # Vision puede escribir un mini-proyecto (HTML/CSS/JS) que el usuario
    # previsualiza en el panel "navegador" -- ver app/browser_workspace.py.
    # Contenido TEMPORAL: se borra al eliminar la sesion o liberar cache.
    browser_dir: str = "./browser"

    @property
    def browser_root_path(self) -> Path:
        p = Path(self.browser_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p.resolve()

    # Carpeta de skills LOCALES del proyecto (archivos SKILL.md) que la tool
    # `skill` puede cargar bajo demanda. Vive en la RAIZ del repo
    # (vision-web/skills/, hermana de backend/ y frontend/), no dentro de
    # backend/ -- asi cuando alguien clona/descarga el repo completo desde
    # GitHub ya tiene esta carpeta lista, sin necesitar sincronizar nada
    # por red (ver tambien skills_repo_path en project_context.py, que es
    # un mecanismo ADICIONAL/opcional para apuntar a otro repo de skills).
    skills_dir: str = "../skills"

    @property
    def skills_path(self) -> Path:
        # Las skills incluidas en el EXE son recursos de solo lectura. En modo
        # desarrollo se conserva la ruta configurable relativa a backend/.
        if IS_FROZEN:
            return (RESOURCE_ROOT / "skills").resolve()
        p = Path(self.skills_dir)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p.resolve()

    # Carpeta GLOBAL de skills del usuario (todas las skills que ya usa en
    # OpenCode/Claude Code, ej. *, desing-ultra-premium, etc.). Vision
    # las lee directo desde ahi -- NO se copian al proyecto -- para que el
    # agente tenga acceso a la MISMA coleccion completa de skills que el
    # usuario, siempre actualizada, sin duplicar contenido que se
    # desincronice del original.
    global_skills_dir: str = str(Path.home() / ".claude" / "skills")

    @property
    def global_skills_path(self) -> Path:
        return Path(self.global_skills_dir).resolve()

    # Archivo de auditoria de tools peligrosas (bash/write/edit)
    audit_log_path: str = "./logs/audit.log"

    @property
    def audit_log_file(self) -> Path:
        p = Path(self.audit_log_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    # Log de diagnostico de rendimiento por turno (investigacion 2026-09:
    # "Vision tarda mucho" -- ver app/perf_log.py). Mismo patron que
    # audit_log_path de arriba, archivo separado porque es de otro dominio
    # (tiempos, no auditoria de seguridad).
    perf_log_path: str = "./logs/perf.log"

    @property
    def perf_log_file(self) -> Path:
        p = Path(self.perf_log_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    # Log general de la app (tracebacks completos) -- mismo patron que los
    # dos de arriba. Antes de esto, `logging.basicConfig` solo mandaba a
    # consola: un traceback real (ej. el error crudo del proveedor de IA detras de un
    # mensaje "amigable" en el chat, ver app/llm/loop.py:_friendly_model_error)
    # solo existia en el scrollback de la ventana de Iniciar-Vision.bat y se
    # perdia para siempre si esa ventana se cerraba o recargaba.
    app_log_path: str = "./logs/app.log"

    @property
    def app_log_file(self) -> Path:
        p = Path(self.app_log_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    # -------------------------------------------------------------------
    # ORACLE (conector propio, mismo esquema que DBManager)
    # -------------------------------------------------------------------
    oracle_user: str = ""
    oracle_password: str = ""
    oracle_host: str = ""
    oracle_port: int = 1521
    oracle_service_name: str = ""
    oracle_schema: str = ""

    @property
    def oracle_dsn(self) -> str:
        return f"{self.oracle_host}:{self.oracle_port}/{self.oracle_service_name}"

    # -------------------------------------------------------------------
    # GOOGLE BIGQUERY
    # -------------------------------------------------------------------
    bq_project_id: str = ""
    bq_dataset_id: str = ""
    bq_table_id: str = ""
    google_application_credentials: str = "./keys/bigquery_key.json"
    bq_location: str = "US"
    # spec gestor-conexiones REQ-008: el CONTENIDO de la key de la cuenta de servicio, importado desde la pantalla y guardado cifrado en vision.db. Si esta, BigQuery lo
    # usa directo y el archivo de `google_application_credentials` deja de hacer falta.
    bq_key_json: str = ""

    @property
    def bq_credentials_path(self) -> Path:
        p = Path(self.google_application_credentials)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    def bq_full_table_ref(
        self, dataset_id: str | None = None, table_id: str | None = None
    ) -> str:
        dataset = dataset_id or self.bq_dataset_id
        table = table_id or self.bq_table_id
        return f"{self.bq_project_id}.{dataset}.{table}"

    # -------------------------------------------------------------------
    # SHAREPOINT / AZURE AD (Microsoft Graph)
    # -------------------------------------------------------------------
    sharepoint_site_url: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    sharepoint_default_list_name: str = ""

    # -------------------------------------------------------------------
    # PROXY DE LA EMPRESA (spec vision-generico REQ-004, portado de Vision-personal
    # junto con la voz): vale para TODO el proceso (proveedor de IA, dictado, voz en
    # la nube y conectores), ver _apply_proxy. `https_proxy` ya existia: SharePoint/
    # Graph lo sigue usando via `requests_proxies`.
    # -------------------------------------------------------------------
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""

    @property
    def azure_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @property
    def requests_proxies(self) -> dict[str, str] | None:
        if not self.https_proxy:
            return None
        return {"http": self.https_proxy, "https": self.https_proxy}

    # -------------------------------------------------------------------
    # SAP (Fiori/OData v2, servicios custom Z*)
    # -------------------------------------------------------------------
    sap_base_url: str = ""
    sap_client: str = "100"
    sap_user: str = ""
    sap_password: str = ""
    sap_language: str = "EN"

    # -------------------------------------------------------------------
    # VOZ DE VISION (spec voz-fish-audio REQ-005/006/009): claves de los
    # motores de voz en la nube. Que motor habla, en que orden y con que
    # voz se elige en Configuracion > Audio (ver app/voice/settings.py);
    # los ID de voz de aca son solo el valor inicial de esa pantalla.
    # -------------------------------------------------------------------
    fish_api_key: str = ""
    fish_voice_id: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    def sap_odata_url(self, service_name: str) -> str:
        return f"{self.sap_base_url.rstrip('/')}/sap/opu/odata/sap/{service_name}/"

    @property
    def sap_logon_url(self) -> str:
        return f"{self.sap_base_url.rstrip('/')}/sap/bc/ui2/flp"

    # -------------------------------------------------------------------
    # CONFLUENCE (spec conexiones-con-nombre, Fase B): el correo y el token son PERSONALES de cada usuario y viven en SU `.env`
    # (decision del usuario: cada persona usa el suyo; nunca van en el archivo compartido `connections.json`). La URL, el espacio por
    # defecto y las secciones SI van en el bloque de la conexion. Los defaults de URL/espacio/timeout solo se usan si el bloque no los trae.
    # -------------------------------------------------------------------
    confluence_email: str = ""
    confluence_api_token: str = ""
    confluence_base_url: str = "https://empresa.atlassian.net/wiki"
    confluence_space_key: str = ""
    confluence_root_page_id: str = ""
    confluence_root_page_name: str = ""
    confluence_timeout_seconds: int = 30
    confluence_https_proxy: str = ""

    # -------------------------------------------------------------------
    # NOMBRE de cada conexion armada desde este `.env` (lo que se muestra despues del guion: «Oracle - Datamart»). Pedido del usuario 2026-10-06.
    # Vacio = el nombre por defecto de cada tipo. Solo rige para la conexion que sale del `.env`: un bloque de connections.json lleva su propio nombre.
    # -------------------------------------------------------------------
    oracle_conexion_name: str = ""
    sharepoint_conexion_name: str = ""
    sap_conexion_name: str = ""
    confluence_conexion_name: str = ""
    graph_conexion_name: str = ""
    bigquery_conexion_name: str = ""

    # -------------------------------------------------------------------
    # FEATURE FLAGS
    # -------------------------------------------------------------------
    enable_oracle: bool = True
    enable_bigquery: bool = True
    enable_sharepoint: bool = True
    enable_sap: bool = True
    enable_confluence: bool = True

    # -------------------------------------------------------------------
    # spec controles-seguridad-optimizacion-lightspeed-bigquery
    # REQ-002: dry run + control de costos de BigQuery. Umbral en bytes
    # (1 GiB = 1_073_741_824) configurable sin tocar codigo -- pedido
    # explicito del usuario, para poder endurecer/relajar el limite segun
    # el proyecto GCP sin necesitar un redeploy. TTL del cache de dry run
    # en segundos: evita repetir el round-trip de red a BigQuery si la
    # MISMA consulta (hash exacto) ya se evaluo hace poco -- el cache
    # NUNCA relaja el veredicto (ver dbquery.py:_bigquery_dry_run_check),
    # solo ahorra la llamada de red en consultas identicas repetidas.
    # -------------------------------------------------------------------
    bq_cost_guardrail_bytes: int = 1_073_741_824
    bq_dry_run_cache_ttl_seconds: int = 300

    # -------------------------------------------------------------------
    # REQ-004: export automatico a CSV cuando un resultado de CUALQUIER
    # conector (Oracle/BigQuery/SharePoint/SAP) supera este umbral de
    # filas -- aplica ademas del top N que ya se le muestra al modelo
    # (MAX_ROWS_TO_MODEL en dbquery.py, tambien configurable aca para no
    # tener 2 constantes desincronizadas).
    # -------------------------------------------------------------------
    export_row_threshold: int = 50
    export_preview_rows: int = 50


# Lo que va siempre directo, sin proxy: el propio Vision (el lanzador y el
# navegador le hablan por loopback).
_LOCAL_HOSTS = "localhost,127.0.0.1,::1"


# Variables de entorno que puso el PROPIO Vision (proxy), con el valor que puso. Las demas son del sistema y mandan sobre la configuracion guardada (spec
# gestor-conexiones, regla 6). Una variable cuenta como de Vision SOLO si todavia tiene ese valor: si alguien la cambio despues, ya no es de Vision y no se toca.
_VISION_SET_ENV: dict[str, str] = {}


def _vision_owns(var: str) -> bool:
    return var in _VISION_SET_ENV and os.environ.get(var) == _VISION_SET_ENV[var]


def vision_set_env() -> frozenset[str]:
    return frozenset(var for var in _VISION_SET_ENV if _vision_owns(var))


def _apply_proxy(settings: Settings) -> None:
    """REQ-004: el proxy del .env vale para todo el proceso -- httpx (proveedor
    de IA y dictado) y requests (voz en la nube, conectores) lo toman de estas
    variables. Lo que ya venga definido en el entorno de Windows no se pisa.
    Sin proxy configurado no toca nada (comportamiento de siempre).
    spec gestor-conexiones: lo que puso Vision SI se puede actualizar (el usuario cambia el proxy desde la pantalla, sin reiniciar)."""
    for var, value in (("HTTP_PROXY", settings.http_proxy), ("HTTPS_PROXY", settings.https_proxy)):
        if value.strip() and (not os.environ.get(var) or _vision_owns(var)):
            os.environ[var] = _VISION_SET_ENV[var] = value.strip()
    if (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")) and (not os.environ.get("NO_PROXY") or _vision_owns("NO_PROXY")):
        os.environ["NO_PROXY"] = _VISION_SET_ENV["NO_PROXY"] = ",".join(filter(None, [settings.no_proxy.strip(), _LOCAL_HOSTS]))


def reapply_proxy(settings: Settings) -> None:
    """El usuario cambio el proxy en la pantalla: se quita lo que habia puesto Vision (nunca lo del sistema) y se vuelve a aplicar."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        if _vision_owns(var):
            os.environ.pop(var, None)
        _VISION_SET_ENV.pop(var, None)
    _apply_proxy(settings)


def _build_settings() -> Settings:
    """spec gestor-conexiones REQ-002/003: con la base cifrada ACTIVA el `.env` ya no se lee (aunque exista) y la configuracion general sale de la base. Si no, lo de
    siempre. Las variables de entorno del sistema se leen en los dos casos."""
    from app import config_store, general_settings  # import diferido: los dos importan este modulo

    if not config_store.is_active():
        return Settings()
    settings = Settings(_env_file=None)
    general_settings.apply_to(settings, config_store.get_general())
    return settings


@lru_cache
def get_settings() -> Settings:
    settings = _build_settings()
    _apply_proxy(settings)
    return settings


def reload_settings_in_place() -> Settings:
    """spec gestor-conexiones A6: despues de importar, el `Settings` vivo tiene que quedar IGUAL a como arrancaria Vision ahora (sin `.env`, con lo de la base),
    sin reiniciar. Se copia campo por campo sobre la MISMA instancia: todo modulo que ya la tiene ve el cambio."""
    live = get_settings()
    fresh = _build_settings()
    for key in Settings.model_fields:
        setattr(live, key, getattr(fresh, key))
    reapply_proxy(live)
    return live
