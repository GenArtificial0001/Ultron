"""
Vision - backend FastAPI del asistente superinteligente.

Levantar con:
    python -m uvicorn app.main:app --reload --reload-dir app --host 127.0.0.1 --port 8030
or:
    python -m app.main

spec jarvis-mobile-pwa: para que celulares en la misma LAN (WiFi) puedan
conectarse (ej. notebook como servidor, celular como cliente), levantar con
APP_HOST=0.0.0.0 en el .env (o --host 0.0.0.0) en vez de 127.0.0.1, y
configurar ACCESS_TOKEN en el .env para no dejar el API abierto a cualquier
dispositivo de la red (ver app/auth.py).

Expone:
    GET  /api/status                 -> healthcheck simple
    GET  /api/models                 -> lista modelos LLM disponibles en
                                         el proveedor de IA (roster en vivo, cacheado
                                         en memoria unos minutos) para el
                                         selector de modelo del frontend
    GET  /api/models/{model_id}      -> valid_args/reasoning_levels reales
                                         de un modelo puntual (para armar
                                         dinamicamente el panel "Modelo e
                                         inteligencia" de Configuracion)
    POST /api/chat                   -> envia un mensaje del usuario y
                                         devuelve un stream SSE con la
                                         respuesta del agente (texto +
                                         tool-calls + tool-results +
                                         eventos de todo/question). El
                                         turno corre en background
                                         INDEPENDIENTE de esta conexion --
                                         cambiar de sesion o cerrar la
                                         pestaña no lo cancela.
    GET  /api/chat/stream/{id}       -> re-engancha el stream SSE a una
                                         sesion que sigue procesando en
                                         background (util al volver a una
                                         sesion que se dejo "pensando")
    GET  /api/session/{id}/status    -> si una sesion tiene un turno
                                         corriendo en background ahora mismo
    GET  /api/context/{session_id}   -> estimacion de tokens/contexto usado
                                         por una sesion (medidor sidebar)
    GET  /api/connections            -> conexiones con nombre (tipo, nombre, estado de configuracion; NUNCA valores)
    POST /api/connections/{tipo}/test?name=... -> prueba UNA conexion por nombre
    /api/vault/*                     -> gestor de conexiones (spec gestor-conexiones): todo cifrado en vision.db, SOLO desde esta PC y la propia pagina:
                                         GET status | POST import | DELETE legacy-files | GET/POST connections | PUT/DELETE connections/{id} | POST connections/{id}/test |
                                         GET connections/{id}/secret/{campo} (desbloqueado) | GET/PUT general | GET general/secret/{clave} (desbloqueado) |
                                         POST bigquery-key | GET/POST/DELETE unlock (Windows Hello)
    GET  /api/confluence/tree|children|page   -> arbol (carga perezosa) y texto de paginas de Confluence (solo lectura)
    GET  /api/connectors/status      -> estado online/error de cada
                                         conector de datos (panel lateral)
    GET  /api/config/project         -> configuracion de proyecto activa
    POST /api/config/project/local   -> fija la carpeta local de proyecto
    DELETE /api/config/project/local -> quita la carpeta local de proyecto
    POST /api/config/project/sharepoint    -> fija carpeta SharePoint de proyecto
    DELETE /api/config/project/sharepoint  -> quita carpeta SharePoint de proyecto
    GET  /api/config/generation      -> parametros de generacion guardados
    POST /api/config/generation      -> guarda parametros de generacion
    GET  /api/config/model-fallback  -> lista de prioridad del salto automatico
                                         de modelos y el estado de cada uno
    POST /api/config/model-fallback  -> guarda lista, pausa e interruptor
    POST /api/model-fallback/check   -> con que modelo seguiria el chat AHORA
                                         (el frontend pregunta al vencer una pausa)
    GET  /api/fs/roots               -> unidades locales (C:\\, D:\\...)
    GET  /api/fs/children            -> subcarpetas de una ruta local
    GET  /api/sharepoint/drives      -> bibliotecas del sitio SharePoint
    GET  /api/sharepoint/children    -> contenido de una carpeta SharePoint
    POST /api/voice/speak            -> audio de UNA frase con el primer motor
                                         de voz de la cadena que responda (spec
                                         voz-fish-audio; ver app/voice/)
    GET/POST /api/voice/config       -> cadena de voces, voces, consentimiento
                                         y efecto (Configuracion > Audio)
    GET  /api/voice/voices/{engine}  -> voces de la cuenta de un motor
    POST /api/voice/local/install    -> instala la clonacion local (con
                                         simulacion previa); GET .../status
    POST/DELETE /api/voice/local/sample -> muestra de voz para clonar
    GET  /api/skills                 -> lista de skills descubiertas (para
                                         el menu "/" del chat)
    POST /api/voice/speak            -> audio de UNA frase con el primer motor
                                         de voz de la cadena que responda (spec
                                         voz-fish-audio; ver app/voice/)
    GET/POST /api/voice/config       -> cadena de voces, voces, consentimiento
                                         y efecto (Configuracion > Audio)
    GET  /api/voice/voices/{engine}  -> voces de la cuenta de un motor
    POST /api/voice/local/install    -> instala la clonacion local (con
                                         simulacion previa); GET .../status
    POST/DELETE /api/voice/local/sample -> muestra de voz para clonar
    GET  /api/sessions/{id}/exports/{filename} -> descarga un CSV exportado
                                         por REQ-004 (spec controles-
                                         seguridad-optimizacion-lightspeed-
                                         bigquery) cuando un resultado de
                                         consulta supera el umbral de filas
    GET  /                           -> sirve el frontend vanilla (index.html)
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import platform
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# BUG real corregido (deteccion QA 2026-09-14): en esta maquina, el loop de
# asyncio con el que corre uvicorn en Windows NO admite subprocesos
# (`asyncio.create_subprocess_exec` tira `NotImplementedError` con mensaje
# vacio) -- rompia la tool `bash` (backend/app/tools/bash.py) Y el cliente
# LSP (backend/app/lsp.py) por igual, ambos con el mismo stack trace. Windows
# ya usa `WindowsProactorEventLoopPolicy` por defecto desde Python 3.8, pero
# hay que fijarla EXPLICITAMENTE antes de que uvicorn cree su loop (aca, al
# importar este modulo, que siempre pasa antes) porque en este entorno algo
# la estaba dejando en el Selector loop (que sí soporta sockets pero NUNCA
# subprocesos en Windows).
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictStr

from app import browser_workspace, credentials, lsp
from app import commands as commands_module
from app import project_context as pc
from app.auth import AccessTokenMiddleware, OpaqueOriginMiddleware
from app import data_migration
from app.config import BASE_DIR, RESOURCE_ROOT, get_settings
from app.connectors.sharepoint_connector import (
    SharePointConnectionError,
    SharePointConnector,
)
from app import connections, switches
from app.connectors.confluence_connector import ConfluenceConnector
from app.connectors_status import (
    CONNECTOR_NAMES,
    check_all_connectors,
    test_bigquery_draft,
    test_draft_connection,
    test_one_connection,
)
from app.friendly_errors import (
    describe_connector_error,
    describe_internal_error,
    describe_model_error,
    describe_os_error,
    describe_transcription_error,
    describe_voice_engine_error,
    technical_summary,
)
from app.llm import model_fallback
from app.llm import providers as llm_providers
from app.llm.client import call_with_token_retry, get_stt_client
from app.llm.client import get_client  # noqa: F401 -- punto de reemplazo de los tests (e2e_support, roster)
from app.llm.loop import run_turn
from app.meeting_gate import MeetingGateState, check_invocation
from app import attachment_service
from app.persistence import purge as persistence_purge
from app.persistence import service as persistence_service
from app.scheduler import agents as agents_scheduler
from app.scheduler import alarms as alarms_scheduler
from app.session.compaction import compact_session
from app.session.events import get_session_store
from app.token_estimate import estimate_tokens
from app.tools.skill import discover_skills
from app.tech_report import normalize_level
from app.vault_guard import VaultGuardMiddleware
from app.version import __version__
from app.voice import local_clone
from app.voice import service as voice_service
from app.voice.engines import VoiceEngineError
from app.voice.settings import VoiceConfigError
from app.voice.settings import load_config as voice_load_config
from app.voice.settings import update_config as voice_update_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Ademas de la consola (unico destino hasta ahora): persiste el mismo log en
# backend/logs/app.log. Sin esto, un traceback real (ej. el error crudo de
# el proveedor de IA detras de un mensaje "amigable" en el chat, ver
# app/llm/loop.py:_model_error_event y app/friendly_errors.py) SOLO existia en el scrollback de la
# ventana de consola de Iniciar-Vision.bat -- si esa ventana se cierra o hace
# --reload, se perdia para siempre y no habia forma de diagnosticar el error
# despues del hecho.
_file_handler = logging.handlers.RotatingFileHandler(
    get_settings().app_log_file,
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_file_handler)
logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title="ULTRON API", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# spec jarvis-mobile-pwa: token compartido simple, solo activo si
# ACCESS_TOKEN esta configurado en .env (ver app/auth.py). Necesario porque
# el plan de "Jarvis Mobile" implica levantar el backend con APP_HOST=
# 0.0.0.0 (alcanzable desde celulares en la misma LAN) en vez del
# 127.0.0.1 de siempre.
app.add_middleware(AccessTokenMiddleware)
# REQ-006 (navegador-integrado-ventana): 403 a /api/* con Origin: null (iframe aislado).
app.add_middleware(OpaqueOriginMiddleware)
# spec gestor-conexiones REQ-007b: /api/vault/* solo desde esta PC y la propia pagina de Vision, ANTES de validar el cuerpo o resolver la ruta (app/vault_guard.py).
app.add_middleware(VaultGuardMiddleware)


def _install_unlock_verifier() -> None:
    """spec gestor-conexiones B3: el desbloqueo de las contrasenas guardadas se confirma con Windows Hello (app/windows_hello.py). Los tests lo reemplazan
    (tests/conftest.py) para que ninguna prueba abra la ventana de Windows."""
    from app import secret_access, windows_hello  # import diferido: solo se usan aca

    if windows_hello.supported():
        secret_access.set_verifier(windows_hello.verify)


_install_unlock_verifier()


@app.on_event("startup")
async def _start_session_purge_task() -> None:
    """persistencia-sqlite-sesiones REQ-007: arranca la tarea de fondo de
    auto-purga (sesiones sin actividad hace >30 dias se exportan a
    Markdown y luego se borran). Se lanza una sola vez al levantar el
    proceso; sigue corriendo mientras el backend este vivo."""
    asyncio.create_task(persistence_purge.run_purge_loop())


@app.on_event("startup")
async def _start_agents_scheduler_task() -> None:
    """spec agentes-programados: arranca el reloj del backend para la
    tabla `agents` -- sigue corriendo aunque no haya ningun navegador
    abierto, con tal de que este proceso siga vivo."""
    asyncio.create_task(agents_scheduler.run_agents_loop())


@app.on_event("startup")
async def _start_alarms_scheduler_task() -> None:
    """2026-09: alarmas/recordatorios (`schedules`, `/api/alarms`)
    migradas al mismo reloj de BACKEND que los agentes (ver app/scheduler/
    alarms.py) -- antes solo disparaban desde `scheduleTick` en el
    frontend, y necesitaban la pestaña de Vision abierta en el navegador
    en el momento exacto del horario para sonar/mostrar el toast."""
    asyncio.create_task(alarms_scheduler.run_alarms_loop())


@app.on_event("startup")
def _start_notifier_thread() -> None:
    """2026-09-17: arranca el hilo de Tkinter que muestra la alerta visual
    persistente de agentes/alarmas (ver app/notifier.py) -- una sola vez,
    sincronico (no hace falta await, solo lanza el hilo daemon)."""
    from app import notifier

    notifier.start_notifier()


@app.on_event("startup")
def _apply_encrypted_credentials() -> None:
    """Aplica sobre `Settings` (en memoria) las credenciales cifradas
    guardadas en `backend/.env.enc` (ver app/credentials.py) -- se corre
    UNA vez al arrancar, ademas de cada vez que se guardan/borran
    credenciales desde la UI en caliente."""
    credentials.apply_credentials_to_settings()


@app.on_event("startup")
async def _start_lsp_client() -> None:
    """REQ-LSP: inicia un único pylsp global sin bloquear el backend.

    En el EXE onefile, el subproceso LSP debe extraer el mismo ejecutable una
    segunda vez. Esperarlo acá bloquearía todas las rutas HTTP durante ese
    trabajo; la tarea en segundo plano mantiene disponible la interfaz y el
    diagnóstico se habilita apenas termina de arrancar.
    """
    asyncio.create_task(lsp.start_lsp_client())


@app.on_event("startup")
def _start_proxy_watch() -> None:
    """spec proxy-autoheal REQ-001/005/011: al arrancar, verifica la salida a internet en un hilo aparte (NUNCA bloquea el arranque) y, si no hay ninguna, la sigue
    buscando unos minutos (la VPN puede conectar despues de abrir Vision). Si algo falla aca, Vision arranca igual."""
    from app import proxy_health  # import diferido: modulo chico, solo lo usa el arranque y las rutas /api/proxy

    try:
        proxy_health.start_watch()
    except Exception:  # noqa: BLE001 -- el chequeo de la salida a internet nunca puede impedir que Vision arranque
        logger.warning("No se pudo iniciar el chequeo de la salida a internet", exc_info=True)


@app.on_event("shutdown")
async def _stop_lsp_client() -> None:
    await lsp.stop_lsp_client()


def _assert_session_owner(session_id: str) -> None:
    """Autorizacion a nivel de objeto (BOLA/IDOR): corta con 403 si esta
    sesion tiene un dueño registrado en el indice maestro DISTINTO del
    usuario de Windows actual, para no dejar que el que conoce un
    session_id ajeno acceda a su historial en una carpeta de proyecto
    compartida (ej. OneDrive con varios usuarios). Fail-open intencional:
    sesiones sin dueño registrado (en memoria / nunca persistidas, ver
    docstring de service.py) pasan sin bloquear -- no rompe el flujo en
    vivo antes de que se asigne carpeta."""
    from app.persistence import master_index
    from app.persistence import service as persistence

    owner = master_index.get_owner(session_id)
    if owner is not None and owner != persistence.current_windows_user():
        raise HTTPException(
            status_code=403,
            detail="No autorizado para acceder a esta sesión.",
        )


class AttachmentRef(BaseModel):
    """REQ-003 (spec adjuntar-archivos-chat): un archivo adjunto al mensaje."""

    path: str
    name: str = ""
    size: int | None = None
    mime: str = ""
    inplace: bool = False


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # REQ-003/005/007/008/009: id generado por el FRONTEND (uuid) que
    # identifica este turno de usuario de forma estable, para poder
    # despues pedir stop/editar/eliminar/reordenar sin ambiguedad. Si no
    # se manda (compatibilidad con clientes viejos), se genera uno del
    # lado del servidor pero no queda persistido en ningun lado que el
    # frontend pueda referenciar luego.
    message_id: str | None = None
    # Modelo elegido en el selector del frontend para ESTE turno. Si no se
    # manda, se usa settings.llm_model (el default de .env).
    model: str | None = None
    # Override puntual de parametros de generacion para ESTE turno (si no
    # se manda, run_turn usa los guardados en project_config.json via
    # Configuracion). Null explicito en cualquier campo = no usar ese
    # parametro (el modelo usa su default).
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    # spec contexto-multimodal REQ-006: data URIs base64 YA comprimidas
    # por el frontend (REQ-003) -- lista vacia/None = turno sin imagenes,
    # comportamiento identico al de antes de esta spec.
    images: list[str] | None = None
    # REQ-003 (spec adjuntar-archivos-chat): archivos ya subidos a `attachment/`
    # (o registrados para leer en su lugar). Opcional: un cliente que solo manda
    # `images` funciona igual que antes. El backend valida cada ruta.
    attachments: list[AttachmentRef] | None = None
    # REQ-006/009 (spec asistente-contextual-voz, Modo Cerebro): True si
    # este turno se origino en una intervencion del modo Cerebro (voz
    # continua) -- activa la confirmacion hablada antes de ejecutar tools
    # peligrosas (ver run_turn en app/llm/loop.py). False (default) = chat
    # de texto normal, ejecuta sin confirmar (comportamiento sin cambios).
    voice_confirm: bool = False
    # spec informe-tecnico-hablado: el frontend avisa si el informe tecnico
    # hablado esta prendido (ver UserTurn.narrate).
    narrate: bool = False
    # REQ-012 (informe-tecnico-hablado): 'summary' | 'detailed'; cualquier otro valor cuenta como 'summary'.
    narrate_level: str = "summary"


class TranscribeRequest(BaseModel):
    """REQ-002 (spec narracion-agente-efimera, "Interaccion por voz"):
    audio capturado en el navegador (MediaRecorder, WebM/Opus tipico) como
    data URI base64 -- mismo criterio que `ChatRequest.images` de arriba
    (data URIs en el body JSON, no multipart/form-data), consistente con
    el resto de la API."""

    audio: str  # data URI completo, ej. "data:audio/webm;base64,GkXf..."
    # Bug real reportado por el usuario: hablando en español, Whisper a
    # veces transcribia en INGLES. Causa: sin este parametro, Whisper hace
    # auto-deteccion de idioma por cada clip -- con audio corto (fragmentos
    # de ~8s del modo reunion) o ruido de fondo, el modelo puede "adivinar
    # mal" el idioma (comportamiento conocido de Whisper en clips cortos).
    # None (default) = auto-detect, tal como pidio el usuario -- un codigo
    # ISO-639-1 ("es"/"en"/"pt"/etc) FUERZA ese idioma y mejora tanto
    # precision como latencia (Whisper se salta el paso de deteccion).
    language: str | None = None


@app.post("/api/transcribe")
def transcribe_audio(req: TranscribeRequest) -> dict:
    """REQ-002 (modo chat por voz) + REQ-004 (modo reunion, fragmentos
    cortos en tiempo real) de la spec narracion-agente-efimera: transcribe
    audio con el proveedor de dictado del .env (spec vision-generico REQ-003:
    STT_BASE_URL/STT_API_KEY/STT_MODEL, un endpoint /audio/transcriptions
    compatible con OpenAI, ej. Whisper). Sin configurar, el usuario ve que el
    dictado no esta disponible (ver describe_transcription_error)."""
    import base64
    import io

    try:
        # Data URI: "data:audio/webm;base64,<payload>" -- se descarta el
        # prefijo antes del primer "," (mismo parseo que ya hace el
        # frontend con imagenes, ver contexto-multimodal).
        _header, _, payload = req.audio.partition(",")
        audio_bytes = base64.b64decode(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio invalido en /api/transcribe (base64 no decodificable): %s", exc)
        raise HTTPException(
            400,
            detail="No pude leer la grabación (llegó dañada o incompleta). Probá grabar de nuevo.",
        ) from None

    stt = {"model": "?"}

    def _stt_client(force_refresh: bool = False):
        # El dictado tiene su propio proveedor (STT_*): se lee la configuracion vigente en cada intento.
        client, stt["model"] = get_stt_client()
        return client

    def _transcribe(client):
        # REQ-002 (transcripcion-reintenta-token): el archivo se ARMA en cada intento -- el SDK lo consume al leerlo,
        # asi que reusar el del primer intento mandaria el audio vacio en el reintento.
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.webm"  # la API de audio infiere el formato del nombre de archivo
        # `language` solo se fuerza si el usuario eligio uno explicito en
        # Configuracion > Audio -- el SDK espera "str | Omit" (no admite
        # None), asi que se arman 2 ramas en vez de un **kwargs generico
        # (el type-checker no puede resolver los overloads de `create`
        # contra un dict desestructurado). Omitir el parametro por
        # completo es lo que le dice a Whisper que haga auto-deteccion
        # (comportamiento default pedido).
        if req.language:
            return client.audio.transcriptions.create(
                model=stt["model"],
                file=audio_file,
                response_format="text",
                language=req.language,
            )
        return client.audio.transcriptions.create(
            model=stt["model"],
            file=audio_file,
            response_format="text",
        )

    try:
        # REQ-001: un 401 (token invalidado) pide un token nuevo y reintenta una vez; el resto de errores no se reintenta.
        response = call_with_token_retry(_transcribe, _stt_client)
    except Exception as exc:
        # El texto crudo del proveedor va solo al log; el usuario ve que paso
        # y que hacer (ver app/friendly_errors.py).
        friendly = describe_transcription_error(exc)
        logger.exception("Error transcribiendo audio con %s [%s]", stt["model"], friendly.code)
        raise HTTPException(502, detail=friendly.message) from None

    # response_format="text" del SDK de OpenAI devuelve el string directo
    # (no un objeto con .text) -- pero por las dudas de que el proveedor lo
    # envuelva distinto, se soportan ambas formas.
    text = response if isinstance(response, str) else getattr(response, "text", str(response))
    return {"text": text.strip()}


# ---------------------------------------------------------------------------
# Voz de Vision (spec voz-fish-audio): motores intercambiables (Fish Audio,
# ElevenLabs, clonacion local) en una cadena de prioridad, con la voz del
# navegador como ultimo eslabon. La logica vive en app/voice/.
# ---------------------------------------------------------------------------
class VoiceSpeakRequest(BaseModel):
    text: str
    # Probar un eslabon puntual desde Configuracion > Audio, sin salto.
    engine: str | None = None
    model: str | None = None


@app.post("/api/voice/speak")
def voice_speak(req: VoiceSpeakRequest) -> dict:
    """REQ-001/003/004: audio (base64) de UNA frase con el primer motor de la
    cadena que responda. `audio: null` = ningun motor pudo (o la cadena esta
    vacia): el frontend habla con la voz del navegador y, si viene `notice`,
    lo muestra una sola vez."""
    import base64

    text = req.text.strip()
    if not text:
        raise HTTPException(400, detail="No hay texto para leer en voz alta.")
    if len(text) > voice_service.MAX_TEXT_CHARS:
        raise HTTPException(400, detail="El texto es demasiado largo para una sola frase.")
    only = None
    if req.engine:
        engine = voice_service.ENGINES.get(req.engine)
        if engine is None:
            raise HTTPException(400, detail="Ese motor de voz no existe.")
        # REQ-002: ni siquiera la prueba sale a la nube sin consentimiento.
        if engine.cloud and not voice_load_config().consent.get(req.engine):
            raise HTTPException(400, detail=f"Primero confirmá que el texto que ULTRON lee se envía a {engine.label}.")
        only = {"engine": req.engine, "model": req.model or engine.models[0]}
    try:
        result = voice_service.speak(text, only=only)
    except voice_service.NoVoiceEngineAvailable as exc:
        notice = exc.notice
        return {
            "audio": None,
            "notice": {"code": notice.code, "message": notice.message} if notice else None,
        }
    return {
        "audio": base64.b64encode(result.audio.data).decode("ascii"),
        "mime": result.audio.mime,
        "engine": result.engine,
        "model": result.model,
        "notice": {"code": result.notice.code, "message": result.notice.message} if result.notice else None,
    }


@app.get("/api/voice/config")
def get_voice_config() -> dict:
    return voice_service.status_payload()


class VoiceConfigRequest(BaseModel):
    chain: list[dict[str, str]] | None = None
    fish_voice_id: str | None = None
    elevenlabs_voice_id: str | None = None
    consent: dict[str, bool] | None = None
    effect: bool | None = None
    pause_minutes: int | None = None


@app.post("/api/voice/config")
def set_voice_config(req: VoiceConfigRequest) -> dict:
    """REQ-001/002/003/008: guarda la cadena, las voces, el consentimiento y el
    efecto. Guardar limpia las pausas: si el usuario arreglo algo (cargo
    credito, cambio la voz), la proxima frase ya lo vuelve a probar."""
    try:
        cfg = voice_update_config(req.model_dump(), voice_service.models_by_engine())
    except VoiceConfigError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    voice_service.PAUSES.clear()
    return voice_service.status_payload(cfg)


@app.get("/api/voice/voices/{engine}")
def list_voice_engine_voices(engine: str) -> dict:
    """REQ-006: las voces de la cuenta para elegir en Configuracion > Audio."""
    voice_engine = voice_service.ENGINES.get(engine)
    if voice_engine is None:
        raise HTTPException(404, detail="Ese motor de voz no existe.")
    try:
        return {"voices": voice_engine.list_voices()}
    except VoiceEngineError as exc:
        friendly = describe_voice_engine_error(exc.kind, voice_engine.label, status=exc.status)
        logger.warning("No se pudieron listar las voces de %s [%s]: %s", engine, friendly.code, exc.detail[:300])
        raise HTTPException(502, detail=friendly.message) from None


@app.post("/api/voice/local/install")
def install_local_voice() -> dict:
    """REQ-007: instala la clonacion local en segundo plano, con simulacion
    previa; el progreso se consulta en /api/voice/local/status."""
    return local_clone.start_install()


@app.get("/api/voice/local/status")
def local_voice_status() -> dict:
    return {**local_clone.install_status(), "sample": local_clone.sample_info()}


class VoiceSampleRequest(BaseModel):
    audio: str  # data URI, mismo criterio que TranscribeRequest
    filename: str


@app.post("/api/voice/local/sample")
def upload_voice_sample(req: VoiceSampleRequest) -> dict:
    """REQ-007: guarda la muestra de voz (WAV o MP3, 10 s como minimo) en la
    carpeta de datos, nunca en el repo."""
    import base64
    import binascii

    try:
        data = base64.b64decode(req.audio.partition(",")[2], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, detail="No pude leer el archivo (llegó dañado). Probá subirlo de nuevo.") from None
    try:
        return local_clone.save_sample(data, req.filename)
    except local_clone.SampleError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@app.delete("/api/voice/local/sample")
def delete_voice_sample() -> dict:
    local_clone.delete_sample()
    return local_clone.sample_info()


# ---------------------------------------------------------------------------------------------------------------------
# spec control-microfono-windows: ver, activar y ajustar el volumen del microfono de Windows (`app/audio_control.py`).
# El navegador abre un microfono SILENCIADO por el sistema sin ningun error (solo trae silencio): el estado vive en Windows y solo el backend, que corre en esta
# misma PC, puede leerlo y cambiarlo. Estas rutas CAMBIAN hardware del usuario, asi que TODAS pasan por `_audio_guard` (REQ-008 + seguridad): solo esta PC y la propia
# pagina de Vision, sin interruptor que lo apague. Nada de aca se llama solo: leer es inocuo y escribir lo dispara una accion explicita de la persona.
# ---------------------------------------------------------------------------------------------------------------------
def _audio_guard(request: Request) -> None:
    from app import secret_access  # import diferido: modulo chico, mismo guard que el gestor de conexiones

    problem = secret_access.local_page_problem(
        request.client.host if request.client else None,
        request.url.hostname,
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        f"{request.url.scheme}://{request.url.netloc}",
    )
    if problem:
        raise HTTPException(
            status_code=403,
            detail="El audio de esta PC solo se puede cambiar desde la página de ULTRON en esta misma PC.",
        )


def _audio_http_error(exc: Exception) -> HTTPException:
    from app import audio_control  # import diferido: ver _audio_guard

    if isinstance(exc, audio_control.AudioUnsupported):
        return HTTPException(status_code=501, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


class AudioVolumeRequest(BaseModel):
    volume: int  # 0..100; 0 silencia y conserva el nivel anterior (REQ-004)


class AudioMuteRequest(BaseModel):
    muted: bool  # False = activar (REQ-002)


@app.get("/api/audio/mics")
def list_audio_mics(request: Request) -> dict:
    """REQ-001: microfonos CONECTADOS de Windows, el predeterminado primero, con si estan silenciados y su volumen (0-100; null = el driver no lo dice)."""
    _audio_guard(request)
    from app import audio_control  # import diferido: ver _audio_guard

    try:
        return {"mics": [m.as_dict() for m in audio_control.list_microphones()]}
    except audio_control.AudioControlError as exc:
        raise _audio_http_error(exc) from exc


@app.post("/api/audio/mics/mute")
def set_audio_mic_mute(request: Request, req: AudioMuteRequest, mic_id: str) -> dict:
    """REQ-002: activa (`muted=false`) o silencia un microfono SIN tocar su volumen ni cual es el predeterminado. Devuelve el estado releido de Windows.
    El id viaja por query (`?mic_id=`): los ids de Windows llevan llaves y puntos que no son comodos en una ruta."""
    _audio_guard(request)
    from app import audio_control  # import diferido: ver _audio_guard

    try:
        return {"mic": audio_control.set_mute(mic_id, req.muted).as_dict()}
    except audio_control.AudioControlError as exc:
        raise _audio_http_error(exc) from exc


@app.post("/api/audio/mics/volume")
def set_audio_mic_volume(request: Request, req: AudioVolumeRequest, mic_id: str) -> dict:
    """REQ-003/004: volumen de 0 a 100. De 1 a 100 fija el nivel y quita el silencio; 0 silencia y conserva el nivel. Devuelve el estado releido."""
    _audio_guard(request)
    from app import audio_control  # import diferido: ver _audio_guard

    try:
        return {"mic": audio_control.set_volume(mic_id, req.volume).as_dict()}
    except audio_control.AudioControlError as exc:
        raise _audio_http_error(exc) from exc


# ---------------------------------------------------------------------------------------------------------------------
# spec proxy-autoheal (REQ-007, REQ-008): Configuracion > Red > Proxy. Cambiar el proxy cambia POR DONDE SALE todo Vision, asi que TODAS las rutas pasan por `_proxy_guard`
# (solo esta PC y la propia pagina de Vision, igual que las contraseñas guardadas y Outlook/Teams). Ninguna respuesta lleva una contraseña (las direcciones salen sin usuario).
# ---------------------------------------------------------------------------------------------------------------------
_PROXY_INVALID = "Esa dirección de proxy no es válida. Escribila como «equipo:puerto», por ejemplo 19.12.80.237:83."
_PROXY_FAILED = "No se pudo revisar la salida a internet. Probá de nuevo; si sigue igual, avisá a soporte."


def _proxy_guard(request: Request) -> None:
    from app import secret_access  # import diferido: modulo chico, mismo guard que las contraseñas y Outlook/Teams

    problem = secret_access.local_page_problem(
        request.client.host if request.client else None,
        request.url.hostname,
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        f"{request.url.scheme}://{request.url.netloc}",
    )
    if problem:
        raise HTTPException(
            status_code=403,
            detail="La salida a internet solo se puede cambiar desde la página de ULTRON en esta misma PC.",
        )


class ProxyAddressRequest(BaseModel):
    address: str = Field("", max_length=300)  # vacio = la conexion directa (sin proxy)
    # Booleano ESTRICTO: un «no» o un 0 mal escrito no puede convertirse en «forzar» una direccion que no responde.
    force: StrictBool = False


@app.get("/api/proxy/status")
def proxy_status(request: Request, response: Response) -> dict:
    """REQ-008: lo ultimo que se chequeo (en uso, guardado, candidatos con su estado, aviso). NO sale a la red."""
    _proxy_guard(request)
    from app import proxy_health  # import diferido: ver _proxy_guard

    response.headers["Cache-Control"] = "no-store"
    return proxy_health.snapshot()


@app.post("/api/proxy/reconnect")
def proxy_reconnect(request: Request, response: Response) -> dict:
    """REQ-007/008, boton «Reconectar»: corre el chequeo completo con cura, igual que al arrancar. Tarda hasta unos segundos si algo no responde."""
    _proxy_guard(request)
    from app import proxy_health  # import diferido: ver _proxy_guard

    response.headers["Cache-Control"] = "no-store"
    try:
        proxy_health.check()
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log, la persona ve una frase
        logger.warning("Reconectar la salida a internet fallo: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=_PROXY_FAILED) from exc
    return proxy_health.snapshot()


@app.post("/api/proxy/test")
def proxy_test(request: Request, req: ProxyAddressRequest, response: Response) -> dict:
    """REQ-007/008, boton «Probar»: prueba UNA direccion y no cambia nada."""
    _proxy_guard(request)
    from app import proxy_health  # import diferido: ver _proxy_guard

    response.headers["Cache-Control"] = "no-store"
    try:
        return proxy_health.probe_one(req.address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_PROXY_INVALID) from exc


@app.post("/api/proxy/apply")
def proxy_apply(request: Request, req: ProxyAddressRequest, response: Response) -> dict:
    """REQ-007/008, boton «Guardar y reconectar»: guarda (cifrado) y aplica YA la direccion elegida. Una que no responde no se aplica salvo `force`."""
    _proxy_guard(request)
    from app import proxy_health  # import diferido: ver _proxy_guard

    response.headers["Cache-Control"] = "no-store"
    try:
        outcome = proxy_health.apply_choice(req.address, force=req.force)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_PROXY_INVALID) from exc
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log, la persona ve una frase
        logger.warning("Guardar la salida a internet fallo: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=_PROXY_FAILED) from exc
    return {**proxy_health.snapshot(), **outcome}


# ---------------------------------------------------------------------------------------------------------------------
# spec office-com-automatizacion (REQ-009, REQ-015): estado de Excel y Word y el interruptor «Office» de Configuracion > Conexiones. Apagar Office cambia lo que el modelo puede hacer
# sobre los archivos de la persona, asi que las dos rutas pasan por `_office_guard` (solo esta PC y la propia pagina de Vision, igual que Outlook y Teams). Consultar NO lanza ni abre nada.
# ---------------------------------------------------------------------------------------------------------------------
_OFFICE_FAILED = "No se pudo revisar el estado de Office. Probá de nuevo; si sigue igual, avisá a soporte."


def _office_guard(request: Request) -> None:
    from app import secret_access  # import diferido: modulo chico, mismo guard que Outlook y Teams

    problem = secret_access.local_page_problem(
        request.client.host if request.client else None,
        request.url.hostname,
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        f"{request.url.scheme}://{request.url.netloc}",
    )
    if problem:
        raise HTTPException(
            status_code=403, detail="Office solo se puede ver y cambiar desde la página de ULTRON en esta misma PC."
        )


@app.get("/api/office/status")
def office_status_route(request: Request, response: Response) -> dict:
    """REQ-015: `{enabled, excel: {state, message}, word: {state, message}}`. No abre ni lanza Excel ni Word; con Office apagado ni siquiera consulta COM."""
    _office_guard(request)
    from app.office import status as office_status  # import diferido: ver _office_guard

    from app import capability_names  # import diferido: ver _office_guard

    response.headers["Cache-Control"] = "no-store"
    try:
        # spec conexiones-office-m365-visibles REQ-008: el nombre que la PERSONA le puso («Principal» si no puso ninguno)
        return {
            **office_status.snapshot(),
            "name": capability_names.get("office"),
            "display": capability_names.display("office"),
        }
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log, la persona ve una frase
        logger.warning("Consultar el estado de Office fallo: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=_OFFICE_FAILED) from exc


class CapabilityNameRequest(BaseModel):
    # `str` ESTRICTO: un numero, una lista o `null` no se convierten en texto. La regla del nombre (la de las demas conexiones) la aplica `capability_names.set_name`.
    name: StrictStr


def _save_capability_name(capability: str, req: CapabilityNameRequest) -> dict:
    """Guarda el nombre de «Outlook y Teams» u «Office» (spec conexiones-office-m365-visibles REQ-008). Nombre invalido = 422 con la frase para la persona."""
    from app import capability_names  # import diferido: modulo chico, solo lo usan estas rutas

    try:
        display = capability_names.set_name(capability, req.name)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Ese nombre no sirve. {capability_names.problem(req.name)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log, la persona ve una frase
        logger.warning("Guardar el nombre de %s fallo: %s", capability, exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail="No se pudo guardar el nombre. Probá de nuevo; si sigue igual, avisá a soporte."
        ) from exc
    return {"name": req.name, "display": display}


@app.post("/api/office/name")
def office_name(request: Request, req: CapabilityNameRequest) -> dict:
    """REQ-008: guarda el nombre que la persona le pone a la conexion «Office». Solo esta PC y la propia pagina."""
    _office_guard(request)
    return _save_capability_name("office", req)


class OfficeSwitchRequest(BaseModel):
    # Booleano ESTRICTO: sin esto pydantic convierte "no", "false" o 0 en False y un valor mal escrito apagaria Office sin avisar.
    enabled: StrictBool


@app.post("/api/office/switch")
def office_switch(request: Request, req: OfficeSwitchRequest) -> dict:
    """REQ-009: prende o apaga Office. Apagado, ninguna herramienta `office_*` sale hacia el modelo. No cierra ningun libro ni toca ningun archivo."""
    _office_guard(request)
    from app import switches  # import diferido: ver _office_guard

    switches.set_office_enabled(req.enabled)
    return {"enabled": switches.is_office_enabled()}


# ---------------------------------------------------------------------------------------------------------------------
# spec office-outlook-teams-mcp (REQ-M02, REQ-M09, REQ-M11, REQ-M12): iniciar y cerrar sesion en Outlook y Teams (`app/m365/`).
# Iniciar sesion abre el navegador y cerrarla borra el token: cambian algo de la persona, asi que TODAS las rutas pasan por `_m365_guard` (solo esta PC y la propia pagina de Vision,
# igual que el audio y las contraseñas guardadas). El retorno del navegador (`/callback`) llega desde el sitio de la empresa hacia 127.0.0.1, asi que solo exige que sea ESTA PC.
# Nada de aca se llama solo: iniciar sesion lo dispara una accion explicita de la persona.
# ---------------------------------------------------------------------------------------------------------------------
def _m365_guard(request: Request, *, from_empresa_redirect: bool = False) -> None:
    from app import secret_access  # import diferido: modulo chico, mismo guard que el audio y las contraseñas

    client_host = request.client.host if request.client else None
    if from_empresa_redirect:
        # El navegador vuelve del sitio de la empresa (otro origen): solo se exige que el pedido llegue desde ESTA PC y a la direccion local.
        problem = secret_access.local_page_problem(
            client_host, request.url.hostname, None, None, f"{request.url.scheme}://{request.url.netloc}"
        )
    else:
        problem = secret_access.local_page_problem(
            client_host,
            request.url.hostname,
            request.headers.get("origin"),
            request.headers.get("sec-fetch-site"),
            f"{request.url.scheme}://{request.url.netloc}",
        )
    if problem:
        raise HTTPException(
            status_code=403,
            detail="Outlook y Teams solo se pueden conectar desde la página de ULTRON en esta misma PC.",
        )


@app.get("/api/m365/status")
def m365_status(request: Request) -> dict:
    """REQ-M09: estado de Outlook y Teams para Configuracion (`connected`, `waiting`, `error`, `disconnected`) y si el interruptor esta `enabled`. Nunca lleva el token."""
    _m365_guard(request)
    from app import capability_names, switches  # import diferido: ver _m365_guard
    from app.m365 import service

    # spec conexiones-office-m365-visibles REQ-008: el nombre que la PERSONA le puso («Principal» si no puso ninguno)
    return {
        **service.status(),
        "enabled": switches.is_m365_enabled(),
        "name": capability_names.get("m365"),
        "display": capability_names.display("m365"),
    }


@app.post("/api/m365/name")
def m365_name(request: Request, req: CapabilityNameRequest) -> dict:
    """REQ-008: guarda el nombre que la persona le pone a la conexion «Outlook y Teams». Solo esta PC y la propia pagina."""
    _m365_guard(request)
    return _save_capability_name("m365", req)


class M365SwitchRequest(BaseModel):
    # Booleano ESTRICTO: sin esto pydantic convierte "no", "false" o 0 en False y un valor mal escrito apagaria Outlook y Teams sin avisar. Solo `true` o `false` reales; el resto da 422.
    enabled: StrictBool


@app.post("/api/m365/switch")
def m365_switch(request: Request, req: M365SwitchRequest) -> dict:
    """REQ-M09: prende o apaga Outlook y Teams. Apagado, ninguna herramienta `m365_*` sale hacia el modelo. NO cierra la sesion ni borra el token."""
    _m365_guard(request)
    from app import switches  # import diferido: ver _m365_guard

    switches.set_m365_enabled(req.enabled)
    return {"enabled": switches.is_m365_enabled()}


@app.post("/api/m365/login")
async def m365_login(request: Request) -> dict:
    """REQ-M02: empieza el inicio de sesion con la cuenta de la empresa (abre el navegador). Responde ya con `waiting`; la pantalla consulta `/status`. La direccion de retorno usa el puerto REAL."""
    _m365_guard(request)
    from app.m365 import service  # import diferido: ver _m365_guard

    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return await service.start_login(port)


@app.post("/api/m365/logout")
async def m365_logout(request: Request) -> dict:
    """REQ-M11: cierra la sesion (borra el token cifrado y corta un login en espera)."""
    _m365_guard(request)
    from app.m365 import service  # import diferido: ver _m365_guard

    return await service.sign_out()


_M365_CALLBACK_PAGE = (
    '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>ULTRON</title>'
    "<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#10131a;color:#e8eaf0;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}"
    "main{{max-width:30rem;padding:2rem;text-align:center}}h1{{font-size:1.25rem;margin:0 0 .75rem}}p{{margin:0;color:#aab0bf}}</style></head>"
    "<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"
)


def _m365_callback_page(title: str, text: str):
    from fastapi.responses import HTMLResponse  # import diferido: solo lo usa esta ruta

    # `title` y `text` son SIEMPRE frases fijas de arriba: lo que llega por la URL (codigo, estado, motivo) nunca se escribe en el HTML.
    return HTMLResponse(_M365_CALLBACK_PAGE.format(title=title, text=text))


@app.get("/api/m365/callback")
def m365_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """REQ-M02: a esta direccion vuelve el navegador tras iniciar sesion en la empresa. Entrega el codigo al login en curso (solo si el `state` es el suyo) y le muestra a la persona una pagina
    simple. La pagina NUNCA repite el codigo ni el motivo crudo de un rechazo: lo recibido no se escribe en el HTML."""
    _m365_guard(request, from_empresa_redirect=True)
    from app.m365 import service  # import diferido: ver _m365_guard

    if error:
        if service.deliver_error(state=state, reason=error):
            return _m365_callback_page(
                "No se pudo iniciar sesión",
                "No se autorizó el acceso. Podés cerrar esta pestaña y volver a tocar «Iniciar sesión» en ULTRON.",
            )
    elif code and service.deliver_callback(code=code, state=state):
        return _m365_callback_page(
            "Listo, ya podés volver a ULTRON", "Iniciaste sesión en Outlook y Teams. Podés cerrar esta pestaña."
        )
    return _m365_callback_page(
        "Este aviso no corresponde a ningún inicio de sesión",
        "No se cambió nada. Si querés conectarte, volvé a ULTRON y tocá «Iniciar sesión». Podés cerrar esta pestaña.",
    )


# REQ-004: estado del gate heuristico, UNO por sesion -- vive en memoria
# del proceso (igual que SessionState), se descarta si el proceso se
# reinicia (no hace falta persistirlo, es solo el timestamp del throttle
# anti-rafaga de una reunion en curso).
_MEETING_GATE_STATES: dict[str, MeetingGateState] = {}


class MeetingAnalyzeRequest(BaseModel):
    session_id: str
    fragment: str
    # REQ-004/REQ-005 (revisado 2026-09-07, pedido explicito del usuario:
    # "razona sobre TODO lo que entendio"): transcripcion acumulada de
    # TODA la reunion hasta este momento (no solo el ultimo fragmento de
    # ~8s) -- sin esto, si alguien preguntaba "Vision, que dijo Juan hace
    # un rato sobre el presupuesto?" el LLM solo veia el fragmento aislado
    # con la pregunta, sin ningun contexto de lo que Juan dijo antes.
    # Opcional (default "") para no romper compatibilidad si algun caller
    # viejo no lo manda.
    full_transcript: str = ""
    # Nuevo REQ (2026-09-07, pedido explicito del usuario): "click dentro
    # del circulo de Vision" durante el Modo reunion equivale a activar
    # con la palabra "Vision" pero manual -- bypasea tanto el gate de
    # mencion (`check_invocation`) COMO el throttle anti-rafaga (una
    # accion explicita del usuario tocando el ojo no deberia bloquearse
    # por "ya opino hace poco"). Default False para no cambiar el
    # comportamiento del camino automatico existente.
    force: bool = False


# REQ-004/005 revisado: se manda TODA la transcripcion acumulada como
# contexto, con el fragmento que disparo la invocacion resaltado al
# final -- asi el LLM "razona sobre todo lo que entendio" en la
# reunion, no solo sobre las ultimas palabras sueltas.
#
# Instruccion de brevedad (pedido explicito del usuario): las
# respuestas se sentian "muy largas" al ESCUCHARLAS en voz durante una
# reunion en vivo (un parrafo largo que se lee rapido en pantalla se
# siente eterno hablado) -- se le pide al LLM que sea breve por
# default, PERO sin un limite fijo de caracteres/oraciones: el usuario
# eligio explicitamente que "Vision decida segun la pregunta" (una
# pregunta simple amerita 1 frase, una que pide explicacion real puede
# extenderse un poco mas). Esto es una instruccion de PROMPT, no un
# truncado mecanico del texto -- truncar a la fuerza podria cortar una
# respuesta a la mitad de una idea.
_MEETING_BREVITY_INSTRUCTION = (
    "[Instrucción de estilo para esta respuesta, Modo reunión] Estás respondiendo "
    "en VOZ ALTA durante una reunión en vivo, no en un chat de texto -- una respuesta "
    "larga se siente pesada de escuchar hablada. Sé breve por defecto (1-2 frases para "
    "preguntas simples/directas). Extendete solo si la pregunta realmente lo amerita "
    "(pide una explicación, un análisis, o varios datos) -- vos decidís según la "
    "pregunta, sin un límite fijo."
)
# REQ-006 / categoria 6 de la spec (regla de negocio "Transcripcion
# confusa/ambigua al pedir opinion"): la transcripcion viene de
# whisper-large sobre audio en vivo de una reunion real -- puede tener
# ruido de fondo, voces superpuestas, o cortes que dejan una pregunta a
# medias. En vez de "opinar a ciegas" sobre algo mal entendido (lo cual
# puede ser peor que no responder), se le pide al LLM un protocolo
# explicito de 3 pasos SOLO para ese caso -- nunca para una pregunta
# que se entiende bien, donde debe responder directo como siempre.
_MEETING_CONFUSION_PROTOCOL_INSTRUCTION = (
    "[Regla de negocio, Modo reunión -- SOLO si aplica] La transcripción que sigue "
    "viene de reconocimiento de voz automático sobre audio real de una reunión: puede "
    "tener ruido de fondo, voces superpuestas, o cortes que dejan una idea a medias. "
    "Si al leerla notás que la pregunta o pedido dirigido a vos es confusa/ambigua "
    "-- no por falta de contexto de negocio, sino porque el propio texto transcrito "
    "no permite entender con confianza qué te están pidiendo -- NO opines a ciegas "
    "adivinando la intención. En ese caso específico: (1) decí explícitamente qué "
    "fue lo que entendiste de la conversación hasta ahora (sin inventar lo que no "
    "quedó claro), (2) preguntá si preferís dar directamente un resumen del tema "
    "tratado en vez de una opinión puntual sobre algo que no se entendió bien, y "
    "(3) si al armar ese resumen notás que la conversación se desvió mucho del tema "
    "original, señalalo explícitamente y recién ahí retomá una opinión sobre el tema "
    "real. Si la pregunta SÍ se entiende con confianza, ignorá esta regla por "
    "completo y respondé directo, igual que siempre."
)
# REQ-001 (spec asistente-contextual-voz): fix de un bug real reportado
# por el usuario -- preguntado algo en Modo reunión, el modelo a veces
# respondia "no puedo acceder al chat" pese a que ARQUITECTONICAMENTE
# SI tiene el historial completo disponible (este mismo `user_message`
# se agrega a `session.messages`, el MISMO objeto que ya contiene todo
# el historial de chat de texto previo de esta sesion -- ver run_turn
# en app/llm/loop.py). La causa real no era falta de contexto, sino que
# nada en el prompt le decia EXPLICITAMENTE al modelo que ese historial
# de chat de texto anterior esta disponible en los mensajes de arriba
# -- sin esa aclaracion, el modelo podia interpretar la pregunta como
# referida solo a la transcripcion de audio (lo unico "presentado"
# explicitamente como contexto) y, al no encontrar la respuesta ahi,
# negar acceso en vez de mirar los turnos anteriores de la conversacion.
_MEETING_CHAT_CONTEXT_INSTRUCTION = (
    "[Aclaración de contexto, Modo reunión] Tenés acceso COMPLETO al historial de "
    "chat de texto de esta misma sesión: todos los mensajes anteriores (tuyos y del "
    "usuario) que ves más arriba en esta conversación son reales y forman parte de tu "
    "contexto disponible, igual que en el chat de texto normal. Si te preguntan algo "
    "sobre lo que se habló antes en el chat (no solo en la transcripción de audio de "
    "esta reunión), respondé usando ESE historial -- nunca digas que no podés acceder "
    "al chat o que no tenés esa información disponible."
)
# REQ-003 (spec asistente-contextual-voz): encuadre de rol -- Record es
# para ESCUCHAR y OPINAR sobre una reunion, no para operar activamente
# sobre archivos/rutas de un proyecto (ese caso de uso vive en el Modo
# Cerebro). Se lo aclara al modelo para que, si le piden algo tipo
# "revisame el archivo X" durante una reunion, redirija al usuario en
# vez de intentar ejecutar tools de archivos sin el contexto adecuado
# de una sesion de trabajo 1:1.
_MEETING_ROLE_SCOPE_INSTRUCTION = (
    "[Alcance de tu rol, Modo reunión] Estás en el Modo reunión: tu función acá es "
    "escuchar la conversación (potencialmente de varias personas) y opinar cuando te "
    "lo piden, usando tus herramientas de consulta (Oracle/BigQuery/SharePoint/skills) "
    "si hace falta fundamento. Si te piden específicamente operar sobre un archivo o "
    'ruta de un proyecto (ej. "revisá el archivo X", "corregí el bug en Y"), '
    "aclará brevemente que ese tipo de trabajo puntual sobre archivos se hace mejor "
    "en el chat de texto o en el Modo Cerebro (pensado para eso), en vez de intentarlo "
    "acá sin el contexto adecuado de una sesión de trabajo dedicada."
)


def _build_meeting_message(fragment: str, full_transcript: str) -> str:
    """Arma el mensaje que Record le manda al modelo en el Modo reunion.

    Las cuatro instrucciones (brevedad, transcripcion confusa, acceso al chat y alcance del rol) van
    SIEMPRE y antes de la transcripcion. Con transcripcion acumulada se manda toda, con la ultima frase
    resaltada al final; sin ella (primer fragmento de una reunion) se manda solo el fragmento."""
    transcript_context = full_transcript.strip()
    if transcript_context:
        return (
            f"{_MEETING_BREVITY_INSTRUCTION}\n\n"
            f"{_MEETING_CONFUSION_PROTOCOL_INSTRUCTION}\n\n"
            f"{_MEETING_CHAT_CONTEXT_INSTRUCTION}\n\n"
            f"{_MEETING_ROLE_SCOPE_INSTRUCTION}\n\n"
            "[Transcripción completa de audio en vivo, Modo reunión, hasta este momento]\n"
            f"{transcript_context}\n\n"
            "[Última frase transcrita -- acaban de mencionarte y pedirte algo, "
            "respondé basándote en TODO el contexto de arriba]\n"
            f"{fragment.strip()}"
        )
    return (
        f"{_MEETING_BREVITY_INSTRUCTION}\n\n"
        f"{_MEETING_CONFUSION_PROTOCOL_INSTRUCTION}\n\n"
        f"{_MEETING_CHAT_CONTEXT_INSTRUCTION}\n\n"
        f"{_MEETING_ROLE_SCOPE_INSTRUCTION}\n\n"
        f"[Transcripción de audio en vivo, Modo reunión] {fragment.strip()}"
    )


@app.post("/api/meeting/analyze")
async def meeting_analyze(req: MeetingAnalyzeRequest) -> dict:
    """REQ-004/REQ-005 (spec narracion-agente-efimera, Modo reunion):
    recibe un fragmento de transcripcion en vivo, lo pasa por el gate
    heuristico local (`check_invocation`, SIN LLM) -- si no matchea o esta
    en throttle, responde `{"opinion": null}` de inmediato (barato, <1ms).
    Si matchea, invoca al LLM REAL (mismo motor/tools que el chat de texto,
    via `run_turn`) con TODA la transcripcion acumulada como contexto (no
    solo el fragmento puntual que disparo la invocacion -- ver
    `full_transcript` arriba) y devuelve la respuesta completa (no
    streameada -- el modo reunion la necesita entera para narrarla por voz
    de una vez, REQ-007) en `opinion`.

    La respuesta del LLM se agrega al historial real de la sesion
    (`session.messages`, igual que cualquier mensaje de chat de texto) --
    coherente con REQ-006, que ya establece que el resumen final del modo
    reunion se persiste como un mensaje mas de la sesion activa.

    REQ-003 (spec asistente-contextual-voz): Record NO cambia de tools --
    sigue teniendo el mismo `run_turn()` con Oracle/BigQuery/SharePoint/
    skills disponibles para poder opinar con fundamento cuando lo
    mencionan. Lo que se redefine es el ENCUADRE: Record es para escuchar
    una reunion (potencialmente de varias personas) y opinar bajo pedido,
    NO para operar activamente sobre archivos/rutas especificas de un
    proyecto (ese caso de uso especifico -- "revisame el archivo X",
    "corregi el bug en Y" -- se deriva al Modo Cerebro, pensado para
    trabajo 1:1 manos-libres del usuario sobre su propia carpeta de
    proyecto). Ver `chat_context_instruction` mas abajo para el fix del
    REQ-001 (acceso al historial de chat)."""
    store = get_session_store()
    session = store.get_or_create(req.session_id)

    gate_state = _MEETING_GATE_STATES.setdefault(req.session_id, MeetingGateState())
    if req.force:
        # Click manual en el ojo: bypasea gate Y throttle, pero SI
        # actualiza el timestamp del throttle (mismo criterio que una
        # invocacion aprobada normal) para que una mencion hablada
        # inmediatamente despues no dispare una 2da respuesta en rafaga.
        gate_state.last_intervention_at = time.monotonic()
    elif not check_invocation(req.fragment, gate_state):
        return {"opinion": None}

    if not req.full_transcript.strip() and not req.fragment.strip():
        # Click manual sin haber grabado nada todavia -- respuesta barata
        # e inmediata, sin invocar al LLM (mismo criterio que el resumen
        # vacio de /api/meeting/summarize, que tambien evita el LLM si no
        # hay nada que procesar).
        return {"opinion": "Todavía no escuché nada para opinar."}

    if session.is_busy:
        # Ya hay un turno de CHAT DE TEXTO corriendo en esta misma sesion
        # -- no se pisa (run_turn no es reentrante sobre el mismo
        # session.messages). Se descarta esta invocacion puntual en vez de
        # encolarla; REQ-004 ya especifica que Jarvis puede pedir que se
        # repita si no llego a procesar algo, mismo criterio se aplica aca.
        return {"opinion": None}

    # REQ-004/005 revisado: TODA la transcripcion acumulada va como contexto, con el fragmento que
    # disparo la invocacion resaltado al final (ver `_build_meeting_message` y sus instrucciones).
    user_message = _build_meeting_message(req.fragment, req.full_transcript)
    opinion_text = ""
    llm_error: str | None = None
    try:
        async for event in run_turn(session, user_message, req.session_id):
            if event.get("type") == "text-delta":
                opinion_text += event.get("data", {}).get("text", "")
            elif event.get("type") == "error":
                # run_turn no lanza excepciones para errores del proveedor de IA
                # (ej. 429 rate-limit, 401) -- las emite como evento
                # "error" y sigue el generador -- hay que capturarlo
                # explicito, si no el endpoint devuelve opinion=None de
                # forma silenciosa e indistinguible de "el gate no
                # aprobo" (bug real encontrado en pruebas contra el proveedor de IA
                # real: un 429 por limite de gasto se tragaba sin dejar
                # rastro).
                llm_error = event.get("data", {}).get("message", "Error desconocido del LLM")
    except Exception:
        logger.exception("Error invocando al LLM desde el Modo reunion")
        raise HTTPException(
            502,
            detail=(
                "No pude preparar mi respuesta en la reunión por un problema interno de "
                "ULTRON. La reunión sigue escuchando; si se repite, cerrá y volvé a abrir ULTRON."
            ),
        ) from None

    if llm_error:
        logger.warning(
            "Modo reunion: el LLM devolvio error para sesion %s: %s",
            req.session_id,
            llm_error,
        )
        # `llm_error` ya viene en lenguaje simple (evento "error" del loop).
        raise HTTPException(502, detail=f"No pude responderte en la reunión. {llm_error}")

    return {"opinion": opinion_text.strip() or None}


class MeetingSummarizeRequest(BaseModel):
    session_id: str
    # REQ-006: transcripcion acumulada completa de la reunion (concatenada
    # en el frontend a medida que llegan fragmentos de whisper-large) --
    # se usa SOLO para generar el resumen, nunca se persiste tal cual
    # (categoria 7 de la spec: "nunca se guarda transcripcion cruda
    # palabra-por-palabra", solo el resumen resultante).
    transcript: str


@app.post("/api/meeting/summarize")
async def meeting_summarize(req: MeetingSummarizeRequest) -> dict:
    """REQ-006 (spec narracion-agente-efimera, Modo reunion): al salir del
    modo reunion (confirmado por el usuario, ver REQ-003), genera un
    resumen ESTRUCTURADO (puntos clave/decisiones/pendientes) de toda la
    transcripcion acumulada y lo persiste como un mensaje mas de la sesion
    activa -- reusa `run_turn` (mismo mecanismo que persiste automatico a
    SQLite via `persistence.record_message_start/record_part_start`, ver
    app/llm/loop.py) en vez de escribir a mano en la base, sin tablas
    nuevas.

    Orden del contenido (adoptado de OpenOats, decision ya registrada en
    spec.md): el resumen va PRIMERO, antes de cualquier detalle extendido
    -- se lo pide explicito en el prompt."""
    if not req.transcript.strip():
        # Reunion sin nada audible transcrito -- no hay nada que resumir,
        # no tiene sentido invocar al LLM para esto.
        return {"summary": None}

    store = get_session_store()
    session = store.get_or_create(req.session_id)

    if session.is_busy:
        raise HTTPException(
            409,
            detail="ULTRON está terminando otra respuesta en esta conversación. Esperá a que termine y probá de nuevo.",
        )

    summarize_prompt = (
        "Se terminó una reunión (Modo reunión de ULTRON). A continuación está la "
        "transcripción completa de audio capturada en vivo (puede tener errores de "
        "transcripción, ruido, o partes cortadas). Generá un RESUMEN ESTRUCTURADO en "
        "formato Markdown con estas 3 secciones, en este orden exacto: "
        "'## Puntos clave' (bullets con lo más importante que se habló), "
        "'## Decisiones' (bullets con decisiones tomadas, si hubo alguna -- si no hubo "
        "ninguna decisión clara, escribí 'Ninguna decisión explícita registrada'), "
        "'## Pendientes' (bullets con tareas o acciones a seguir, si las hubo -- si no "
        "hubo ninguna, escribí 'Ninguno'). No repitas la transcripción cruda, solo el "
        "resumen.\n\n"
        f"Transcripción:\n{req.transcript.strip()}"
    )

    summary_text = ""
    llm_error: str | None = None
    try:
        async for event in run_turn(session, summarize_prompt, req.session_id):
            if event.get("type") == "text-delta":
                summary_text += event.get("data", {}).get("text", "")
            elif event.get("type") == "error":
                llm_error = event.get("data", {}).get("message", "Error desconocido del LLM")
    except Exception:
        logger.exception("Error generando el resumen del Modo reunion")
        raise HTTPException(
            502,
            detail=(
                "No pude armar el resumen de la reunión por un problema interno de ULTRON. "
                "Probá de nuevo; si se repite, cerrá y volvé a abrir ULTRON."
            ),
        ) from None

    if llm_error:
        logger.warning(
            "Modo reunion: el LLM devolvio error generando el resumen para sesion %s: %s",
            req.session_id,
            llm_error,
        )
        raise HTTPException(502, detail=f"No pude armar el resumen de la reunión. {llm_error}")

    # Limpieza del estado del gate: la reunion termino, no tiene sentido
    # mantener el throttle de una sesion que ya no esta escuchando.
    _MEETING_GATE_STATES.pop(req.session_id, None)

    return {"summary": summary_text.strip() or None}


@app.get("/api/status")
def status() -> dict:
    # `version` es la version de codigo que este proceso tiene REALMENTE
    # cargada en memoria (ver app/version.py) -- distinto de "que .exe
    # compilaste ultimo": si corres el backend en modo dev (`uvicorn
    # --reload`) contra el codigo fuente, esto refleja el codigo fuente en
    # disco; si corres un .exe portable ya compilado, refleja lo que
    # quedo CONGELADO adentro al compilarlo (ver CHANGELOG.md, incidente
    # 2026-09-05, para el bug real que motivo agregar este campo).
    return {
        "status": "ok",
        "model": default_model_id(),  # spec varios-proveedores: "proveedor/modelo"
        "version": __version__,
        # spec vision-generico REQ-005: nombre de la empresa en la pantalla.
        "company_name": settings.vision_company_name,
        # REQ-009 (spec portar-desde-origen): que se copio de Vision-Personal, UNA sola vez (despues ya no viene).
        "data_migration": data_migration.take_notice(BASE_DIR),
        # spec salto-automatico-modelos: con el salto activo, el selector de
        # modelos ofrece "Automatico (segun tu lista de prioridad)".
        "model_fallback_enabled": model_fallback.load_settings().enabled,
    }


# REQ-107/108 ELIMINADOS (spec configuracion-limpieza-credenciales-servidor,
# 2026-09-20): endpoint GET /api/network/local-ips + app/network_info.py
# eliminados -- exponer el servidor a la LAN (0.0.0.0) para que la deteccion
# de IPs tuviera algun uso practico exigia ademas una regla de firewall de
# Windows con privilegios de administrador; el usuario decidio
# explicitamente NO automatizar la apertura de un puerto de entrada en una
# notebook corporativa por riesgo real de politica de Cyber
# Security. Ver CHANGELOG.md para el detalle completo de la reversion.


# Cache en memoria de la lista de modelos -- evita consultar al proveedor en
# cada apertura del selector del frontend.
_MODELS_CACHE: dict = {"data": None, "fetched_at": 0.0}
_MODELS_CACHE_TTL = 300  # 5 min

# Parametros de generacion que acepta cualquier proveedor compatible con OpenAI:
# arman el panel "Modelo e inteligencia" de Configuracion.
_GENERIC_VALID_ARGS = ["temperature", "top_p", "max_tokens"]
# Bug real (2026-09-27, un "hola" rechazado por los tres modelos): Anthropic
# responde 400 si recibe parametros de muestreo en sus modelos nuevos
# ("`top_p` is deprecated for this model" en claude-sonnet-5; Sonnet 5, Opus
# 4.7 en adelante y Fable no aceptan temperature/top_p/top_k), y en los que
# todavia los aceptan (Haiku 4.5, Opus/Sonnet 4.6 y anteriores) no admite
# `temperature` y `top_p` juntos. Con Anthropic, esos modelos reciben solo
# `temperature`; cualquier otro, ninguno (un modelo que salga despues se trata
# como nuevo: mandar de menos nunca rompe el pedido, mandar de mas si).
_ANTHROPIC_SAMPLING_MODEL_RE = re.compile(r"^claude-(?:3-|(?:opus|sonnet|haiku)-4(?:-[0-6])?(?:-\d{8})?$)")


def _valid_args_for(model_id: str) -> list[str]:
    """Parametros de generacion que acepta `model_id`. spec varios-proveedores:
    la regla de Anthropic vale para los modelos de Claude, sea el proveedor
    principal o uno mas; el resto recibe los genericos."""
    provider_id, name = llm_providers.split_model(model_id)
    active = llm_providers.active_providers().get(provider_id or "")
    if not (provider_id == "anthropic" or (active is not None and active.anthropic)):
        return list(_GENERIC_VALID_ARGS)
    if _ANTHROPIC_SAMPLING_MODEL_RE.match(name):
        return ["temperature", "max_tokens"]
    return ["max_tokens"]


def _model_entry(model_id: str, provider: Any = None, free: bool = False) -> dict:
    provider_id, name = llm_providers.split_model(model_id)
    return {
        "id": model_id,
        # spec varios-proveedores REQ-004: el selector agrupa por proveedor.
        "name": name,
        "provider": provider.id if provider is not None else provider_id,
        "provider_label": provider.label if provider is not None else (provider_id or ""),
        "free": free,
        "aliases": [],
        "hosted_on_prem": False,
        "valid_args": _valid_args_for(model_id),
        "reasoning_levels": [],
        # Los modelos actuales aceptan imagenes; si uno no, el chat lo explica
        # (MODELO-IMAGEN en app/friendly_errors.py).
        "supports_image_input": True,
        # Ventana de contexto: solo si el proveedor la informa (spec ia-local); sin dato, `resolve_context_window` usa 200.000.
        "context_window": None,
        "secret_data_compliant": None,
    }


def default_model_id(configured: str | None = None) -> str:
    """El modelo por defecto como "proveedor/modelo" (spec varios-proveedores):
    el configurado (`LLM_MODEL`) del proveedor principal, o si no hay principal,
    el primero de la lista."""
    configured = configured or settings.llm_model
    if llm_providers.has_principal():
        return llm_providers.canonical(configured)
    models = (_MODELS_CACHE.get("data") or {}).get("models") or []
    return models[0]["id"] if models else llm_providers.canonical(configured)


def _provider_catalog() -> tuple[list[dict], list[dict]]:
    """(modelos para el selector, estado de cada proveedor) -- spec
    varios-proveedores REQ-003/004/006."""
    cfg = pc.get_project_config()
    principal = llm_providers.principal_id()
    models: list[dict] = []
    status: list[dict] = []
    for provider in llm_providers.active_providers().values():
        error = None
        try:
            ids = llm_providers.provider_models(provider)
        except Exception as exc:  # noqa: BLE001 -- un proveedor caido no tapa a los demas
            logger.warning("No se pudo obtener la lista de modelos de %s: %s", provider.label, exc)
            # spec proxy-autoheal REQ-006 (de la version de origen): la persona ve una frase con que hacer (un proxy caido manda a
            # Configuracion > Red); el texto crudo de la falla queda solo en el log.
            ids, error = [], describe_model_error(exc).message
        suggested = list(ids) if provider.fixed_models else llm_providers.suggested_models(provider, ids)
        visible = [m for m in cfg.provider_models.get(provider.id, suggested) if m in ids] if ids else []
        # REQ-006: un proveedor gratuito se usa recien cuando el usuario lo
        # confirma (el principal de siempre, configurado a mano, cuenta como confirmado).
        consent = bool(cfg.provider_consent.get(provider.id)) or provider.id == principal
        for name in visible:
            free = llm_providers.is_free(provider.id, name)
            if free and not consent:
                continue
            models.append(_model_entry(f"{provider.id}/{name}", provider, free))
        status.append(
            {
                "id": provider.id,
                "label": provider.label,
                "free": provider.free,
                "consent": consent,
                "principal": provider.id == principal,
                "models": ids,
                "visible": visible,
                "suggested": suggested,
                "error": error,
            }
        )
    return models, status


def model_needs_consent(model_id: str) -> str | None:
    """spec varios-proveedores REQ-006: nombre del proveedor gratuito que el
    usuario todavia no confirmo (None si se puede usar)."""
    provider_id, name = llm_providers.split_model(model_id)
    if not provider_id or provider_id == llm_providers.principal_id():
        return None
    if not llm_providers.is_free(provider_id, name):
        return None
    if pc.get_project_config().provider_consent.get(provider_id):
        return None
    provider = llm_providers.active_providers().get(provider_id)
    return provider.label if provider else provider_id


@app.get("/api/models")
def list_models() -> dict:
    """spec varios-proveedores REQ-002/003/004 (antes: vision-generico REQ-002):
    los modelos de TODOS los proveedores con clave en el `.env`, "proveedor/
    modelo". Se renueva a los 5 minutos o apenas cambia una clave. Siempre
    incluye el modelo por defecto."""
    now = time.monotonic()
    signature = llm_providers.signature()
    if (
        _MODELS_CACHE["data"] is not None
        and (now - _MODELS_CACHE["fetched_at"]) < _MODELS_CACHE_TTL
        and _MODELS_CACHE.get("signature") == signature
    ):
        return _MODELS_CACHE["data"]

    models, status = _provider_catalog()
    failed = [s["label"] for s in status if s["error"]]
    warning = None
    if not status:
        warning = "No hay ningún proveedor de IA con clave en el .env."
    elif failed:
        warning = f"No se pudo consultar la lista de modelos de: {', '.join(failed)}."
        reason = next((s["error"] for s in status if s["error"]), "")
        if reason and reason not in warning:
            warning = f"{warning} {reason}"
    ids = {m["id"] for m in models}
    # El por defecto: el del principal (LLM_MODEL) o, sin principal, el primero.
    default = default_model_id() if llm_providers.has_principal() or not models else models[0]["id"]
    if default not in ids and (llm_providers.has_principal() or not status):
        models.append(_model_entry(default))
    payload = {"models": models, "current_default": default}
    if warning:
        payload["warning"] = warning
        if not models or len(failed) == len(status):
            return payload  # sin cache: se reintenta en la proxima apertura
    _MODELS_CACHE["data"] = payload
    _MODELS_CACHE["by_id"] = {m["id"]: m for m in models}
    # Si algun proveedor no respondio, se guarda lo que si respondio solo 1
    # minuto (para no volver a esperar al caido en cada apertura del selector).
    _MODELS_CACHE["fetched_at"] = now - (_MODELS_CACHE_TTL - 60) if failed else now
    _MODELS_CACHE["signature"] = signature
    return payload


# ---------------------------------------------------------------------------
# spec varios-proveedores REQ-003/006: proveedores con clave en el `.env`, sus
# modelos (cuales se muestran) y el consentimiento de los gratuitos.
# ---------------------------------------------------------------------------
@app.get("/api/providers")
def get_providers() -> dict:
    _, status = _provider_catalog()
    active = {s["id"] for s in status}
    missing = [
        {"id": spec.id, "label": spec.label, "key_names": list(spec.key_names), "free": spec.free}
        for spec in llm_providers.KNOWN.values()
        if spec.id not in active
    ]
    return {"providers": status, "available": missing}


class ProvidersRequest(BaseModel):
    models: dict[str, list[str]] | None = None
    consent: dict[str, bool] | None = None


@app.post("/api/providers")
def set_providers(req: ProvidersRequest) -> dict:
    pc.set_providers(models=req.models, consent=req.consent)
    _MODELS_CACHE.update({"data": None, "fetched_at": 0.0, "by_id": None})
    return get_providers()


@app.get("/api/models/{model_id:path}")
def get_model_detail(model_id: str) -> dict:
    """Devuelve valid_args/reasoning_levels de UN modelo puntual (por id o
    alias), leyendo del cache que arma list_models() -- si el cache esta
    vacio/vencido, lo refresca primero."""
    if _MODELS_CACHE.get("by_id") is None or (time.monotonic() - _MODELS_CACHE["fetched_at"]) >= _MODELS_CACHE_TTL:
        list_models()

    entry = (_MODELS_CACHE.get("by_id") or {}).get(model_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"El modelo '{model_id}' no está disponible en este momento. Elegí otro en el selector de modelos.",
        )
    return entry


def _sse_format(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _drain_session_queue(session, wait_for_turn_id: str | None = None, queue=None):
    """Generador SSE que se SUSCRIBE a los eventos de la sesion (fan-out,
    ver SessionState.subscribe()/push_event() para el detalle) -- no crea
    ni cancela ninguna tarea. Si el cliente corta la conexion (recarga de
    pagina, cambio de sesion, cierre de pestaña), Starlette simplemente
    deja de iterar este generador y el `finally` se desuscribe: el turno
    en background (session.current_task) sigue vivo y corriendo, ya que
    nada lo referencia desde aca.

    `wait_for_turn_id`: si se especifica (REQ-003 -- un mensaje que se
    encolo), esta conexion IGNORA eventos de turnos que no sean el suyo
    (`_turn_id` distinto), salvo los eventos sin turno asociado
    ('queued'/'dequeued', que ya llevan su propio `message_id` en `data`).
    Asi, una conexion abierta para el mensaje B que se encolo mientras A
    esta en curso ve el 'queued' de B pero NO el contenido de A -- recien
    empieza a ver eventos reales cuando el turno de B arranca de verdad
    (su `_turn_id` empieza a coincidir). Si es None (turno que arranca de
    inmediato, o reconexion generica a "lo que sea que este pasando"), no
    se filtra nada.

    `queue`: si ya existe una suscripcion previa (ver el orden critico
    documentado en el endpoint /api/chat -- suscribirse ANTES de lanzar
    la tarea de background, para no perder los primeros eventos), se
    reusa esa cola en vez de crear una nueva aca."""
    owns_queue = queue is None
    if owns_queue:
        queue = session.subscribe()
    try:
        while True:
            event = await queue.get()
            if event.get("type") == "__turn_done__":
                # Solo terminar el stream si el turno que se cerro es el
                # que ESTA conexion esperaba. Si es el "__turn_done__" de
                # OTRO turno (ej. la conexion de B viendo terminar el turno
                # de A), se ignora y se sigue esperando -- el propio turno
                # de B todavia no arranco.
                done_turn_id = event.get("data", {}).get("turn_id")
                if wait_for_turn_id is not None:
                    if done_turn_id == wait_for_turn_id:
                        return
                    continue
                # BUG real corregido (deteccion QA 2026-09-14, 3ra ronda):
                # en una reconexion GENERICA (`wait_for_turn_id=None`, el
                # camino de `startLoop()`/`/api/chat/stream/{id}`) esto
                # cerraba SIEMPRE, sin importar el loop -- y "__turn_done__"
                # se empuja ANTES que "loop-stopped" (ver `finally` de
                # `_launch_turn`: primero `__turn_done__`, recien despues
                # `_maybe_continue_loop` push "loop-stopped"). Cerrar aca
                # perdia ese evento igual que el bug ya corregido mas abajo
                # en el chequeo de "done" -- mismo criterio: no cerrar si ya
                # hay mas eventos esperando en la cola.
                if queue.empty():
                    return
                continue
            if wait_for_turn_id is not None and event.get("_turn_id") not in (
                None,
                wait_for_turn_id,
            ):
                continue
            payload = {k: v for k, v in event.items() if k != "_turn_id"}
            # REQ-014 f (spec fix-detener-y-progreso-visible): cada evento de un turno sale con su `turn_id`, asi el frontend sabe
            # que turno esta viendo responder y Detener frena ESE (no el siguiente de la cola que arranco en los ultimos
            # milisegundos). Los eventos sin turno ('queued'/'dequeued') salen como siempre.
            if event.get("_turn_id"):
                payload["turn_id"] = event["_turn_id"]
            yield _sse_format(payload)
            # REQ-014 c (spec fix-detener-y-progreso-visible): el usuario BORRO el mensaje que esta conexion esperaba mientras
            # seguia en la cola. Ese turno ya no va a arrancar: su conexion se cierra ACA (antes quedaba abierta para siempre y
            # el navegador seguia con el boton en «Detener» y «Vision está trabajando…» sin que corriera nada).
            if (
                wait_for_turn_id is not None
                and event.get("type") == "dequeued"
                and (event.get("data") or {}).get("reason") == "deleted"
                and (event.get("data") or {}).get("message_id") == wait_for_turn_id
            ):
                return
            if event.get("type") in ("done", "error", "stopped", "loop-stopped"):
                # BUG real corregido (deteccion QA 2026-09-14, 4 rondas --
                # historial completo porque cada intento parcial genero un
                # sintoma nuevo y distinto, todos reportados por el usuario
                # probando Loop en uso real):
                #
                # Ronda 1: con `wait_for_turn_id` seteado (un turno puntual
                # que esta conexion esta esperando -- REQ-003 mensajes
                # encolados), "done" SI marca el final para ESTA conexion.
                # Pero en una reconexion GENERICA (`None` -- el camino de
                # `startLoop()`/`/api/chat/stream/{id}`), cerrar apenas
                # llegaba "done" perdia el evento "loop-stopped" (se emite
                # DESPUES, en el `finally` de `_launch_turn`) -- el toggle
                # "Loop" del frontend quedaba pegado en "corriendo" para
                # siempre.
                #
                # Ronda 2: el primer intento de fix (chequear
                # `session.loop_active` aca) no alcanzaba -- `push_event` es
                # sincronico y el `finally` de `_launch_turn` corre sin
                # ningun `await` de por medio, asi que para cuando este
                # consumidor procesa "done" el productor YA corrio ese
                # `finally` completo: `loop_active` ya lee `False` Y
                # "loop-stopped" YA esta encolado, se perdia igual. Fix real:
                # mirar la COLA directamente (`queue.empty()`) antes de
                # cerrar, no un flag de estado.
                #
                # Ronda 3: habia un SEGUNDO punto de cierre incondicional
                # mas arriba, para el evento interno "__turn_done__" (se
                # empuja ANTES que "loop-stopped") -- mismo criterio de
                # "no cerrar si la cola no esta vacia" aplicado ahi tambien.
                #
                # Ronda 4 (esta): "loop-stopped" en si NUNCA estaba en esta
                # lista de tipos terminales -- al no ser "done"/"error"/
                # "stopped", pasaba de largo este chequeo por completo y la
                # conexion quedaba colgada para siempre en `await
                # queue.get()`, esperando un evento que ya no iba a llegar
                # (el loop realmente ya termino, no hay nada mas pendiente).
                # Sintoma real: `startLoop()` (que hace `await
                # renderIncomingStream(...)` y recien DESPUES resetea
                # "Vision esta trabajando…" a "online") quedaba pegado en
                # "busy" para siempre pese a que el loop habia terminado
                # limpio, sin ningun error.
                if wait_for_turn_id is not None or queue.empty():
                    return
    finally:
        session.unsubscribe(queue)


def _resolve_gen_params(req: ChatRequest) -> dict:
    """Los parametros de generacion del request pisan a los guardados en
    Configuracion solo si vinieron explicitos; si no, se usan los
    persistidos (o ninguno, si tampoco hay guardados)."""
    cfg = pc.get_project_config()
    return {
        "temperature": req.temperature if req.temperature is not None else cfg.temperature,
        "top_p": req.top_p if req.top_p is not None else cfg.top_p,
        "max_tokens": req.max_tokens if req.max_tokens is not None else cfg.max_tokens,
        "reasoning_effort": req.reasoning_effort or cfg.reasoning_effort or None,
        "verbosity": req.verbosity or cfg.verbosity or None,
    }


def _text_for_model(text: str) -> str:
    """REQ-005 (spec clasificador-tipo-respuesta): texto que de verdad recibe el modelo. Solo el comando `/plan <pedido>` se
    reescribe; cualquier otro mensaje (incluido `/plan` sin pedido) pasa tal cual. Funcion pura, sin I/O."""
    # REQ-005 (spec clasificador-tipo-respuesta): comando /plan
    # override manual que fuerza la clasificacion a Planificable. Se
    # instrumenta agregandole una instruccion fuerte e in-place al texto
    # del usuario (el LLM se acopla a las instrucciones).
    final_text = text
    if final_text.lstrip().lower().startswith("/plan "):
        pedido = final_text.replace("/plan ", "", 1).strip()
        final_text = (
            "OVER@ride de clasificacion: este mensaje llego con el comando /plan. "
            "Obligatoriamente printea como PRIMER TOKEN [[TIPO: Planificable]] y luego "
            f"responde este pedido: {pedido}"
        )
    return final_text


def _push_internal_turn_error(session, session_id: str, turn) -> None:
    """Falla DENTRO de Vision a mitad de un turno (no del modelo: esas llegan como evento `error` del loop). Llamar SOLO desde
    un `except` (el `logger.exception` necesita la excepcion en curso): deja el detalle tecnico en el log con una referencia
    y le avisa a la persona en lenguaje simple por el mismo stream."""
    internal = describe_internal_error()
    when = datetime.now().astimezone().strftime("%H:%M:%S")
    logger.exception(
        "Fallo interno del turno %s (sesion %s) [%s · %s]",
        turn.id,
        session_id,
        internal.code,
        when,
    )
    session.push_event(
        "error",
        {
            "message": internal.message,
            "code": internal.code,
            "technical": internal.technical(when=when),
        },
    )


def _finish_turn(session, session_id: str, turn) -> None:
    """Cierre de un turno de fondo (con exito, error o Detener): lo marca `done`, descarta lo borrado, avisa `__turn_done__` y
    arranca el siguiente de la cola o del loop. TODO el cuerpo es SINCRONO a proposito (sin `await`): ningun otro pedido puede
    meterse entre el descarte y el arranque del siguiente turno."""
    turn.status = "done"
    # REQ-014 b: el usuario borro este mensaje con el turno ya arrancado -> se descarta (memoria y SQLite) ANTES de
    # arrancar el siguiente de la cola, asi el siguiente nunca ve nada del mensaje borrado. Sin `await` de por medio:
    # ningun otro pedido puede meterse entre el descarte y el arranque del siguiente.
    if turn.id in session.discard_turn_ids:
        try:
            _discard_finished_turn(session, session_id, turn)
        except Exception:  # noqa: BLE001 -- un fallo del descarte nunca deja la sesion trabada ni la cola parada
            logger.exception("No se pudo descartar el turno borrado %s (sesion %s)", turn.id, session_id)
    session.turn_baseline = None
    session.active_turn_id = None
    # "__turn_done__" es una senal INTERNA (nunca llega al
    # frontend, se filtra en _drain_session_queue) que le dice a
    # la conexion que esperaba ESTE turno puntual que ya puede
    # cerrar su stream. Lleva `turn_id` explicito en el `data`
    # (ademas del `_turn_id` que push_event agrega automatico,
    # que en este punto ya es None porque active_turn_id se
    # limpio arriba) para que cada conexion sepa si es SU turno
    # el que termino o el de otro -- bug real detectado en
    # pruebas: antes cualquier conexion que viera CUALQUIER
    # "__turn_done__" cortaba su propio stream, incluso si
    # pertenecia a otro turno (ej. la conexion de B cortaba
    # apenas terminaba A, antes de que el turno de B arrancara).
    session.push_event("__turn_done__", {"turn_id": turn.id})

    # REQ-003/007: apenas termina este turno, si hay algo en cola
    # se dispara automaticamente -- el frontend no necesita
    # reenviar nada. El nuevo turno sigue empujando eventos a la
    # MISMA session.event_queue, asi que cualquier cliente que
    # siga escuchando (via /api/chat/stream/{id}) ve la
    # continuacion sin cortes.
    next_turn = session.dequeue_next_turn()
    if next_turn is not None:
        # REQ-014 d: se avisa que salio de la cola (antes 'dequeued' nunca se emitia y el cartel «En cola · posición N»
        # quedaba pegado). Va ANTES de los eventos del turno y con la cola que queda, para renumerar a los demas.
        session.push_event(
            "dequeued",
            {"message_id": next_turn.id, "reason": "started", "queue": list(session.queue_order)},
        )
        _launch_turn(session, session_id, next_turn)
    elif session.loop_active:
        # spec modo-plan-loop REQ-006/007: sin nada en la cola
        # HUMANA, pero con el loop activo -- se sintetiza el
        # PROXIMO turno automaticamente (mismo mecanismo de
        # auto-encadenamiento de arriba, reusado tal cual).
        _maybe_continue_loop(session, session_id)


def _launch_turn(session, session_id: str, turn) -> None:
    """REQ-003: arranca un UserTurn como la tarea de fondo activa de la
    sesion. Al terminar (con exito, error, o stop), marca el turno como
    'done' y SI hay algo en la cola (REQ-003/007), dispara automaticamente
    el siguiente turno sin que el frontend tenga que reenviarlo -- esto es
    lo que hace que un mensaje mandado mientras Vision esta ocupada 'se
    procese solo' apenas termina el turno anterior."""
    turn.status = "active"
    session.active_turn_id = turn.id
    # REQ-009: registra donde va a quedar el mensaje `user` de este turno
    # DENTRO de session.messages -- run_turn() le agrega el system prompt
    # (solo si session.messages esta vacio) y despues el mensaje user, en
    # ese orden, asi que el indice es exactamente len(messages) ANTES de
    # llamar a run_turn. Se guarda en session.turns para poder ubicar y
    # truncar este turno mas adelante si el usuario lo edita/elimina
    # cuando ya paso a ser "historial viejo".
    turn.messages_start_index = len(session.messages) + (0 if session.messages else 1)
    session.turns[turn.id] = turn
    # REQ-014 b (spec fix-detener-y-progreso-visible): lo que ya habia ANTES de este turno. Si el usuario borra el mensaje
    # con el turno ya arrancado, al terminar se descarta solo lo que quedo despues (ver `_discard_finished_turn`). El loop
    # lo refina despues de compactar (la compactacion rearma la lista).
    session.turn_baseline = list(session.messages)
    # spec office-outlook-teams-mcp REQ-M07b: cada turno EMPIEZA sin la marca de «se leyo texto de otra persona» (correo, chat, transcripcion). Si no, una lectura de ayer seguiria
    # frenando acciones legitimas de hoy en modo Auto. Es solo la marca de lectura: las autorizaciones ya dadas (`m365_write_seen`) sobreviven al cambio de turno.
    from app.m365 import (
        policy as m365_policy,
    )  # import diferido: el paquete m365 importa tools, y estas importan este modulo

    m365_policy.new_turn(session)

    async def _run_in_background():
        final_text = _text_for_model(turn.text)

        # Corre COMPLETAMENTE independiente del generador SSE que lo
        # escucha: se crea con asyncio.create_task() y se guarda en
        # session.current_task, pero nada llama a .cancel() sobre el.
        # Empuja sus eventos DIRECTO a session.event_queue (la misma cola
        # que usan las tools para eventos fuera de banda de todowrite/
        # question), asi un unico consumidor (el generador SSE, sea el de
        # este request o el de un /stream posterior) ve todo en orden.
        try:
            async for event in run_turn(
                session,
                final_text,
                session_id,
                model=turn.model,
                generation_params=turn.generation_params,
                user_message_id=turn.id,
                images=turn.images,
                voice_confirm=turn.voice_confirm,
                narrate=turn.narrate,
                narrate_level=turn.narrate_level,
                attachments=turn.attachments,
            ):
                data = event.get("data", {})
                if event["type"] == "stopped" and turn.id in session.discard_turn_ids:
                    # REQ-014 b: no fue un Detener a secas: el usuario borro este mensaje (la pantalla lo dice asi).
                    data = {**data, "reason": "message_deleted"}
                session.push_event(event["type"], data)
        except Exception:  # turno en background: si no se avisa aca, nadie lo ve
            # Antes solo habia `finally`: si algo fallaba DENTRO de Vision (no
            # en el modelo -- esas fallas ya llegan como evento "error" del
            # loop), la excepcion moria dentro de la Task y el chat terminaba
            # EN SILENCIO, sin ningun aviso (pedido del usuario 2026-09-24:
            # errores entendibles para personas no tecnicas).
            _push_internal_turn_error(session, session_id, turn)
        finally:
            _finish_turn(session, session_id, turn)

    session.current_task = asyncio.create_task(_run_in_background())


def _discard_finished_turn(session, session_id: str, turn) -> None:
    """REQ-014 b (spec fix-detener-y-progreso-visible): el usuario borro el mensaje de este turno cuando ya habia arrancado
    (el borrado llego tarde: el modal de confirmacion seguia abierto mientras el turno anterior terminaba, o despues de
    Detener). Borrarlo equivale a Detener y DESCARTAR: se quitan su mensaje de usuario y todo lo que el turno agrego (texto
    parcial, tool-calls y resultados), en memoria y en SQLite, sin tocar los turnos anteriores.

    Se llama desde el `finally` de `_launch_turn`, con el turno YA terminado: nada mas escribe en `session.messages` en ese
    momento. SQLite va por la misma cola de escritura (en orden): el truncado corre despues de las escrituras de este turno
    y antes de las del siguiente. Antes, un borrado tardio caia en el camino de «historial» con el turno CORRIENDO: truncaba
    `session.messages` debajo del turno, no lo detenia, y el mensaje borrado se respondia entero."""
    session.discard_turn_ids.discard(turn.id)
    removed = session.discard_active_turn_messages()
    persistence_service.truncate_from_message(session_id, turn.id)
    turn.status = "cancelled"
    session.turns.pop(turn.id, None)
    logger.info("Turno %s descartado (el usuario borro su mensaje): %d mensajes quitados", turn.id, removed)


def _request_stop(session) -> None:
    """REQ-001 (parada-y-cola-mensajes): marca el Detener del turno activo. Cancelacion cooperativa (el loop lo ve entre
    pasos y durante el streaming), nunca `Task.cancel()`. Compartido por Detener y por borrar un mensaje cuyo turno ya
    arranco (REQ-014 b: «equivale a Detener ese turno y descartarlo»)."""
    session.stop_requested = True
    session.edit_pending_text = None  # asegura que sea un STOP real, no una edicion
    # spec modo-plan-loop REQ-007b: STOP tambien apaga el loop autonomo --
    # sin esto, _maybe_continue_loop sintetizaria el proximo turno apenas
    # termine el actual, ignorando la intencion real del usuario de frenar.
    _turn_off_loop(session)


def _turn_off_loop(session) -> None:
    """spec modo-plan-loop REQ-007b/011: apaga el loop autonomo (y su modo auto) sin tocar el turno que este corriendo. Lo usa todo
    Detener (`_request_stop`) y tambien el Detener que llega tarde a un turno del loop cuando lo que corre ahora es un mensaje que el
    usuario dejo en cola (REQ-014 f, spec fix-detener-y-progreso-visible, revision: ese mensaje no se frena, pero el loop no sigue)."""
    session.loop_active = False
    session.loop_turns_count = 0
    session.loop_paused_for_question = False
    session.loop_auto = False  # REQ-011: detener el loop apaga el modo auto


LOOP_MAX_TURNS = 30
# spec modo-plan-loop REQ-006: id de los turnos que arma el loop solo (los del usuario usan el id que manda la pagina, un uuid).
# REQ-014 f (spec fix-detener-y-progreso-visible, revision): Detener lo usa para reconocer la continuacion del loop.
LOOP_TURN_ID_PREFIX = "loop-"


def _maybe_continue_loop(session, session_id: str) -> None:
    """spec modo-plan-loop REQ-006/007/009: decide si sintetizar el
    proximo turno del loop autonomo, o detenerlo. Se llama SOLO cuando no
    hay nada en la cola humana (ver _launch_turn) -- nunca compite con un
    mensaje real del usuario."""
    if session.loop_paused_for_question:
        return  # REQ-009: ya esta pausado esperando respuesta humana -- no sintetiza nada mas

    pending = collect_pending_todos(session)
    if not pending:
        # REQ-007a: exito -- no queda nada pendiente, se apaga solo, sin alarma.
        session.loop_active = False
        session.loop_turns_count = 0
        session.loop_auto = False  # REQ-011: terminar apaga el modo auto
        session.push_event("loop-stopped", {"reason": "completed"})
        return

    if session.loop_turns_count >= LOOP_MAX_TURNS:
        # REQ-007c: guardarail de seguridad -- SI dispara alarma (a
        # diferencia del caso de exito), porque el plan no termino solo y
        # amerita revision humana de por que no avanzo en 30 turnos.
        session.loop_active = False
        session.loop_turns_count = 0
        session.loop_paused_for_question = True
        session.loop_auto = False  # REQ-011: max_turns apaga el modo auto
        session.push_event("loop-stopped", {"reason": "max_turns"})
        return

    session.loop_turns_count += 1
    from app.session.events import UserTurn

    synthetic_turn = UserTurn(
        id=f"{LOOP_TURN_ID_PREFIX}{uuid.uuid4()}",
        text=(
            "Continuá automáticamente con la próxima tarea 'pending' de la lista de tareas "
            "(todowrite). Marcala 'completed' cuando termines. Si necesitás tomar una decisión "
            "sin una opción claramente mejor, preguntá con la tool question -- si SÍ hay una "
            "opción claramente mejor, marcala como recomendada y seguí sin preguntar."
        ),
    )
    _launch_turn(session, session_id, synthetic_turn)


def _validated_attachments(req: ChatRequest) -> list[dict] | None:
    """REQ-003 b: cada ruta tiene que estar dentro de `<proyecto>/attachment/` o
    ser una ruta registrada para leer en su lugar (REQ-007). Si no, 400.
    Devuelve dicts completos (metadata real del disco), no lo que dijo el
    cliente: tamano, tipo y ruta canonica salen del archivo."""
    if not req.attachments:
        return None
    from app import attachments as attachments_module
    from app.persistence import service as persistence

    if len(req.attachments) > attachments_module.MAX_FILES_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=f"Solo se pueden adjuntar hasta {attachments_module.MAX_FILES_PER_MESSAGE} archivos por mensaje.",
        )
    project_root = persistence.project_root_for_session(req.session_id)
    if project_root is None:
        raise HTTPException(
            status_code=400,
            detail="Elegí una carpeta para la sesión antes de adjuntar archivos.",
        )
    registered = attachment_service.registered_inplace(req.session_id)
    out: list[dict] = []
    for ref in req.attachments:
        real = attachments_module.resolve_attachment_path(project_root, ref.path, registered)
        if real is None or not real.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"No encuentro el archivo adjunto «{ref.name or ref.path}». Volvé a adjuntarlo.",
            )
        inplace = str(real) in registered
        rel = str(real) if inplace else real.relative_to(project_root.resolve()).as_posix()
        out.append(
            {
                "name": real.name,
                "path": rel,
                "size": real.stat().st_size,
                "mime": ref.mime,
                "kind": attachments_module.kind_for(real.name),
                "inplace": inplace,
                "token_estimate": attachments_module.estimate_tokens_for_file(real),
            }
        )
    return out


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    _assert_session_owner(req.session_id)
    store = get_session_store()
    session = store.get_or_create(req.session_id)

    turn_id = req.message_id or f"srv-{time.monotonic_ns()}"
    from app.session.events import UserTurn

    turn = UserTurn(
        id=turn_id,
        text=req.message,
        model=req.model,
        generation_params=_resolve_gen_params(req),
        images=req.images or None,
        attachments=_validated_attachments(req),
        voice_confirm=req.voice_confirm,
        narrate=req.narrate,
        narrate_level=normalize_level(req.narrate_level),
    )

    if session.is_busy:
        # REQ-003: ya hay un turno corriendo en ESTA sesion -- el mensaje
        # nuevo se ENCOLA (no se descarta ni se mezcla con el turno
        # activo). Esta conexion SOLO debe ver el 'queued' propio y,
        # despues, el contenido de SU turno cuando le llegue -- nunca el
        # contenido del turno A que sigue corriendo (ver
        # _drain_session_queue, wait_for_turn_id).
        #
        # ORDEN CRITICO (bug real detectado en pruebas, ver progress.md):
        # hay que SUSCRIBIRSE antes de encolar/emitir 'queued'. Si se
        # encola primero y recien despues (dentro del generador SSE, que
        # Starlette puede tardar en empezar a iterar) nos suscribimos,
        # existe una ventana donde el turno A podria terminar y disparar
        # el turno de B ANTES de que esta conexion este escuchando -- los
        # primeros eventos de B (incluso el turno completo entero, en un
        # caso extremo) se pierden porque el fan-out solo entrega a
        # suscriptores YA registrados en el momento del push. Suscribirse
        # de forma SINCRONA aca (antes de devolver el StreamingResponse)
        # elimina esa ventana de carrera por completo.
        queue = session.subscribe()
        session.enqueue_turn(turn)
        session.push_event(
            "queued",
            {"message_id": turn.id, "position": session.queue_length - 1},
        )

        # Reusa la MISMA logica de filtrado que _drain_session_queue (ya
        # corregida para distinguir "__turn_done__" de otro turno) en vez
        # de duplicarla aca -- se le pasa la cola ya suscripta.
        return StreamingResponse(
            _drain_session_queue(session, wait_for_turn_id=turn.id, queue=queue),
            media_type="text/event-stream",
        )

    # spec modo-plan-loop REQ-009: si el loop estaba pausado esperando
    # esta respuesta humana (pregunta SIN opcion "(Recomendada)"), este
    # mensaje normal ES esa respuesta -- se limpia el flag de pausa ANTES
    # de lanzar el turno, para que _maybe_continue_loop (disparado al
    # terminar este turno, ver _launch_turn) pueda retomar el loop en vez
    # de quedar frenado para siempre (session.is_busy ya es False en este
    # punto porque el turno anterior ya termino al pausarse, asi que esta
    # respuesta SIEMPRE entra por este camino, nunca por el de cola).
    if session.loop_active and session.loop_paused_for_question:
        session.loop_paused_for_question = False

    # Mismo orden critico que en el caso de cola: suscribirse ANTES de
    # arrancar la tarea de background, para no perder los primeros
    # eventos (text-delta iniciales, etc.) en la ventana entre crear la
    # tarea y que Starlette empiece a iterar el StreamingResponse.
    queue = session.subscribe()
    _launch_turn(session, req.session_id, turn)

    return StreamingResponse(
        _drain_session_queue(session, wait_for_turn_id=turn.id, queue=queue),
        media_type="text/event-stream",
    )


class StopTurnRequest(BaseModel):
    # REQ-014 f (spec fix-detener-y-progreso-visible): el turno que el usuario estaba viendo responder al apretar Detener.
    # None (o sin cuerpo) = el que este corriendo, como siempre.
    turn_id: str | None = None


@app.post("/api/chat/{session_id}/stop")
async def stop_turn_endpoint(session_id: str, req: StopTurnRequest | None = None) -> dict:
    """REQ-001: pide detener el turno activo de la sesion. NO cancela la
        tarea a la fuerza (asyncio.Task.cancel()) -- eso cortaria una tool en
        ejecucion a mitad de camino. En cambio, marca `stop_requested = True`,
        que `run_turn` chequea entre pasos del loop y tambien durante el
    streaming de texto (ver app/llm/loop.py), garantizando que cualquier
        tool ya en curso termine de correr por completo antes de cortar.

    REQ-014 e/f (spec fix-detener-y-progreso-visible): `async def` -- corre en el event loop, igual que el fin de un turno y el
    arranque del siguiente de la cola (`finally` de `_launch_turn`), asi el "¿cual es el turno activo?" y la marca no se
    intercalan con ese cambio (antes corria en el threadpool). Con `turn_id`, solo se detiene si ese sigue siendo el turno
    activo: si ya termino y arranco el siguiente de la cola, NO se lo frena (antes un Detener en los ultimos 10-100 ms del
    turno frenaba al SIGUIENTE mensaje) y se contesta `stopped: false` con el turno que esta corriendo ahora.

    Con el loop autonomo (REQ-014 f, revision; spec modo-plan-loop REQ-007b «un Detener frena el turno y apaga el loop»): si lo
    que corre ahora es la continuacion del loop (un turno `loop-...` que `_maybe_continue_loop` armo apenas termino el que el
    usuario miraba), ese turno NO es un mensaje del usuario: se lo frena y se apaga el loop, como antes de REQ-014 f. Antes
    contestaba `other_turn_active` y el loop seguia hasta un segundo Detener (el turno nuevo no emite nada mientras compacta o
    pide la lista de modelos). Si lo que corre es un mensaje que el USUARIO dejo en cola, no se lo frena, pero el loop se apaga
    igual: no sigue despues de ese mensaje."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None or not session.is_busy:
        return {"stopped": False, "reason": "no_active_turn"}
    wanted = req.turn_id if req is not None else None
    if wanted and wanted != session.active_turn_id:
        if (session.active_turn_id or "").startswith(LOOP_TURN_ID_PREFIX):
            _request_stop(session)
            logger.info(
                "Detener del turno %s: ya era el siguiente del loop (%s), se frena y se apaga el loop",
                wanted,
                session.active_turn_id,
            )
            return {"stopped": True}
        if session.loop_active:
            _turn_off_loop(session)
            logger.info(
                "Detener del turno %s con el loop prendido: se apaga el loop y sigue el mensaje en cola %s",
                wanted,
                session.active_turn_id,
            )
        return {"stopped": False, "reason": "other_turn_active", "active_turn_id": session.active_turn_id}
    _request_stop(session)
    return {"stopped": True}


class StartLoopRequest(BaseModel):
    auto: bool = False  # REQ-011: modo auto (acepta todas las opciones, no pausa)


@app.post("/api/session/{session_id}/clear")
def clear_conversation_endpoint(session_id: str) -> dict:
    """Pedido del usuario (2026-09): borra TODA la conversacion de la sesion
    -- preguntas y respuestas, viejas y nuevas. Limpia el historial en
    memoria (`session.messages`/`turns`/`queue_order`) y el persistido en
    SQLite (`persistence_service.clear_messages`), asi no reaparece al
    recargar. NO toca los todos (el plan), ni el modo, ni el estado del
    loop. Si la sesion esta ocupada (turno en curso) se rechaza con 409:
    primero hay que detener con STOP (no tendria sentido limpiar mientras
    se esta generando una respuesta)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_restore(session_id)  # persistencia-sqlite-sesiones REQ-003: tambien tras un reinicio
    if session is None:
        raise HTTPException(
            404,
            detail="Esta conversación ya no está abierta en ULTRON (puede que se haya reiniciado). Recargá la página.",
        )
    if session.is_busy:
        raise HTTPException(
            409,
            detail="ULTRON todavía está respondiendo en esta conversación. Frenala con el botón Detener y después limpiala.",
        )

    session.messages = []
    session.turns = {}
    session.queue_order = []
    session.push_event("conversation-cleared", {})
    persistence_service.clear_messages(session_id)
    return {"cleared": True}


@app.post("/api/session/{session_id}/loop")
async def start_loop_endpoint(session_id: str, req: StartLoopRequest | None = None) -> dict:
    """spec modo-plan-loop REQ-006/010/011: dispara /loop -- activa
        `loop_active` y, si la sesion no tiene ya un turno corriendo, arranca
        el primer turno sintetico de inmediato (si SI hay un turno activo, el
        loop simplemente se activa y `_maybe_continue_loop` toma la posta
        cuando ese turno termine, via el mismo camino de auto-encadenamiento).

        Bug real corregido (mismo patron ya documentado en AGENTS.md: "un
        endpoint que hace asyncio.create_task() debe ser async def, nunca def
        sync"): este endpoint quedo definido `def` sync -- FastAPI lo corre en
        un thread de threadpool (via run_in_threadpool) que NO tiene un event
        loop de asyncio corriendo, asi que `_maybe_continue_loop` ->
        `_launch_turn` -> `asyncio.create_task()` explotaba con
        'RuntimeError: no running event loop' apenas /loop arrancaba un turno
    de inmediato (sesion no ocupada)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_create(session_id)

    pending = collect_pending_todos(session)
    if not pending:
        raise HTTPException(400, detail="No hay tareas pendientes para ejecutar en loop.")

    session.loop_active = True
    session.loop_turns_count = 0
    session.loop_paused_for_question = False
    session.loop_auto = req.auto if req else False  # REQ-011: modo auto del loop

    if not session.is_busy:
        _maybe_continue_loop(session, session_id)

    return {"loop_started": True, "pending_count": len(pending)}


class SetModeRequest(BaseModel):
    mode: str  # "plan" | "active"


@app.post("/api/session/{session_id}/mode")
def set_session_mode_endpoint(session_id: str, req: SetModeRequest) -> dict:
    """spec modo-plan-loop REQ-001/003/004: alterna Modo Plan/Activo para
        ESTA sesion -- NO reinicia session.messages/todos (REQ-004, el
    contexto se preserva). Se valida server-side (nunca confia en el
        frontend) porque este valor determina si write/edit/bash se filtran
        del payload real hacia el LLM (ver app/llm/loop.py)."""
    _assert_session_owner(session_id)
    if req.mode not in ("plan", "active"):
        raise HTTPException(400, detail="El modo debe ser 'plan' o 'active'.")
    store = get_session_store()
    session = store.get_or_create(session_id)
    session.mode = req.mode
    return {"mode": session.mode}


class EditMessageRequest(BaseModel):
    text: str


def _user_content_text(content) -> str:
    """Texto de un mensaje `user` de session.messages (str, o lista con bloques text/image_url)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _locate_closed_user_message(session, session_id: str, message_id: str, turn, snapshot: dict | None) -> int | None:
    """editar-mensaje-tras-reinicio REQ-001/004/006: indice en `session.messages` del mensaje `user` ya cerrado.

    1) `turn.messages_start_index` si SIGUE apuntando a ese mensaje (se valida por texto: el indice en
       memoria se corre al borrar respuestas anteriores y apuntaria al mensaje equivocado).
    2) Fallback `idx:N` (clientes viejos).
    3) SQLite: la base guarda el id real de cada mensaje -> posicion entre los mensajes `user`; es lo unico que
       sobrevive a un reinicio. Se valida igual por texto.
    Devuelve None si no se puede ubicar con certeza (nunca adivina: truncar el mensaje equivocado borraria
    conversacion que el usuario no pidio tocar)."""
    msgs = session.messages

    def matches(i: int | None, expected: str | None) -> bool:
        if i is None or i < 0 or i >= len(msgs) or msgs[i].get("role") != "user":
            return False
        return expected is None or _user_content_text(msgs[i].get("content")).startswith(expected)

    expected = (turn.text if turn is not None else None) or (snapshot or {}).get("text")
    if turn is not None and turn.messages_start_index is not None and matches(turn.messages_start_index, expected):
        return turn.messages_start_index
    if message_id.startswith("idx:"):
        try:
            return session.find_user_message_index(int(message_id.removeprefix("idx:")))
        except ValueError:
            return None
    occurrence = persistence_service.find_user_occurrence(session_id, message_id)
    if occurrence is None:
        return None
    found = _nth_user_message_index(msgs, occurrence)
    return found if found is not None and matches(found, expected) else None


def _resendable_files(session_id: str, saved_files: list[dict]) -> list[dict] | None:
    """editar-mensaje-tras-reinicio REQ-003: archivos guardados en SQLite que SIGUEN siendo validos para
    reenviar. Se revalida cada ruta contra la carpeta del proyecto (o las registradas para leer en su lugar):
    lo guardado no se confia. Un archivo eliminado o que ya no existe se descarta, nunca se reenvia."""
    if not saved_files:
        return None
    from app import attachments as attachments_module

    project_root = persistence_service.project_root_for_session(session_id)
    if project_root is None:
        return None
    registered = attachment_service.registered_inplace(session_id)
    out: list[dict] = []
    for f in saved_files:
        real = attachments_module.resolve_attachment_path(project_root, f.get("path") or "", registered)
        if real is None or not real.is_file():
            continue
        inplace = str(real) in registered
        out.append(
            {
                "name": real.name,
                "path": str(real) if inplace else real.relative_to(project_root.resolve()).as_posix(),
                "size": real.stat().st_size,
                "mime": f.get("mime", ""),
                "kind": attachments_module.kind_for(real.name),
                "inplace": inplace,
                "token_estimate": attachments_module.estimate_tokens_for_file(real),
            }
        )
    return out or None


@app.patch("/api/chat/{session_id}/message/{message_id}")
async def edit_message_endpoint(session_id: str, message_id: str, req: EditMessageRequest) -> dict:
    """Edita un mensaje de usuario, con 3 casos distintos segun donde este
        ese mensaje ahora mismo (ver spec REQ-004/005/009):
            1. Es el turno ACTIVO ahora mismo -> REQ-004: se detiene la
               generacion en curso (mismo mecanismo que STOP) SIN perder el
               progreso ya logrado (tool-calls/resultados/todowrite de esa
               corrida quedan intactos), y se continua el MISMO turno con el
               texto editado.
            2. Esta EN COLA (todavia no arranco) -> REQ-005: se edita el
               texto en el lugar, se procesara con el contenido nuevo cuando
               le llegue el turno.
            3. Es HISTORIAL YA CERRADO (ni activo ni en cola) -> REQ-009: se
               trunca `session.messages` desde ese punto y se arranca un
               turno nuevo con el texto editado, descartando lo que vino
    despues en la conversacion.
    """
    _assert_session_owner(session_id)
    store = get_session_store()
    # editar-mensaje-tras-reinicio REQ-001: tras un reinicio la sesion no esta en memoria pero SI en SQLite;
    # `get_or_create` la reconstruye desde la base. Una sesion que no existe en ningun lado sigue dando 404.
    session = store.get_or_create(session_id) if store.exists(session_id) else None
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Esta conversación ya no está abierta en ULTRON (puede que se haya reiniciado). Recargá la página.",
        )

    # Caso 1: turno activo ahora mismo (REQ-004)
    if session.active_turn_id == message_id and session.is_busy:
        session.edit_pending_text = req.text
        session.stop_requested = True
        return {"case": "active_turn_edited", "message_id": message_id}

    # Caso 2: en cola, todavia no arranco (REQ-005)
    if session.edit_queued_turn(message_id, req.text):
        return {"case": "queued_turn_edited", "message_id": message_id}

    # Caso 3: historial ya cerrado (REQ-009) -- el frontend manda el
    # message_id que asigno originalmente a ese mensaje de usuario, pero
    # session.messages (formato OpenAI) no tiene ids propios. Se ubica por
    # el turno registrado en `session.turns` (si existe con
    # messages_start_index) o, como fallback razonable para mensajes de
    # sesiones creadas antes de esta feature, se interpreta message_id
    # como "posicion desde el final" si viene con el prefijo especial
    # 'idx:N' (usado por el frontend cuando no hay un id real registrado).
    turn = session.turns.get(message_id)
    snapshot = persistence_service.user_message_snapshot(session_id, message_id)
    start = _locate_closed_user_message(session, session_id, message_id, turn, snapshot)

    if start is None:
        raise HTTPException(
            status_code=404,
            detail="No encontré ese mensaje en la conversación: puede que ya se haya borrado. Recargá la página.",
        )

    # Trunca desde ese mensaje user en adelante (descarta su respuesta
    # original y todo lo posterior) y arranca un turno nuevo con el texto
    # editado, como si fuera el mensaje mas reciente de la conversacion.
    # editar-mensaje-tras-reinicio REQ-002/007: la base se trunca igual, ANTES de relanzar (misma cola de
    # escritura): sin esto el turno nuevo reutilizaba el id y SQLite le pegaba el texto nuevo al viejo.
    # REQ-014 c (spec fix-detener-y-progreso-visible, revision): con OTRO turno corriendo se trunca solo hasta donde
    # empieza ese turno (antes se llevaba tambien su mensaje y su salida, en memoria y en SQLite) y el editado espera en
    # la cola, igual que al borrar.
    _truncate_closed_history(session, session_id, message_id, start, after_stop="editalo")
    turn_id = message_id
    from app.session.events import UserTurn

    # REQ-003 e (adjuntar-archivos-chat) + editar-mensaje-tras-reinicio REQ-003: el reenvio conserva los
    # archivos e imagenes del mensaje. Salen del turno en memoria; si no esta (servidor reiniciado), de lo
    # guardado en SQLite, con las rutas revalidadas.
    if turn is not None:
        images, attachments = turn.images, turn.attachments
    else:
        images = (snapshot or {}).get("images") or None
        attachments = _resendable_files(session_id, (snapshot or {}).get("files") or [])
    new_turn = UserTurn(
        id=turn_id,
        text=req.text,
        images=images,
        attachments=attachments,
        model=getattr(turn, "model", None),
        generation_params=getattr(turn, "generation_params", None),
    )
    if session.is_busy:
        session.enqueue_turn(new_turn)
        session.push_event("queued", {"message_id": turn_id, "position": session.queue_length - 1})
    else:
        _launch_turn(session, session_id, new_turn)
    return {"case": "history_truncated_and_relaunched", "message_id": message_id}


@app.delete("/api/chat/{session_id}/message/{message_id}")
async def delete_message_endpoint(session_id: str, message_id: str) -> dict:
    """Elimina un mensaje de usuario. REQ-014 (spec fix-detener-y-progreso-visible, regla del usuario: «Detener como hoy, pero
    si elimino el mensaje con el botón borrar, se elimina de la cola»): un mensaje que el usuario BORRA no se responde nunca.
      a. Sigue EN COLA (REQ-005/007 de parada-y-cola-mensajes): sale de la cola sin procesarse, como antes. Ademas se avisa
         con 'dequeued' (reason 'deleted', con la cola que queda): la conexion que esperaba ese mensaje se CIERRA (c; antes
         quedaba abierta para siempre y el boton seguia en «Detener») y los demas se renumeran (d).
      b. Su turno YA ARRANCO (el borrado llego tarde: el modal de confirmacion seguia abierto cuando el turno anterior
         termino, o despues de Detener; antes caia en el camino de historial, le truncaba el historial al turno EN CURSO y el
         mensaje se respondia entero): equivale a Detener ese turno y descartarlo. Al terminar, `_launch_turn` quita su
         mensaje y su salida parcial de memoria y de SQLite (`_discard_finished_turn`) antes de arrancar el siguiente.
      c. Historial ya cerrado (REQ-009): se trunca desde ese mensaje, como antes. Si hay OTRO turno corriendo, se trunca solo
         hasta donde empieza ese turno: lo que esta respondiendo ahora no es historial cerrado (antes perdia su propio
         mensaje y seguia respondiendo sobre un historial roto, en memoria y en SQLite).
    `async def` (REQ-014 e): corre en el event loop, igual que el desencolado del `finally` de `_launch_turn` -- sin carreras
    entre "¿esta en la cola?/¿es el turno activo?" y el arranque del siguiente (antes, en el threadpool: ValueError -> 500)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_create(session_id)

    if session.remove_queued_turn(message_id):
        session.push_event(
            "dequeued", {"message_id": message_id, "reason": "deleted", "queue": list(session.queue_order)}
        )
        return {"case": "queued_turn_removed", "message_id": message_id}

    if session.is_busy and session.active_turn_id == message_id:
        session.discard_turn_ids.add(message_id)
        _request_stop(session)
        logger.info(
            "Mensaje %s borrado con su turno en curso: se detiene y se descarta (sesion %s)", message_id, session_id
        )
        return {"case": "active_turn_discarded", "message_id": message_id}

    turn = session.turns.get(message_id)
    snapshot = persistence_service.user_message_snapshot(session_id, message_id)
    start = _locate_closed_user_message(session, session_id, message_id, turn, snapshot)

    if start is None:
        raise HTTPException(
            status_code=404,
            detail="No encontré ese mensaje en la conversación: puede que ya se haya borrado. Recargá la página.",
        )

    # editar-mensaje-tras-reinicio REQ-004: tambien en la base; si no, el mensaje borrado reaparecia al reiniciar.
    _truncate_closed_history(session, session_id, message_id, start, after_stop="borralo")
    return {"case": "history_truncated", "message_id": message_id}


def _truncate_closed_history(session, session_id: str, message_id: str, start: int, after_stop: str) -> None:
    """Trunca el historial YA CERRADO desde el mensaje de usuario `message_id` (indice `start` en `session.messages`), en memoria y
    en SQLite (misma cola de escritura que el resto). Lo comparten borrar (REQ-009/REQ-014 c) y editar (REQ-009) un mensaje viejo.

    REQ-014 c (spec fix-detener-y-progreso-visible; la revision encontro que editar tenia el mismo defecto): si hay OTRO turno
    corriendo, se trunca solo hasta donde empieza ese turno -- lo que esta respondiendo ahora no es historial cerrado. Antes se
    truncaba hasta el final: el turno en curso perdia su propio mensaje y seguia respondiendo sobre un historial roto, en memoria y
    en SQLite. `after_stop` completa el aviso para personas del caso que no deberia pasar («... y después borralo/editalo»)."""
    running_turn_id = session.active_turn_id if session.is_busy else None
    if running_turn_id is None:
        del session.messages[start:]
        persistence_service.truncate_from_message(session_id, message_id)
        return
    running_start = session.active_turn_start_index()
    if start >= running_start:
        # No deberia pasar (el turno activo sale por su propio caso): ante la duda no se toca nada antes que romper lo que corre.
        raise HTTPException(
            status_code=409,
            detail=f"Ese mensaje es parte de lo que ULTRON está respondiendo ahora. Frenala con el botón Detener y después {after_stop}.",
        )
    del session.messages[start:running_start]
    persistence_service.truncate_from_message(session_id, message_id, until_message_id=running_turn_id)


def _assistant_block_end(messages: list[dict], user_idx: int) -> int:
    """Indice (exclusivo) donde termina la respuesta del turno cuyo mensaje de usuario esta en `user_idx`: todo lo que le sigue
    mientras sea `assistant` o `tool` (texto, tool-calls y sus resultados). Se detiene en el PROXIMO mensaje de usuario o al
    acabarse la lista. Si el turno no tiene respuesta devuelve `user_idx + 1`. Funcion pura."""
    end = user_idx + 1
    while end < len(messages) and messages[end].get("role") in ("assistant", "tool"):
        end += 1
    return end


def _nth_user_message_index(messages: list[dict], occurrence: int) -> int | None:
    """Indice en `messages` del mensaje de usuario que ocupa el lugar `occurrence` (0 = el primero) entre los de usuario, o None
    si no hay tantos. Es como se ubica un turno cuando no hay `message_id` en memoria (sesion reconstruida desde SQLite)."""
    seen = 0
    for i, message in enumerate(messages):
        if message.get("role") == "user":
            if seen == occurrence:
                return i
            seen += 1
    return None


@app.delete("/api/chat/{session_id}/response/{message_id}")
def delete_response_endpoint(session_id: str, message_id: str) -> dict:
    """eliminar-respuestas REQ-003/004/005: borrado QUIRURGICO de la
        respuesta del asistente que le sigue al turno de usuario `message_id`
    -- a diferencia de delete_message_endpoint (que TRUNCA todo lo
        posterior), aca se borra UNICAMENTE el bloque assistant+tool de esa
        respuesta puntual, dejando el resto de la conversacion intacto. Mismo
        criterio de resolucion de `start` que edit/delete de mensajes de
        usuario (turn.messages_start_index, o fallback 'idx:N'), con un
        fallback adicional para sesiones RECONSTRUIDAS (ver abajo, BUG real
        reportado por el usuario)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_create(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Esta conversación ya no está abierta en ULTRON (puede que se haya reiniciado). Recargá la página.",
        )

    # REQ-006 (reglas de negocio): no se puede borrar el turno activo
    # ahora mismo -- para eso esta STOP, no este endpoint.
    if session.active_turn_id == message_id and session.is_busy:
        raise HTTPException(
            status_code=409,
            detail="Esa respuesta todavía se está escribiendo. Frenala con el botón Detener y después eliminala.",
        )

    # BUG CORREGIDO durante validacion E2E: `messages_start_index` YA ES
    # el indice del propio mensaje 'user' (ver _launch_turn linea ~320:
    # "registra donde va a quedar el mensaje user"), NO el indice
    # siguiente -- restarle 1 apuntaba un mensaje antes de la cuenta,
    # rompiendo la deteccion de "role == user" de mas abajo y devolviendo
    # 404 (visible como error de consola en Playwright: el fetch DELETE
    # devolvia 404 silenciosamente ignorado por el frontend).
    turn = session.turns.get(message_id)
    if turn is not None and turn.messages_start_index is not None:
        user_idx = turn.messages_start_index
    elif message_id.startswith("idx:"):
        user_idx = session.find_user_message_index(int(message_id.removeprefix("idx:")))
    else:
        user_idx = None

    # BUG REAL reportado por el usuario (2026-09): al borrar una respuesta
    # individual en una sesion RECONSTRUIDA (backend reiniciado / historial
    # recargado desde SQLite), `session.turns` esta VACIO -- solo hay
    # `session.messages` (que ademas no lleva message_id). El endpoint
    # devolvia 404 silencioso y el frontend no quitaba nada de pantalla.
    # Fallback: resolver la ocurrencia del user message por su `id` real
    # desde SQLite (que si persiste el message_id), y borrar por ocurrencia.
    if (
        user_idx is None
        or user_idx < 0
        or user_idx >= len(session.messages)  # FIX (2026-09): selo desbordaba -> IndexError 500
        or session.messages[user_idx].get("role") != "user"
    ):
        occurrence = persistence_service.find_user_occurrence(session_id, message_id)
        if occurrence is None:
            raise HTTPException(
                status_code=404,
                detail="No encontré esa pregunta en la conversación: puede que ya se haya borrado. Recargá la página.",
            )
        user_occurrence = occurrence
        # Sincronizar el historial EN MEMORIA (si esta hidratado): hallar el
        # indice real del user message en session.messages por ocurrencia.
        user_idx = _nth_user_message_index(session.messages, occurrence)
        if user_idx is not None:
            end = _assistant_block_end(session.messages, user_idx)
            if end > user_idx + 1:
                del session.messages[user_idx + 1 : end]
        persistence_service.delete_assistant_response(session_id, user_occurrence)
        return {"case": "response_deleted", "message_id": message_id}

    # REQ-004: el bloque a borrar es TODO lo que sigue inmediatamente al
    # mensaje 'user' mientras sea 'assistant' o 'tool' -- se detiene apenas
    # aparece el PROXIMO 'user' (inicio de otro turno) o se acaba la lista.
    end = _assistant_block_end(session.messages, user_idx)

    if end == user_idx + 1:
        raise HTTPException(
            status_code=404,
            detail="Ese turno todavia no tiene ninguna respuesta para eliminar.",
        )

    del session.messages[user_idx + 1 : end]

    # REQ-005: consistencia SQLite -- se identifica el turno por CUANTOS
    # mensajes 'user' lo preceden (0-indexed), NO por el indice absoluto.
    user_occurrence = sum(1 for m in session.messages[:user_idx] if m.get("role") == "user")
    persistence_service.delete_assistant_response(session_id, user_occurrence)

    return {"case": "response_deleted", "message_id": message_id}


class ReorderQueueRequest(BaseModel):
    message_id: str
    new_position: int


@app.post("/api/chat/{session_id}/queue/reorder")
def reorder_queue_endpoint(session_id: str, req: ReorderQueueRequest) -> dict:
    """REQ-007: mueve un mensaje YA en cola a una nueva posicion."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Esta conversación ya no está abierta en ULTRON (puede que se haya reiniciado). Recargá la página.",
        )
    ok = session.reorder_queue(req.message_id, req.new_position)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Ese mensaje ya no está en la cola (puede que ya se haya enviado).",
        )
    return {"reordered": True, "queue_order": session.queue_order}


@app.get("/api/chat/stream/{session_id}")
async def chat_stream_attach(session_id: str) -> StreamingResponse:
    """Permite 're-engancharse' a una sesion que sigue procesando en
    background (ej. el usuario cambio de sesion mientras Vision pensaba, y
        ahora volvio a esta). Si la sesion no esta 'busy', devuelve un stream
        vacio de inmediato (nada que escuchar)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None or not session.is_busy:

        async def _empty():
            return
            yield  # pragma: no cover - hace de _empty un generador async

        return StreamingResponse(_empty(), media_type="text/event-stream")

    return StreamingResponse(_drain_session_queue(session), media_type="text/event-stream")


def collect_pending_todos(session) -> list[dict]:
    """[refactor-monolitos REQ-002 + spec modo-plan-loop] Duplicado que estaba
    en 2 endpoints (loop automático y /loop). Cuenta trabajo pendiente NO SOLO
    en tareas top-level sino TAMBIEN dentro de `subtasks`: en la practica el LLM
    suele marcar la tarea principal como `in_progress` y dejar sus pasos
    `pending`, y el loop debe seguir mientras haya cualquier paso pendiente
    (pedido del usuario: 'cualquier proceso tiene pasos'). Sin esto, /loop
    fallaba 400 'no hay tareas pendientes' teniendo sub-tareas pending."""
    out: list[dict] = []
    for t in session.todos:
        if t.get("status") == "pending":
            out.append(t)
        for sub in t.get("subtasks") or []:
            if sub.get("status") == "pending":
                out.append(sub)
    return out


@app.get("/api/todos/{session_id}")
def get_todos(session_id: str) -> dict:
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_restore(session_id)  # persistencia-sqlite-sesiones REQ-003: tambien tras un reinicio
    return {"todos": session.todos if session else []}


# ---------------------------------------------------------------------------
# Programador de alarmas (spec programador-alarmas) -- persistencia GLOBAL en
# el DB de Vision (pedido del usuario 2026-09: no por-sesion). La tabla
# `schedules` vive en el DB maestro (`master_index.py`), valida sin importar
# la sesion activa. El motor de disparo es FRONTEND (timers, REQ-005) que
# persiste via esta API.
# ---------------------------------------------------------------------------
def _reject_invalid_trigger(kind: str, trigger: dict | None) -> None:
    """PQ-004 / BL-019: 422 con un mensaje para personas si el horario no es valido (ver app/scheduler/triggers.py)."""
    from app.scheduler.triggers import validate_trigger

    problem = validate_trigger(kind, trigger)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)


def _reject_invalid_edit(current: dict, patch: dict) -> None:
    """Valida una edicion SOLO si toca `kind` o `trigger`: asi un agente o alarma ya guardados con un horario malo (de antes de esta
    validacion) se pueden apagar o renombrar. Se valida el resultado COMBINADO (lo que quedaria guardado)."""
    if "kind" not in patch and "trigger" not in patch:
        return
    _reject_invalid_trigger(patch.get("kind", current["kind"]), patch.get("trigger", current["trigger"]))


class ScheduleCreate(BaseModel):
    label: str = ""
    kind: str = "once"  # once | recurring
    trigger: dict = {}
    sound: str = "beep"
    file_path: str = ""  # ruta opcional .exe/.bat/.ps1 a ejecutar al disparar
    enabled: bool = True


class SchedulePatch(BaseModel):
    label: str | None = None
    kind: str | None = None
    trigger: dict | None = None
    sound: str | None = None
    file_path: str | None = None
    enabled: bool | None = None


@app.get("/api/alarms")
def list_alarms() -> dict:
    from app.persistence import master_index as mi

    return {"schedules": mi.list_schedules()}


@app.post("/api/alarms")
def create_alarm(body: ScheduleCreate) -> dict:
    from app.persistence import master_index as mi

    _reject_invalid_trigger(body.kind, body.trigger)
    schedule = mi.create_schedule(
        uuid.uuid4().hex,
        body.label,
        body.kind,
        body.trigger,
        body.sound,
        body.file_path,
        body.enabled,
    )
    return {"schedule": schedule}


@app.put("/api/alarms/{schedule_id}")
def update_alarm(schedule_id: str, body: SchedulePatch) -> dict:
    from app.persistence import master_index as mi

    patch = body.model_dump(exclude_unset=True)
    current = mi.get_schedule(schedule_id)
    if current is not None:
        _reject_invalid_edit(current, patch)
    schedule = mi.update_schedule(schedule_id, patch)
    if schedule is None:
        raise HTTPException(status_code=404, detail="No se encontró la alarma.")
    return {"schedule": schedule}


@app.delete("/api/alarms/{schedule_id}")
def delete_alarm(schedule_id: str) -> dict:
    from app.persistence import master_index as mi

    if not mi.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="No se encontró la alarma.")
    return {"deleted": True}


@app.post("/api/alarms/{schedule_id}/fired")
def mark_alarm_fired(schedule_id: str) -> dict:
    """REQ-006: el frontend informa que disparo ESE programado (para
    persistir `last_fired_at` y no re-disparar un 'once' vencido al recargar)."""
    from app.persistence import master_index as mi

    mi.update_schedule_last_fired(schedule_id, time.time())
    return {"ok": True}


@app.post("/api/alarms/{schedule_id}/run")
def run_alarm_file(schedule_id: str) -> dict:
    """Ejecuta el archivo opcional (`file_path`) asociado a un programado,
    lanzandolo con su app por defecto / como ejecutable (`os.startfile`).
    Riesgo alto (ejecutar .exe/.bat/.ps1): SOLO corre la ruta guardada en la
    DB (no acepta rutas arbitrarias del cliente) y se audita en audit.log --
    mismo criterio 'acceso total sin friccion' compensado con rastro pasivo
    que usa el resto de las tools peligrosas."""
    import os

    from app.persistence import master_index as mi
    from app.tools import base as tool_base

    schedule = mi.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="No se encontró la alarma.")
    path = (schedule.get("file_path") or "").strip()
    if not path:
        return {
            "ok": False,
            "message": "Esta alarma no tiene asociado un archivo a ejecutar.",
        }
    try:
        # os.startfile: abre/ejecuta con la app por defecto de Windows
        # (equivale a doble clic). Windows unicamente -- Vision es local en win32.
        os.startfile(path)
    except OSError as exc:
        tool_base._audit("run_file", {"path": path, "status": "error", "error": str(exc)})
        raise HTTPException(status_code=400, detail=describe_os_error(exc).message) from exc
    tool_base._audit("run_file", {"path": path, "status": "ok"})
    return {"ok": True, "message": f"Ejecutado: {path}"}


class SnoozeConfigRequest(BaseModel):
    minutes: int


@app.get("/api/settings/snooze")
def get_snooze_config() -> dict:
    """Minutos de "posponer" (boton Zzz de la alerta visual, pedido del
    usuario 2026-09-17) -- UN solo numero global para agentes Y alarmas, en
    `app_settings` (`<carpeta de datos>/vision.db`), no por item."""
    from app.scheduler.agents import get_snooze_minutes

    return {"minutes": get_snooze_minutes()}


@app.post("/api/settings/snooze")
def set_snooze_config(req: SnoozeConfigRequest) -> dict:
    from app.persistence import master_index as mi
    from app.scheduler.agents import SNOOZE_SETTING_KEY

    minutes = max(1, min(60, req.minutes))
    mi.set_app_setting(SNOOZE_SETTING_KEY, str(minutes))
    return {"minutes": minutes}


# REQ-001 (spec agentes-huerfanos-y-gestion-skills): sesion de reserva para
# agentes que quedan sin sesion destino -- ver docstring de
# `get_fallback_session_id` en app/scheduler/agents.py. Mismo patron GET/POST
# que /api/settings/snooze arriba.
class AgentFallbackSessionRequest(BaseModel):
    session_id: str = ""


@app.get("/api/settings/agent-fallback-session")
def get_agent_fallback_session_config() -> dict:
    from app.scheduler.agents import get_fallback_session_id

    return {"session_id": get_fallback_session_id()}


@app.post("/api/settings/agent-fallback-session")
def set_agent_fallback_session_config(req: AgentFallbackSessionRequest) -> dict:
    from app.persistence import master_index as mi
    from app.scheduler.agents import AGENT_FALLBACK_SESSION_KEY

    session_id = (req.session_id or "").strip()
    mi.set_app_setting(AGENT_FALLBACK_SESSION_KEY, session_id)
    return {"session_id": session_id}


# ---------------------------------------------------------------------------
# Agentes programados (spec agentes-programados) -- persistencia GLOBAL, mismo
# criterio que `schedules` arriba: el motor de disparo de AMBAS tablas es el
# BACKEND (app/scheduler/agents.py y app/scheduler/alarms.py respectivamente),
# siguen corriendo aunque no haya ningun navegador abierto. Un agente
# automatiza uno de 3 tipos (`automation_type`): 'command' (secuencia de
# comandos propios, en orden), 'skill' (una skill), o 'file' (un archivo
# ejecutable) -- Alarmas y Recordatorios se quedan en `schedules`/
# `/api/alarms` (CRUD identico, sin cambios) pero ahora suenan/muestran un
# toast NATIVO de Windows via app/scheduler/alarms.py, no dependen de una
# pestaña abierta.
# ---------------------------------------------------------------------------
# Adjuntos de archivo (spec adjuntar-archivos-chat). Rutas finas: la logica y las
# validaciones viven en app/attachment_service.py. Toda ruta que manda el cliente
# se valida con resolve() contra <proyecto>/attachment/ o una ruta registrada.
# ---------------------------------------------------------------------------
def _attachment_error(exc: attachment_service.AttachmentError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


class AttachmentPathRequest(BaseModel):
    path: str


class AttachmentCleanupRequest(BaseModel):
    # ids de la vista previa (`GET .../attachments/cleanup-preview`) a borrar.
    delete: list[str] = []


class AttachmentThresholdsRequest(BaseModel):
    warn_mb: int | None = None
    inplace_mb: int | None = None


@app.post("/api/sessions/{session_id}/attachments")
async def upload_attachment(session_id: str, request: Request, name: str = "") -> dict:
    """REQ-002: sube UN archivo (cuerpo crudo, en streaming) a `attachment/`.
    El nombre viaja por query (`?name=`, url-encoded); no se usa multipart."""
    _assert_session_owner(session_id)
    length = request.headers.get("content-length")
    try:
        return await attachment_service.upload(
            session_id,
            name or "archivo",
            request.stream(),
            int(length) if length and length.isdigit() else None,
        )
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.delete("/api/sessions/{session_id}/attachments/{name}")
def remove_pending_attachment(session_id: str, name: str) -> dict:
    """REQ-002 a: quita un adjunto pendiente (que ningun mensaje referencia)."""
    _assert_session_owner(session_id)
    try:
        return attachment_service.remove_pending(session_id, name)
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.post("/api/sessions/{session_id}/attachments/register-path")
def register_attachment_path(session_id: str, req: AttachmentPathRequest) -> dict:
    """REQ-007: registra una ruta absoluta para leerla en su lugar (sin copiar)."""
    _assert_session_owner(session_id)
    try:
        return attachment_service.register_path(session_id, req.path)
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.post("/api/sessions/{session_id}/attachments/open")
def open_session_attachment(session_id: str, req: AttachmentPathRequest) -> dict:
    """REQ-004 d/e: abre un adjunto de ESTA sesion con la app de Windows; un
    ejecutable nunca se ejecuta (se muestra en el Explorador)."""
    _assert_session_owner(session_id)
    try:
        return attachment_service.open_attachment(session_id, req.path)
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.get("/api/sessions/{session_id}/attachments/cleanup-preview")
def attachments_cleanup_preview(session_id: str) -> dict:
    """REQ-009 a: que se va a liberar (archivos, imagenes, huerfanos)."""
    _assert_session_owner(session_id)
    try:
        return attachment_service.cleanup_preview(session_id)
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.post("/api/sessions/{session_id}/attachments/cleanup")
async def attachments_cleanup(session_id: str, req: AttachmentCleanupRequest) -> dict:
    """REQ-009 b/c/d: borra del disco lo elegido, deja tombstones y compacta la base."""
    _assert_session_owner(session_id)
    try:
        return await asyncio.to_thread(attachment_service.cleanup_apply, session_id, req.delete)
    except attachment_service.AttachmentError as exc:
        raise _attachment_error(exc) from exc


@app.get("/api/sessions/{session_id}/attachments/summary")
def attachments_summary(session_id: str) -> dict:
    """REQ-009 g: la sesion tiene adjuntos? (decide si eliminarla lleva 2 pasos)."""
    _assert_session_owner(session_id)
    return {"has_attachments": attachment_service.has_attachments(session_id)}


@app.get("/api/config/attachments")
def get_attachment_thresholds() -> dict:
    """REQ-007 a: umbrales de tamano de un adjunto (MB)."""
    return attachment_service.thresholds()


@app.post("/api/config/attachments")
def set_attachment_thresholds(req: AttachmentThresholdsRequest) -> dict:
    try:
        cfg = pc.set_attachment_thresholds(req.warn_mb, req.inplace_mb)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"warn_mb": cfg.attachment_warn_mb, "inplace_mb": cfg.attachment_inplace_mb}


class DocumentImagesRequest(BaseModel):
    min_px: int | None = None
    ask_count: int | None = None
    ask_pct: int | None = None


def _document_images_payload(cfg) -> dict:
    return {"min_px": cfg.image_min_px, "ask_count": cfg.image_ask_count, "ask_pct": cfg.image_ask_pct}


@app.get("/api/config/document-images")
def get_document_images_thresholds() -> dict:
    """spec leer-imagenes-pdf-word REQ-007: filtro y umbral de costo de la lectura de imagenes de PDF/Word."""
    return _document_images_payload(pc.get_project_config())


@app.post("/api/config/document-images")
def set_document_images_thresholds(req: DocumentImagesRequest) -> dict:
    try:
        cfg = pc.set_image_thresholds(req.min_px, req.ask_count, req.ask_pct)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _document_images_payload(cfg)


# ---------------------------------------------------------------------------
class AgentCreate(BaseModel):
    label: str = ""
    session_id: str
    automation_type: str = "command"  # 'command' | 'skill' | 'file'
    commands: list[str] = []
    skill_name: str = ""
    file_path: str = ""
    kind: str = "once"  # once | recurring
    trigger: dict = {}
    enabled: bool = True


class AgentPatch(BaseModel):
    label: str | None = None
    session_id: str | None = None
    automation_type: str | None = None
    commands: list[str] | None = None
    skill_name: str | None = None
    file_path: str | None = None
    kind: str | None = None
    trigger: dict | None = None
    enabled: bool | None = None


@app.get("/api/agents")
def list_agents() -> dict:
    """REQ-005 (spec agentes-huerfanos-y-gestion-skills): cada agente
    devuelto incluye `broken` (None si esta sano) -- validado en VIVO en
    cada listado, no solo al disparar, para que la lista de Agentes pueda
    mostrar el tag rojo correspondiente sin esperar a que el proximo
    disparo falle en silencio (ver compute_agent_broken_status)."""
    from app.persistence import master_index as mi
    from app.scheduler.agents import compute_agent_broken_status

    agents = mi.list_agents()
    for agent in agents:
        agent["broken"] = compute_agent_broken_status(agent)
    return {"agents": agents}


@app.post("/api/agents")
def create_agent(body: AgentCreate) -> dict:
    from app.persistence import master_index as mi

    _reject_invalid_trigger(body.kind, body.trigger)
    agent = mi.create_agent(
        uuid.uuid4().hex,
        body.label,
        body.session_id,
        body.automation_type,
        body.commands,
        body.skill_name,
        body.file_path,
        body.kind,
        body.trigger,
        body.enabled,
    )
    return {"agent": agent}


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentPatch) -> dict:
    from app.persistence import master_index as mi

    patch = body.model_dump(exclude_unset=True)
    current = mi.get_agent(agent_id)
    if current is not None:
        _reject_invalid_edit(current, patch)
    agent = mi.update_agent(agent_id, patch)
    if agent is None:
        raise HTTPException(status_code=404, detail="No se encontró el agente.")
    return {"agent": agent}


@app.delete("/api/agents/runs")
def clear_agent_runs_endpoint(agent_id: str | None = None) -> dict:
    """Pedido del usuario: botón "Limpiar historial viejo" -- borra las
    ejecuciones YA terminadas (nunca las 'running', para no perder rastro
    de algo en curso). Sin `agent_id`, limpia el historial de TODOS los
    agentes.

    Registrada ANTES de `DELETE /api/agents/{agent_id}` a propósito: FastAPI
    matchea rutas en orden de registro, así que si esta ruta literal
    quedara despues de la de parametro, cualquier DELETE a
    /api/agents/runs caería en delete_agent(agent_id="runs") y fallaria con
    404 'No se encontró el agente' -- exactamente el bug reportado por el
    usuario (el boton "Limpiar historial viejo" no hacia nada)."""
    from app.persistence import master_index as mi

    return {"cleared": mi.clear_finished_agent_runs(agent_id=agent_id)}


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str) -> dict:
    from app.persistence import master_index as mi

    if not mi.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="No se encontró el agente.")
    return {"deleted": True}


@app.post("/api/agents/{agent_id}/run")
async def run_agent_now_endpoint(agent_id: str) -> dict:
    """Pedido del usuario: "Ejecutar ahora" desde el panel lateral de la
    sesion o la pestaña Agentes -- dispara este agente YA, sin esperar su
    horario. Cuenta como un disparo real (marca `last_fired_at`, ver
    `run_agent_now`), asi un 'once' no se dispara DE NUEVO mas tarde.

    Bug real corregido (2026-10-21, mismo patron de AGENTS.md: "un endpoint que hace `asyncio.create_task()` debe ser `async def`"): estaba definido con
    `def` sync, asi que FastAPI lo corria en un hilo de su pool SIN event loop y `run_agent_now` -> `fire_agent` -> `_launch_sequence` -> `_launch_turn`
    -> `asyncio.create_task()` explotaba con `RuntimeError: no running event loop` (500 «Vision tuvo un problema interno» al tocar «Ejecutar ahora»).
    Con `async def` corre en el event loop, igual que el reloj de agentes (`run_agents_loop`), y no espera nada: `run_agent_now` es rapido y no bloquea."""
    from app.scheduler.agents import run_agent_now

    if not run_agent_now(agent_id):
        raise HTTPException(status_code=404, detail="No se encontró el agente.")
    return {"started": True}


@app.post("/api/agents/{agent_id}/stop")
def stop_agent_endpoint(agent_id: str) -> dict:
    """Corta la ejecucion EN CURSO de este agente (los pasos que ya
    arrancaron se dejan terminar via stop cooperativo, los que faltaban se
    cancelan) -- ver docstring de `stop_agent` para por que esto no alcanza
    con reusar el generico POST /api/chat/{id}/stop tal cual."""
    from app.scheduler.agents import stop_agent

    return {"stopped": stop_agent(agent_id)}


@app.get("/api/agents/runs")
def list_all_agent_runs(limit: int = 100, offset: int = 0) -> dict:
    from app.persistence import master_index as mi

    return {
        "runs": mi.list_agent_runs(limit=limit, offset=offset),
        "total": mi.count_agent_runs(),
    }


@app.get("/api/agents/{agent_id}/runs")
def list_agent_runs_endpoint(agent_id: str, limit: int = 50, offset: int = 0) -> dict:
    from app.persistence import master_index as mi

    return {
        "runs": mi.list_agent_runs(agent_id=agent_id, limit=limit, offset=offset),
        "total": mi.count_agent_runs(agent_id=agent_id),
    }


@app.get("/api/session/{session_id}/status")
def get_session_status(session_id: str) -> dict:
    """Indica si ESTA sesion tiene un turno corriendo en background ahora
        mismo -- el frontend lo usa para mostrar 'pensando...' al volver a una
        sesion, o para decidir si re-engancharse con GET /api/chat/stream/{id}.

        REQ-006: `exists=False` es la señal de "sesion huerfana" -- el
        frontend tiene un session_id con historial persistido en localStorage
        que este proceso backend (tras un reinicio) ya no reconoce. El
    frontend usa esto para diferenciar 'sesion realmente nueva' (nunca
        tuvo historial) de 'sesion que existia pero se perdio', y mostrar el
        aviso correspondiente solo en el segundo caso."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    return {
        "busy": session.is_busy if session else False,
        "exists": store.exists(session_id),
        # El frontend lo usa para resolver a que .turn-response-slot debe
        # engancharse al reconectarse (ver turnContainers en app.js) --
        # asi la respuesta de un turno reconectado tambien queda pegada
        # debajo de su propio mensaje, no solo la de un envio nuevo.
        "active_turn_id": session.active_turn_id if session else None,
        # persistencia-sqlite-sesiones REQ-006: el frontend usa esto para
        # decidir si mostrar el banner de "sin carpeta de proyecto -- tu
        # historial no se esta guardando". proyecto-por-sesion REQ-002:
        # se resuelve para ESTA sesion especifica (carpeta propia si tiene,
        # sino el default), no un valor global unico.
        "persistence_available": persistence_service.is_persistence_available(session_id),
        # proyecto-por-sesion REQ-006: ruta a mostrar en el chip del
        # command-bar -- carpeta propia, default sugerido, o raiz de
        # Vision como referencia visual si no hay ninguna.
        "project_path": persistence_service.resolve_project_path_for_display(session_id),
        # spec modo-plan-loop REQ-001/005: modo actual ("plan"/"active") --
        # el frontend lo usa para pintar el chip correcto sin depender de
        # un estado local que se desincronice al recargar/cambiar de sesion.
        "mode": session.mode if session else "active",
        # REQ-006/009: el frontend usa esto para saber si debe mostrar el
        # banner+alarma de "el loop necesita tu ayuda" al reconectarse.
        "loop_active": session.loop_active if session else False,
        "loop_paused_for_question": session.loop_paused_for_question if session else False,
        "loop_auto": session.loop_auto if session else False,  # REQ-011
    }


@app.get("/api/sessions")
def list_persisted_sessions_endpoint(session_id: str = "") -> dict:
    """persistencia-sqlite-sesiones REQ-004/REQ-005, extendido por
    proyecto-por-sesion REQ-002/005: lista las sesiones de la carpeta
    resuelta para `session_id` (o el default global si no se pasa),
    filtradas por el usuario de Windows ACTUAL -- ya NO 'sin ningun
    filtro de usuario' como decia la spec original (ver Decisiones
    registradas de proyecto-por-sesion). Alimenta el sidebar del frontend
    como fuente adicional al indice liviano de localStorage."""
    return {
        "sessions": persistence_service.list_persisted_sessions(session_id or None),
        "persistence_available": persistence_service.is_persistence_available(session_id or None),
    }


@app.get("/api/sessions/all")
def list_all_sessions_endpoint() -> dict:
    """Bug real reportado por el usuario: abrir Vision en OTRO navegador
    (misma maquina, mismo backend) mostraba el sidebar vacio pese a que
    las sesiones ya estaban persistidas en SQLite -- `GET /api/sessions`
    de arriba exige `session_id` con carpeta YA conocida para saber que
    `vision.db` abrir, y un navegador nuevo arranca con un session_id
    recien generado sin ninguna carpeta asignada, asi que no alcanza.

    Este endpoint NO depende de ningun `session_id` de contexto: cruza
    TODAS las carpetas de proyecto conocidas por el indice maestro
    (`<carpeta de datos>\\vision.db`) para el usuario de Windows
    ACTUAL, sin importar en cual de ellas quedo cada sesion (ver
    persistence_service.list_all_sessions_for_current_user()). Usado por
    renderSessionsPanel() en app.js para fusionar el listado real de
    SQLite con el indice liviano de localStorage (que sigue existiendo
    como cache, no como fuente de verdad)."""
    return {"sessions": persistence_service.list_all_sessions_for_current_user()}


class RenameSessionRequest(BaseModel):
    title: str


@app.patch("/api/sessions/{session_id}/rename")
def rename_session_endpoint(session_id: str, req: RenameSessionRequest) -> dict:
    """gestion-sesiones REQ-001/005/006: renombrado MANUAL de una sesion --
        marca title_manual=1 en SQLite (ver db.rename_session) para que ningun
        criterio automatico lo vuelva a pisar. REQ-005: validacion de nombre
    (no vacio, truncado a 80 chars) se hace ACA, no solo en el frontend,
        para no depender de que el cliente sea confiable."""
    _assert_session_owner(session_id)
    title = req.title.strip()
    if not title:
        raise HTTPException(400, detail="El nombre no puede estar vacío.")
    title = title[:80]
    persistence_service.rename_session(session_id, title)
    return {"renamed": True, "title": title}


class AssignProjectPathRequest(BaseModel):
    path: str
    title: str = ""
    # spec cambio-carpeta-historial REQ-003: solo con True y una carpeta anterior distinta se COPIA el historial; sin el campo todo
    # sigue exactamente como antes.
    copy_history: bool = False
    # spec importar-historial-de-carpeta REQ-004: id de una conversacion ANTERIOR que ya vive en la carpeta elegida, para COPIAR su historial a esta
    # sesion (nueva). Vacio = no importar: todo sigue exactamente como antes.
    import_from_session_id: str = ""
    # spec sesion-publica-privada REQ-001/008: al CREAR la sesion se puede dejar publica. Sin el campo (o False) queda privada, como siempre.
    public: bool = False


@app.post("/api/sessions/{session_id}/project-path")
def assign_session_project_path_endpoint(session_id: str, req: AssignProjectPathRequest) -> dict:
    """Arquitectura carpeta-por-sesion (definitiva, ver docstring largo en
        app/persistence/service.py): asigna la carpeta de proyecto de UNA
        sesion especifica -- llamado desde el modal BLOQUEANTE de "Nueva
        sesión" (nombre + carpeta obligatorios antes de poder chatear) y
        tambien reusable para cambiarla despues desde el modal de edicion del
        chip de sesion. Crea la estructura fija de subcarpetas
        (skills/sessions/decisions/commands) dentro de la carpeta al
    asignarla (ver ensure_project_structure)."""
    _assert_session_owner(session_id)
    path = req.path.strip()
    if not path:
        raise HTTPException(400, detail="La ruta no puede estar vacía.")
    # REQ-016 (spec fix-detener-y-progreso-visible, revision): lo que la sesion ya hablo sin carpeta (solo en memoria) se guarda
    # en SQLite al asignarle la primera carpeta (ver `assign_project_path`); sin esto, F5 lo perdia.
    session = get_session_store().get(session_id)
    unsaved_history = _unsaved_history_with_page_ids(session) if session is not None else None
    copied = 0
    imported = 0
    import_failed = False
    try:
        if req.import_from_session_id:
            # spec importar-historial-de-carpeta REQ-004/006/008: la sesion es NUEVA y la carpeta ya tiene conversaciones anteriores del usuario;
            # se COPIA el historial de una de ellas (la de origen no se toca). Excluyente con `copy_history` (que es para una sesion que cambia
            # de carpeta). Un origen inexistente/ajeno o una sesion que ya hablo es un 400 claro y NO asigna nada; si la copia falla por otra
            # causa, la carpeta se asigna igual y la respuesta lo dice (`import_failed`).
            try:
                persistence_service.preflight_import(session_id, path, req.import_from_session_id)
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
            persistence_service.assign_project_path(session_id, path, title=req.title, unsaved_history=unsaved_history)
            try:
                imported = persistence_service.import_session_history(session_id, path, req.import_from_session_id)
                _reload_session_memory(session_id)
            except Exception:
                import_failed = True
                logger.warning(
                    "No se pudo importar el historial de %s a la sesion %s (carpeta %s)",
                    req.import_from_session_id,
                    session_id,
                    path,
                    exc_info=True,
                )
        elif req.copy_history:
            # spec cambio-carpeta-historial REQ-003: la persona eligio copiar el historial al cambiar de carpeta. Si NO habia carpeta
            # anterior (primera asignacion) esto cae en el camino de siempre, con lo hablado sin carpeta.
            if persistence_service.master_index_has_folder(session_id):
                copied = persistence_service.change_folder_with_history(session_id, path, req.title)
            else:
                persistence_service.assign_project_path(
                    session_id, path, title=req.title, unsaved_history=unsaved_history
                )
        else:
            persistence_service.assign_project_path(session_id, path, title=req.title, unsaved_history=unsaved_history)
    except FileNotFoundError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if req.public:
        # spec sesion-publica-privada REQ-001: nace publica (sin dueño en la base). El indice maestro de esta PC no cambia: sigue siendo mia.
        try:
            persistence_service.set_session_visibility(session_id, True)
        except Exception as exc:
            logger.warning("No se pudo dejar publica la sesion %s", session_id, exc_info=True)
            raise HTTPException(
                500,
                detail="La carpeta se asignó, pero no pude dejar la sesión como pública. Probá de nuevo desde Editar sesión.",
            ) from exc
    response: dict = {"assigned": True, "path": path}
    if req.copy_history and not req.import_from_session_id:
        # Aditivo: `copied_messages` aparece SOLO si se pidio copiar. Sin `copy_history` la respuesta es EXACTAMENTE la de siempre
        # (un test existente y cualquier cliente que compare el cuerpo completo dependen de eso).
        response["copied_messages"] = copied
    if req.import_from_session_id:
        response["imported_messages"] = imported
        if import_failed:
            response["import_failed"] = True
    return response


def _reload_session_memory(session_id: str) -> None:
    """spec importar-historial-de-carpeta REQ-007: tras importar, la sesion en memoria pasa a tener el historial importado, para que el proximo
    mensaje ya parta con todo el contexto anterior. NO toca la memoria si hay un turno corriendo (se pisaria lo que ese turno esta armando) ni
    si la sesion no esta en memoria (se cargara de SQLite cuando alguien la use: `get_or_create`). No incluye el mensaje de sistema: el loop lo
    agrega solo, y solo si falta."""
    session = get_session_store().get(session_id)
    if session is None:
        return
    task = session.current_task
    if task is not None and not task.done():
        return
    session.messages = persistence_service.load_session_messages(session_id)
    session.todos = persistence_service.load_session_todos(session_id)


class VisibilityRequest(BaseModel):
    public: bool


@app.get("/api/sessions/{session_id}/visibility")
def get_session_visibility_endpoint(session_id: str) -> dict:
    """spec sesion-publica-privada REQ-007: si la sesion esta marcada publica. Sin carpeta asignada (o sin fila todavia) es privada."""
    _assert_session_owner(session_id)
    return {"public": persistence_service.get_session_visibility(session_id)}


@app.post("/api/sessions/{session_id}/visibility")
def set_session_visibility_endpoint(session_id: str, req: VisibilityRequest) -> dict:
    """spec sesion-publica-privada REQ-007: marca la sesion como publica o privada. Solo la dueña (403 si no). Publica = sin dueño en la base
    de la carpeta, para que otra persona pueda importar una COPIA; privada = el dueño vuelve a ser quien la marca. Sin carpeta asignada no hay
    donde guardarlo: 400 con una frase."""
    _assert_session_owner(session_id)
    try:
        public = persistence_service.set_session_visibility(session_id, req.public)
    except LookupError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"public": public}


@app.get("/api/sessions/{session_id}/folder-sessions")
def folder_sessions_endpoint(session_id: str, path: str = "") -> dict:
    """spec importar-historial-de-carpeta REQ-001: consultado por el modal de edicion y por el de «Nueva sesion» ANTES de guardar una carpeta,
    para avisar si ya tiene conversaciones anteriores del usuario y ofrecer importarlas o abrirlas. Solo lectura."""
    _assert_session_owner(session_id)
    folder = path.strip()
    if not folder:
        raise HTTPException(400, detail="Falta indicar la carpeta.")
    return persistence_service.folder_sessions(session_id, folder)


@app.get("/api/sessions/{session_id}/folder-change-preview")
def folder_change_preview_endpoint(session_id: str, path: str = "") -> dict:
    """spec cambio-carpeta-historial REQ-001: consultado por el modal de edicion ANTES de guardar una carpeta distinta, para saber si
    corresponde preguntar «¿copiar el historial?». Solo lectura (no crea nada ni escribe en ninguna base)."""
    _assert_session_owner(session_id)
    new_path = path.strip()
    if not new_path:
        raise HTTPException(400, detail="Falta indicar la carpeta nueva.")
    return persistence_service.folder_change_preview(session_id, new_path)


def _unsaved_history_with_page_ids(session) -> list[dict]:
    """REQ-016 (spec fix-detener-y-progreso-visible, revision): copia de `session.messages` (sin el system prompt) donde cada mensaje
    de usuario que se puede atribuir CON CERTEZA a un turno lleva `message_id` = el id con que la pagina lo mando (el del turno: F5,
    ↻ y 🗑 lo buscan por ese id) y `text` = lo que escribio la persona (sin las instrucciones que Vision le agrega al modelo, igual
    que lo que guarda el turno en vivo). Se atribuye con el mismo criterio que editar/borrar (`_locate_closed_user_message`): el
    indice del turno tiene que seguir apuntando a un mensaje de usuario que empiece con su texto; si no (indice corrido por un
    borrado o una compactacion), el mensaje se guarda igual, con un id nuevo. Si dos turnos apuntan al mismo, gana el mas reciente."""
    msgs = list(session.messages)
    turn_at: dict[int, object] = {}
    for turn in list(session.turns.values()):
        index = turn.messages_start_index
        if index is None or not 0 <= index < len(msgs) or msgs[index].get("role") != "user":
            continue
        text, content_text = turn.text or "", _user_content_text(msgs[index].get("content"))
        # Un turno sin texto (solo imagenes) solo se atribuye a un mensaje sin texto: «empieza con ''» no prueba nada.
        if content_text.startswith(text) if text else not content_text:
            turn_at[index] = turn
    history: list[dict] = []
    for index, message in enumerate(msgs):
        if message.get("role") == "system":
            continue
        item = dict(message)
        turn = turn_at.get(index)
        if turn is not None:
            item["message_id"] = turn.id
            item["text"] = turn.text
        history.append(item)
    return history


@app.get("/api/sessions/search")
def search_sessions_endpoint(q: str = "", session_id: str = "") -> dict:
    """busqueda-mensajes REQ-002/005: busqueda global case-insensitive por
        substring simple sobre el texto de las sesiones de la carpeta resuelta
        (proyecto-por-sesion REQ-002), filtrada por usuario (REQ-005). `[]` si
        no hay persistencia disponible -- el frontend cae a buscar en
    localStorage en ese caso (REQ-006 de esa spec)."""
    term = q.strip()
    if session_id:
        _assert_session_owner(session_id)
    # REQ-007 (busqueda-mensajes): la busqueda es GLOBAL -- recorre todas las carpetas de proyecto
    # del usuario y NO depende de que la sesion abierta tenga carpeta (una "Nueva sesion" todavia
    # no la tiene, y aun asi el usuario quiere encontrar lo que dijo en las otras). Bug real
    # ("no me aparece la barra"): con la sesion abierta sin carpeta se respondia vacio SIN buscar.
    # Solo si NO hay ninguna carpeta conocida queda el modo degradado de REQ-006 (el frontend
    # busca en localStorage).
    available = bool(persistence_service.known_project_folders()) or (
        persistence_service.is_persistence_available(session_id or None)
    )
    if not term or not available:
        return {"results": [], "persistence_available": available}
    return {
        "results": persistence_service.search_all_sessions_for_current_user(term),
        "persistence_available": True,
    }


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages_endpoint(session_id: str) -> dict:
    """Bug real reportado por el usuario: activar una sesion existente (ej.
    elegida desde el listado de GET /api/sessions/all en un navegador
    nuevo) no traia su historial real -- activateSession() en app.js solo
    leia `chatHistory` de localStorage, que en un navegador distinto esta
    vacio aunque la sesion SI tenga mensajes persistidos en su propio
    `vision.db`. Devuelve `[]` (no error) si la sesion no tiene carpeta de
    proyecto asignada o simplemente no tiene mensajes todavia -- ambos son
    casos normales, no fallas.

    REQ-018 (spec fix-detener-y-progreso-visible): `pending_question` = la pregunta (tool `question`) que esta sesion dejo sin
    contestar, o null -- para que F5 la vuelva a mostrar en SU sesion (ver `_pending_question_for_page`)."""
    _assert_session_owner(session_id)
    return {
        "messages": persistence_service.load_session_messages_for_display(session_id),
        "pending_question": _pending_question_for_page(session_id),
    }


def _pending_question_for_page(session_id: str) -> dict | None:
    """REQ-018 (spec fix-detener-y-progreso-visible): {question, options} de la ultima pregunta sin contestar de la sesion, segun su
    SQLite (la pagina guarda en memoria las que llegan en vivo; con F5 las pierde). None si no hay, si ya la contesto el loop solo, o si
    la sesion tiene mensajes esperando en la cola: el usuario ya toco una opcion y su respuesta todavia no arranco (por eso no esta en
    SQLite), y volver a mostrarla invitaria a contestar dos veces."""
    from app.tools.question import pending_question_view

    session = get_session_store().get(session_id)
    if session is not None and session.queue_order:
        return None
    part = persistence_service.load_pending_question(session_id)
    return pending_question_view(part) if part else None


# REQ-004 (spec controles-seguridad-optimizacion-lightspeed-bigquery): sirve
# los CSV completos que `app/tools/dbquery.py` exporta cuando el resultado
# de una consulta (Oracle/BigQuery/SharePoint/SAP) supera el umbral de filas
# configurado -- es lo que hace que el link Markdown que el LLM devuelve en
# el chat sea CLICKEABLE de verdad desde el navegador (antes de esto, no
# existia ningun endpoint que sirviera archivos de la carpeta de proyecto
# REAL de una sesion -- /browser/{id}/... es una carpeta TEMPORAL distinta,
# ver browser_workspace.py).
#
# Seguridad (ver skill secure-code-review, patron BOLA/IDOR + path
# traversal): (1) `_assert_session_owner` verifica que quien pide el archivo
# sea el dueño de la sesion, igual que el resto de los endpoints /sessions/*;
# (2) `filename` se resuelve DENTRO de `exports/` de esa sesion puntual y se
# verifica con `Path.resolve()` + `relative_to()` que el resultado siga
# ADENTRO de esa carpeta -- rechaza cualquier intento de escapar con `..`,
# rutas absolutas, o symlinks que apunten afuera. Nunca sirve una ruta
# arbitraria del disco.
@app.get("/api/sessions/{session_id}/exports/{filename}")
def download_session_export(session_id: str, filename: str):
    """Descarga un CSV exportado por REQ-004 dentro de `exports/` de esta
    sesion. 404 tanto si la sesion no tiene carpeta de proyecto asignada
    como si el archivo no existe DENTRO de esa carpeta -- nunca se filtra
    informacion sobre rutas fuera de ese directorio."""
    _assert_session_owner(session_id)
    from fastapi.responses import FileResponse

    project_root = persistence_service.project_root_for_session(session_id)
    if project_root is None:
        raise HTTPException(status_code=404, detail="Esta sesión no tiene carpeta de proyecto asignada.")

    exports_dir = (project_root / persistence_service.EXPORTS_SUBDIR).resolve()
    # `filename` nunca deberia traer separadores de path (es el nombre de
    # archivo devuelto por _write_export_csv, sin subcarpetas) -- se
    # normaliza igual antes de resolver, en vez de confiar en el valor que
    # llega por URL.
    candidate = (exports_dir / Path(filename).name).resolve()
    try:
        candidate.relative_to(exports_dir)
    except ValueError:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.") from None
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    # Bug real reportado por el usuario (2026-09-21): "el enlace no deberia
    # descargarmelo de nuevo, deberia abrirme el archivo sin descargar".
    # `FileResponse` de Starlette pone `Content-Disposition: attachment`
    # por defecto (fuerza el dialogo de "Guardar como" en el navegador) --
    # se pasa `content_disposition_type="inline"` para que el navegador
    # intente ABRIR el archivo en una pestaña/visor en vez de descargarlo
    # (comportamiento real depende del navegador/su config para .csv, pero
    # "inline" es la señal HTTP correcta para pedir esto -- "attachment"
    # la fuerza a descargar SIEMPRE, sin importar la configuracion del
    # navegador).
    return FileResponse(
        candidate,
        media_type="text/csv",
        filename=candidate.name,
        content_disposition_type="inline",
    )


@app.get("/api/session/{session_id}/agent-impact")
def get_session_agent_impact(session_id: str) -> dict:
    """REQ-002/003/004 (spec agentes-huerfanos-y-gestion-skills): consultado
    por el frontend ANTES de mostrar el modal de confirmacion de borrado, para
    enriquecerlo con cuantos agentes se veran afectados y que va a pasar con
    ellos -- sin esto el usuario no tiene forma de saber, antes de confirmar,
    que borrar una sesion puede romper una automatizacion diaria en uso.

    `is_fallback_blocked`: True si esta sesion ES la reserva configurada Y
    tiene agentes dependiendo de ella AHORA (REQ-004) -- el frontend debe
    impedir el borrado y explicar por que (nunca se bloquea solo por "ser
    la reserva" en abstracto, solo cuando de verdad tiene dependientes)."""
    from app.persistence import master_index as mi
    from app.scheduler.agents import get_fallback_session_id

    count = mi.count_agents_by_session(session_id)
    fallback_id = get_fallback_session_id()
    is_fallback = bool(fallback_id) and fallback_id == session_id
    # spec indice-resiliente-y-borrado-seguro REQ-010: campos ADITIVOS para que el modal diga cuantos mensajes se van a borrar,
    # de que carpeta y si el mismo id existe en otra. De mejor esfuerzo: nunca rompe el chequeo de agentes de arriba.
    try:
        deletion = persistence_service.deletion_info(session_id)
    except Exception:
        logger.warning("No se pudo armar el resumen de borrado de la sesion %s", session_id, exc_info=True)
        deletion = {"message_count": 0, "project_path": "", "copies_elsewhere": []}
    return {
        **deletion,
        "agent_count": count,
        "is_fallback_session": is_fallback,
        "is_fallback_blocked": is_fallback and count > 0,
        "fallback_session_id": fallback_id,
        "will_reassign_to_fallback": count > 0 and bool(fallback_id) and fallback_id != session_id,
    }


@app.delete("/api/session/{session_id}")
def delete_session_endpoint(session_id: str) -> dict:
    """Elimina una sesion (icono de basura del frontend): cancela su turno
    en curso si lo hubiera, borra su estado en memoria, borra su carpeta
        de vista previa en vivo (browser/{session_id}/, temporal por diseno),
        y su registro persistido en SQLite si existe (REQ-004).

    REQ-002/003/004 (spec agentes-huerfanos-y-gestion-skills): antes de
    borrar, resuelve que pasa con los agentes que tenian esta sesion como
    destino --
      - REQ-004: si esta sesion ES la reserva configurada y tiene agentes
        dependiendo de ella AHORA, el borrado se BLOQUEA (409) -- evita el
        efecto cascada de que los ya reasignados vuelvan a quedar
        huerfanos. El usuario debe reasignar esos agentes o cambiar la
        reserva primero (decision explicita del usuario, ver spec.md).
      - REQ-002: si hay una reserva configurada (valida, distinta de esta
        sesion), los agentes se reasignan EN BLOQUE antes de borrar.
      - REQ-003: sin reserva (o reserva invalida/ya borrada), los agentes
        quedan con el `session_id` viejo -- REQ-005 los marca `broken` al
        listar."""
    from app.persistence import master_index as mi
    from app.scheduler.agents import get_fallback_session_id

    _assert_session_owner(session_id)

    agent_count = mi.count_agents_by_session(session_id)
    fallback_id = get_fallback_session_id()
    if agent_count > 0 and fallback_id and fallback_id == session_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Esta sesión es tu sesión de reserva de agentes y tiene {agent_count} "
                "agente(s) asignados. Reasigná esos agentes o cambiá la sesión de reserva "
                "en Configuración > Agentes antes de poder eliminarla."
            ),
        )

    reassigned = 0
    if agent_count > 0 and fallback_id and fallback_id != session_id and mi.session_index_exists(fallback_id):
        reassigned = mi.reassign_agents_session(session_id, fallback_id)

    store = get_session_store()
    existed = store.delete(session_id)
    browser_deleted = browser_workspace.delete_browser_dir(session_id)
    persistence_service.delete_session(session_id)
    return {
        "deleted": existed,
        "browser_deleted": browser_deleted,
        "agents_reassigned": reassigned,
        "agents_orphaned": max(0, agent_count - reassigned),
    }


# ---------------------------------------------------------------------------
# Contexto/tokens de sesion (medidor del sidebar, ver app/token_estimate.py)
# ---------------------------------------------------------------------------
def _valid_context_window(value: object) -> int | None:
    """Ventana de contexto como entero positivo, o None si falta o es basura (`bool` incluido: True no es 1 token)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value > 0 else None


def resolve_context_window(model_id: str) -> int:
    """Ventana de contexto REAL del modelo (por id canonico o por alias), o 200.000 si no se conoce.

    El catalogo guarda la MISMA entrada bajo el id y bajo cada alias (ver `list_models`), asi que se busca
    por clave: antes se comparaba contra `entry["id"]` y un alias como `claude-sonnet-latest` (el modelo por
    defecto) nunca coincidia. Tambien la usa la compactacion del historial (85 % de esta ventana)."""
    fallback = 200_000
    entry = (_MODELS_CACHE.get("by_id") or {}).get(model_id)
    return _valid_context_window(entry.get("context_window")) or fallback if entry else fallback


def _context_level(pct: int | float) -> str:
    """spec aviso-contexto-largo REQ-007: semaforo del uso de contexto. ok < 60, warn 60-74, high 75-89, critical >= 90."""
    if pct >= 90:
        return "critical"
    if pct >= 75:
        return "high"
    if pct >= 60:
        return "warn"
    return "ok"


@app.get("/api/context/{session_id}")
def get_context_usage(session_id: str, model: str | None = None) -> dict:
    """Uso estimado del contexto. `model` (opcional, spec aviso-contexto-largo REQ-007) mide contra la ventana del modelo elegido en el
    selector; sin el, contra el modelo por defecto (`default_model_id()`) como siempre. Los campos de siempre no cambian; `level` es aditivo."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_restore(session_id)  # persistencia-sqlite-sesiones REQ-003: tambien tras un reinicio
    if session is None:
        return {"tokens": 0, "context_window": 0, "pct_used": 0, "estimated": True, "level": "ok"}

    tokens = estimate_tokens(session.messages)
    context_window = resolve_context_window((model or "").strip() or default_model_id())

    pct = min(100, round((tokens / context_window) * 100)) if context_window else 0
    return {
        "tokens": tokens,
        "context_window": context_window,
        "pct_used": pct,
        "estimated": True,
        "level": _context_level(pct),
    }


# ---------------------------------------------------------------------------
# Estado de conectores (panel lateral, reemplazo funcional de "LSP")
# ---------------------------------------------------------------------------
@app.get("/api/connectors/status")
async def connectors_status() -> dict:
    return {"connectors": await check_all_connectors()}


@app.get("/api/connections")
def list_named_connections() -> dict:
    """spec conexiones-con-nombre REQ-002/004: las conexiones con nombre que hay (Oracle - Datamart, SharePoint - Sitio Logistico, ...).

    Solo tipo, nombre, estado de configuracion y NOMBRES de los campos que faltan: nunca un valor (ni contrasenas, ni hosts, ni tokens). `error` es el
    ultimo problema al leer `connections.json` (None si esta bien); un archivo roto conserva la ultima lectura valida (REQ-011)."""
    return {
        "connections": connections.describe_all(),
        "types": [
            {"type": t.type_id, "label": t.label, "testable": t.tester is not None}
            for t in connections.get_types().values()
        ],
        "error": connections.last_error(),
        "file": connections.display_path(),  # `~/.vision/connections.json`: sin el nombre de usuario de Windows
    }


def _confluence_for_request(connection: str | None):
    """(conector, conexion) de Confluence para un endpoint. La conexion se elige SOLO por nombre contra el registro; 404 con los nombres validos si no existe."""
    settings = get_settings()
    if not getattr(settings, "enable_confluence", True):
        raise HTTPException(status_code=400, detail="Confluence está desactivado en esta instalación de ULTRON.")
    try:
        conn = switches.resolve_enabled("confluence", connection or None)
    except switches.DisabledConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except connections.ConnectionLookupError as exc:
        if connection:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ConfluenceConnector(settings), None
    effective = connections.settings_for(conn) if conn.from_file else settings
    return ConfluenceConnector(effective), conn


def _confluence_http_error(exc: BaseException, conn) -> HTTPException:
    """Error de Confluence para la pantalla. El texto sale enmascarado (token, correo y valores de la conexion) y el LOG solo lleva el tipo de la
    excepcion: `_connector_error_detail` registra `exc` crudo, y un fallo de red puede traer credenciales en su mensaje."""
    logger.warning("Fallo de Confluence (%s)", type(exc).__name__)
    detail = connections.redact(conn, str(exc)) if conn is not None else str(exc)
    return HTTPException(status_code=502, detail=f"No pude consultar Confluence. {detail}")


@app.get("/api/confluence/tree")
async def confluence_tree(
    connection: str | None = None, section: str | None = None, ref: str | None = None, depth: int = 0
) -> dict:
    """Raiz de una seccion (o de `ref`) con `depth` niveles (0 = solo la raiz; los hijos se piden con /children al expandir)."""
    connector, conn = _confluence_for_request(connection)
    root = (ref or "").strip()
    if not root:
        sections = connections.parse_confluence_sections(conn) if conn is not None else []
        wanted = (section or "").strip().lower()
        match = (
            next((s for s in sections if s["name"].lower() == wanted), None)
            if wanted
            else (sections[0] if sections else None)
        )
        if match is None:
            raise HTTPException(status_code=404, detail="Esa seccion no existe en la conexion de Confluence.")
        root = match["page_id"]
    try:
        return await asyncio.to_thread(connector.get_page_tree, root, max(0, min(depth, 3)), 300)
    except Exception as exc:  # noqa: BLE001
        raise _confluence_http_error(exc, conn) from exc


@app.get("/api/confluence/children")
async def confluence_children(id: str, connection: str | None = None) -> dict:
    """Hijos directos de una pagina (expansion perezosa del arbol). `id` tiene que ser el numero de la pagina."""
    if not id.isdigit():
        raise HTTPException(status_code=400, detail="El id de la página debe ser numérico.")
    connector, conn = _confluence_for_request(connection)
    try:
        return {"children": await asyncio.to_thread(connector.list_child_pages, id)}
    except Exception as exc:  # noqa: BLE001
        raise _confluence_http_error(exc, conn) from exc


@app.get("/api/confluence/page")
async def confluence_page(ref: str, connection: str | None = None) -> dict:
    """Contenido de una pagina como TEXTO PLANO (nunca HTML: la pantalla lo pinta con textContent, asi no hay XSS desde una pagina del wiki)."""
    connector, conn = _confluence_for_request(connection)
    try:
        page = await asyncio.to_thread(connector.get_page, ref)
    except Exception as exc:  # noqa: BLE001
        raise _confluence_http_error(exc, conn) from exc
    return {
        "id": page["id"],
        "title": page["title"],
        "url": page["url"],
        "space": page["space_key"],
        "text": page["text"][:60_000],
    }


class SwitchRequest(BaseModel):
    """Prende o apaga UNA conexion o skill. `key`/`name` se validan contra lo REAL configurado: no se guarda una cadena arbitraria."""

    key: str | None = None  # conexiones: `<tipo>:<nombre en minuscula>`
    name: str | None = None  # skills: nombre exacto
    enabled: bool


@app.get("/api/switches/connections")
def list_connection_switches() -> dict:
    """spec interruptores-conexiones-skills REQ-006/008: TODAS las conexiones configuradas (activas y desactivadas) con su interruptor. Solo tipo, nombre y estado:
    nunca campos ni credenciales (REQ-011)."""
    return {"connections": switches.describe_switches()}


@app.post("/api/switches/connections")
def set_connection_switch(req: SwitchRequest) -> dict:
    """Prende o apaga una conexion. Persiste en `~/.vision/vision.db` (global, no por sesion). 404 si la clave no es de una conexion real; 409 si el `.env` la apaga."""
    key = (req.key or "").strip()
    info = switches.all_connection_keys().get(key)
    if info is None:
        raise HTTPException(status_code=404, detail="Esa conexión no existe.")
    if req.enabled and not switches._env_allows(info["type"]):
        raise HTTPException(
            status_code=409,
            detail="Esta conexión está desactivada en el .env (ENABLE_*). Activala allá para poder prenderla desde la pantalla.",
        )
    switches.set_connection_enabled(key, req.enabled)
    return {"connections": switches.describe_switches()}


def _known_skill_names(session_id: str | None) -> set[str]:
    """Nombres de skills REALES: las globales y, si se indica una sesion propia, las locales de esa sesion."""
    from app.tools.skill import discover_local_skills, discover_skills

    names = {s["name"] for s in discover_skills()}
    if session_id:
        _assert_session_owner(session_id)
        names |= {s["name"] for s in discover_local_skills(session_id)}
    return names


@app.get("/api/switches/skills")
def list_skill_switches(session_id: str | None = None) -> dict:
    """Skills (globales y, con `session_id`, las locales de esa sesion) con su interruptor individual. `global_enabled` es el interruptor GENERAL de las globales, que se
    mantiene y manda por encima. Solo nombre, origen y estado: nunca el contenido de la skill."""
    from app.persistence import master_index
    from app.tools.skill import SKILLS_GLOBAL_ENABLED_KEY, discover_local_skills, discover_skills

    off = switches.disabled_skill_names()
    rows = [{"name": s["name"], "scope": "global", "enabled": s["name"] not in off} for s in discover_skills()]
    if session_id:
        _assert_session_owner(session_id)
        rows += [
            {"name": s["name"], "scope": "local", "enabled": s["name"] not in off}
            for s in discover_local_skills(session_id)
        ]
    return {
        "global_enabled": master_index.get_app_setting(SKILLS_GLOBAL_ENABLED_KEY) == "true",
        "skills": sorted(rows, key=lambda r: (r["scope"], r["name"].lower())),
    }


@app.post("/api/switches/skills")
def set_skill_switch(req: SwitchRequest, session_id: str | None = None) -> dict:
    """Prende o apaga UNA skill por su nombre exacto (global o local de la sesion). Persiste en `~/.vision/vision.db`. 404 si no es una skill real."""
    name = (req.name or "").strip()
    try:
        switches.set_skill_enabled(name, req.enabled, _known_skill_names(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Esa skill no existe.") from exc
    return list_skill_switches(session_id)


# ---------------------------------------------------------------------------------------------------------------------
# spec gestor-conexiones A7: API del almacen cifrado (`/api/vault/*`). TODO pedido pasa por `_vault_guard` (esta PC + la propia pagina de Vision, sin interruptor
# que lo apague: REQ-007b); ver o cambiar un secreto YA guardado exige ademas el desbloqueo (Windows Hello, B3). Ninguna respuesta lleva un secreto salvo las de
# `/secret/`, que solo responden desbloqueado. Los errores son para personas (`ConfigStoreError` ya trae el texto).
# ---------------------------------------------------------------------------------------------------------------------
_NO_STORE = {"Cache-Control": "no-store"}


def _vault_guard(request: Request) -> None:
    from app import secret_access  # import diferido: modulo chico, solo lo usan estas rutas

    problem = secret_access.local_page_problem(
        request.client.host if request.client else None,
        request.url.hostname,
        request.headers.get("origin"),
        request.headers.get("sec-fetch-site"),
        f"{request.url.scheme}://{request.url.netloc}",
    )
    if problem:
        raise HTTPException(status_code=403, detail=problem)


def _vault_require_unlocked() -> None:
    from app import secret_access  # import diferido: modulo chico, solo lo usan estas rutas

    if not secret_access.is_unlocked():
        raise HTTPException(
            status_code=423, detail="Para ver o cambiar una contraseña guardada, desbloqueá con Windows Hello."
        )


def _vault_store_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _vault_require_imported() -> None:
    """La PRIMERA escritura en la base la activa como unica fuente. Si todavia hay archivos viejos sin importar, activarla con una sola conexion haria desaparecer de
    golpe todas las del `.env`: hasta importar, no se escribe nada (409). Una instalacion sin archivos viejos puede cargar directo."""
    from app import config_import, config_store  # import diferido: modulos del gestor, solo los usan estas rutas

    if not config_store.is_active() and any(config_import.legacy_sources().values()):
        raise HTTPException(
            status_code=409,
            detail="Primero importá lo que tenés en el .env (botón «Importar a ULTRON»): si no, tus conexiones de ahora dejarían de verse.",
        )


def _vault_secret_keys(type_id: str) -> set[str]:
    ctype = connections.get_types()[type_id]
    return {f.key for f in (*ctype.fields, *ctype.personal_fields) if f.secret}


def _vault_entry(stored: Any) -> dict:
    """Una tarjeta (REQ-005): datos NO secretos a la vista, de los secretos solo si estan cargados. Nunca un secreto."""
    ctype = connections.get_types().get(stored.type_id)
    conn = connections.Connection(
        stored.type_id, stored.name, stored.default, True, dict(stored.fields), "vision", stored.undecryptable
    )
    secret_keys = _vault_secret_keys(stored.type_id) if ctype else set()
    all_fields = (*ctype.fields, *ctype.personal_fields) if ctype else ()
    entry = {
        "id": stored.id,
        "type": stored.type_id,
        "type_label": ctype.label if ctype else stored.type_id,
        "name": stored.name,
        "display": conn.display,
        "default": stored.default,
        "created_at": stored.created_at,
        "updated_at": stored.updated_at,
        "tested_at": stored.tested_at,
        "test_status": stored.test_status,
        "unreadable": stored.undecryptable,
        "missing": connections.missing_fields(conn) if ctype else [],
        "enabled": switches.is_connection_enabled(stored.type_id, stored.name),
        "fields": {
            f.key: stored.fields.get(f.key, "") for f in all_fields if f.key not in secret_keys and not f.structured
        },
        "secrets": {key: bool(stored.fields.get(key)) for key in sorted(secret_keys)},
    }
    if ctype is not None and ctype.public_info is not None:
        info = ctype.public_info(conn)
        if info:
            entry["info"] = info  # solo datos NO secretos (ej. secciones de Confluence)
    return entry


@app.get("/api/vault/status")
def vault_status(request: Request) -> dict:
    """Estado del almacen: si es la fuente activa, que archivos viejos hay (solo si existen), el esquema de cada tipo y si esta desbloqueado. Sin valores."""
    from app import (
        config_import,
        config_store,
        secret_access,
    )  # import diferido: modulos del gestor, solo los usan estas rutas

    _vault_guard(request)
    types = []
    for ctype in connections.get_types().values():
        types.append(
            {
                "type": ctype.type_id,
                "label": ctype.label,
                "derived": ctype.derived_from is not None,
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "secret": f.secret,
                        "required": f.required,
                        "structured": f.structured,
                    }
                    for f in (*ctype.fields, *ctype.personal_fields)
                ],
            }
        )
    return {
        "active": config_store.is_active(),
        "legacy": config_import.legacy_sources(),
        "types": types,
        "unlocked_until": secret_access.unlocked_until(),
    }


@app.post("/api/vault/import")
def vault_import(request: Request) -> dict:
    """REQ-003: importa una vez el .env, connections.json y .env.enc. Informe por NOMBRE, nunca con valores. No toca los archivos."""
    from app import config_import  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    return config_import.import_legacy().as_dict()


@app.delete("/api/vault/legacy-files")
def vault_delete_legacy_files(request: Request) -> dict:
    """REQ-011 (Fase C): borra el .env, connections.json y .env.enc ya importados (la pantalla pide confirmacion antes). Solo con la base activa: borrarlos antes de
    importar perderia esas conexiones (409). Devuelve los nombres borrados."""
    from app import config_import, config_store  # import diferido: modulos del gestor, solo los usan estas rutas

    _vault_guard(request)
    if not config_store.is_active():
        raise HTTPException(
            status_code=409,
            detail="Primero importá tus archivos a ULTRON: si los borrás antes, perdés esas conexiones.",
        )
    return config_import.delete_legacy_files()


@app.get("/api/vault/connections")
def vault_list_connections(request: Request) -> dict:
    """Las tarjetas (REQ-005). Sin secretos (REQ-010)."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    return {"connections": [_vault_entry(c) for c in config_store.list_stored()]}


class VaultConnectionRequest(BaseModel):
    """Alta o edicion de una conexion. En la edicion, `fields` es PARCIAL (lo que no viene queda igual; null borra el campo)."""

    type: str | None = None
    name: str | None = None
    fields: dict[str, str | None] | None = None
    default: bool | None = None


@app.post("/api/vault/connections", status_code=201)
def vault_add_connection(req: VaultConnectionRequest, request: Request) -> dict:
    """REQ-004/004b: alta. Sin nombre valido, 422 y no se guarda nada."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    _vault_require_imported()
    try:
        stored = config_store.add_connection(
            str(req.type or ""),
            req.name,
            {k: v for k, v in (req.fields or {}).items() if v is not None},
            default=bool(req.default),
        )
    except config_store.ConfigStoreError as exc:
        raise _vault_store_error(exc) from exc
    return _vault_entry(stored)


@app.put("/api/vault/connections/{conn_id}")
def vault_update_connection(conn_id: str, req: VaultConnectionRequest, request: Request) -> dict:
    """REQ-006: edicion. Cambiar un secreto que YA estaba guardado exige el desbloqueo (REQ-007b); cargarlo por primera vez, no."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    try:
        current = config_store.get_stored(conn_id)
    except config_store.ConfigStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        if req.fields and not current.undecryptable:
            touched = {k for k in req.fields if k in _vault_secret_keys(current.type_id) and current.fields.get(k)}
            if touched:
                _vault_require_unlocked()
        stored = config_store.update_connection(conn_id, name=req.name, fields=req.fields, default=req.default)
    except config_store.ConfigStoreError as exc:
        raise _vault_store_error(exc) from exc
    return _vault_entry(stored)


@app.delete("/api/vault/connections/{conn_id}")
def vault_delete_connection(conn_id: str, request: Request) -> dict:
    """REQ-009: baja (la pantalla pide confirmacion antes). Tambien se olvida su interruptor."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    try:
        gone = config_store.delete_connection(conn_id)
    except config_store.ConfigStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    switches.forget_connection(switches.connection_key(gone.type_id, gone.name))
    return {"deleted": gone.id}


@app.post("/api/vault/connections/{conn_id}/test")
async def vault_test_connection(conn_id: str, request: Request) -> dict:
    """«Probar» de la tarjeta: misma prueba que el resto de Vision; el resultado queda en la tarjeta (insignia y fecha)."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    try:
        stored = config_store.get_stored(conn_id)
    except config_store.ConfigStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = await test_one_connection(stored.type_id, stored.name)
    config_store.record_test(conn_id, bool(result.get("ok")))
    return result


class VaultTestDraftRequest(BaseModel):
    """«Probar» antes de guardar: el formulario tal como esta. `conn_id` al editar (lo que no viene sale de la guardada)."""

    type: str
    name: str | None = None
    fields: dict[str, str | None] = {}
    conn_id: str | None = None


@app.post("/api/vault/connections/test-draft")
async def vault_test_draft(req: VaultTestDraftRequest, request: Request) -> dict:
    """Pedido del usuario 2026-10-10 («todos deberian tener un boton de probar antes de guardar»): prueba lo escrito SIN guardar nada. Usar una
    contraseña guardada para probar no la muestra, asi que no pide el desbloqueo (igual que el «Probar» de la tarjeta); la respuesta sale enmascarada."""
    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    base: dict[str, str] | None = None
    if req.conn_id:
        try:
            stored = config_store.get_stored(req.conn_id)
        except config_store.ConfigStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if stored.type_id != req.type:
            raise HTTPException(status_code=422, detail="Esa conexión es de otro tipo.")
        base = dict(stored.fields)
    try:
        fields = config_store.check_fields(req.type, {k: v for k, v in req.fields.items() if v is not None})
    except config_store.ConfigStoreError as exc:
        raise _vault_store_error(exc) from exc
    return await test_draft_connection(req.type, fields, base, req.name or (stored.name if req.conn_id else ""))


class VaultBigQueryTestRequest(BaseModel):
    values: dict[str, str | None] = {}


@app.post("/api/vault/bigquery/test")
async def vault_test_bigquery(req: VaultBigQueryTestRequest, request: Request) -> dict:
    """«Probar» de la seccion BigQuery (pedido del usuario 2026-10-10): proyecto y ubicacion escritos + la key guardada, sin guardar nada."""
    _vault_guard(request)
    return await test_bigquery_draft({k: v for k, v in req.values.items() if v is not None})


@app.get("/api/vault/connections/{conn_id}/secret/{field_key}")
def vault_reveal_connection_secret(conn_id: str, field_key: str, request: Request):
    """REQ-007: el ojo pide UN secreto guardado, solo desde esta PC, desde la pagina de Vision y desbloqueado. Nunca se cachea."""
    from fastapi.responses import JSONResponse  # import diferido: solo esta ruta arma la respuesta a mano

    from app import config_store  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    _vault_require_unlocked()
    try:
        stored = config_store.get_stored(conn_id)
    except config_store.ConfigStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if field_key not in _vault_secret_keys(stored.type_id):
        raise HTTPException(status_code=404, detail="Ese dato no es una contraseña de esta conexión.")
    return JSONResponse({"value": stored.fields.get(field_key, "")}, headers=_NO_STORE)


@app.get("/api/vault/general")
def vault_general(request: Request) -> dict:
    """Configuracion general (proveedor de IA, voz, BigQuery, Graph, red, general). Los secretos solo dicen si estan cargados."""
    from app import general_settings  # import diferido: modulo del gestor, solo lo usan estas rutas

    _vault_guard(request)
    return {"fields": general_settings.describe(), "graph_sources": connections.names_for("sharepoint")}


class VaultGeneralRequest(BaseModel):
    values: dict[str, str | None]


@app.put("/api/vault/general")
def vault_set_general(req: VaultGeneralRequest, request: Request) -> dict:
    """REQ-006: guarda y aplica sin reiniciar. Cambiar un secreto ya guardado exige el desbloqueo."""
    from app import config_store, general_settings  # import diferido: modulos del gestor, solo los usan estas rutas

    _vault_guard(request)
    _vault_require_imported()
    stored = config_store.get_general()
    if any((f := general_settings.field_for(k)) is not None and f.secret and stored.get(k) for k in req.values):
        _vault_require_unlocked()
    try:
        general_settings.save(req.values)
    except config_store.ConfigStoreError as exc:
        raise _vault_store_error(exc) from exc
    return {"fields": general_settings.describe()}


@app.get("/api/vault/general/secret/{key}")
def vault_reveal_general_secret(key: str, request: Request):
    """REQ-007: un secreto de la configuracion general (ej. la clave de Fish), con las mismas barreras que el de una conexion."""
    from fastapi.responses import JSONResponse  # import diferido: solo esta ruta arma la respuesta a mano

    from app import config_store, general_settings  # import diferido: modulos del gestor, solo los usan estas rutas

    _vault_guard(request)
    _vault_require_unlocked()
    field = general_settings.field_for(key)
    if field is None or not field.secret:
        raise HTTPException(status_code=404, detail="Ese dato no es una contraseña de la configuración.")
    return JSONResponse({"value": config_store.get_general().get(key, "")}, headers=_NO_STORE)


class VaultBigQueryKeyRequest(BaseModel):
    key_json: str


_BQ_KEY_REQUIRED = ("type", "project_id", "private_key", "client_email")
_BQ_KEY_MAX_CHARS = 100_000


@app.post("/api/vault/bigquery-key")
def vault_set_bigquery_key(req: VaultBigQueryKeyRequest, request: Request) -> dict:
    """REQ-008: importar (o reemplazar) la key de la cuenta de servicio. Se valida, se guarda cifrada (sin archivo) y se autocompleta el proyecto si estaba vacio.
    Devuelve solo proyecto y cuenta (para la tarjeta), nunca la clave. Reemplazar una key ya guardada exige el desbloqueo."""
    import json as _json  # import diferido: solo esta ruta parsea la key

    from app import config_store, general_settings  # import diferido: modulos del gestor, solo los usan estas rutas

    _vault_guard(request)
    _vault_require_imported()
    text = req.key_json.strip()
    if len(text) > _BQ_KEY_MAX_CHARS:
        raise HTTPException(
            status_code=422, detail="Ese archivo es demasiado grande para ser una clave de cuenta de servicio."
        )
    try:
        info = _json.loads(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Ese archivo no es una clave de cuenta de servicio de Google (no es un JSON válido).",
        ) from exc
    if (
        not isinstance(info, dict)
        or info.get("type") != "service_account"
        or any(not str(info.get(k) or "").strip() for k in _BQ_KEY_REQUIRED)
    ):
        raise HTTPException(
            status_code=422,
            detail="Ese archivo no es una clave de cuenta de servicio de Google: le faltan datos (tipo, proyecto, cuenta o clave).",
        )
    stored = config_store.get_general()
    if stored.get("bq_key_json"):
        _vault_require_unlocked()
    updates = {"bq_key_json": text}
    project_set = not stored.get("bq_project_id")
    if project_set:
        updates["bq_project_id"] = str(info["project_id"])
    general_settings.save(updates)
    return {
        "project_id": str(info["project_id"]),
        "client_email": str(info["client_email"]),
        "project_set": project_set,
    }


@app.get("/api/vault/unlock")
def vault_unlock_status(request: Request) -> dict:
    from app import secret_access  # import diferido: modulo chico, solo lo usan estas rutas

    _vault_guard(request)
    return {"unlocked_until": secret_access.unlocked_until()}


@app.post("/api/vault/unlock")
def vault_unlock(request: Request) -> dict:
    """REQ-007b: confirma la identidad (Windows Hello) y desbloquea unos minutos."""
    from app import secret_access  # import diferido: modulo chico, solo lo usan estas rutas

    _vault_guard(request)
    try:
        until = secret_access.unlock()
    except secret_access.UnlockError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"unlocked_until": until}


@app.delete("/api/vault/unlock")
def vault_lock(request: Request) -> dict:
    """Bloquear ya (al cerrar la pantalla)."""
    from app import secret_access  # import diferido: modulo chico, solo lo usan estas rutas

    _vault_guard(request)
    secret_access.lock()
    return {"unlocked_until": None}


@app.post("/api/connections/{type_id}/test")
async def test_named_connection(type_id: str, name: str | None = None) -> dict:
    """Prueba UNA conexion por nombre (boton «Probar» de su tarjeta). Sin `name`, la predeterminada del tipo."""
    try:
        return await test_one_connection(type_id, name)
    except connections.ConnectionLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Tipo de conexion desconocido: '{type_id}'.") from exc


# ---------------------------------------------------------------------------
# Configuracion de proyecto (SharePoint global / skills repo). La carpeta
# LOCAL ya NO es config global -- ver POST /api/sessions/{id}/project-path,
# cada sesion tiene la suya (arquitectura carpeta-por-sesion definitiva).
# ---------------------------------------------------------------------------
class SharepointProjectRequest(BaseModel):
    drive_id: str
    drive_name: str
    folder_id: str = ""
    folder_path: str = ""


class GenerationParamsRequest(BaseModel):
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str = ""
    verbosity: str = ""
    small_model: str = ""


# Pestaña "Conexiones" de Configuracion (pedido del usuario 2026-09-21):
# permite pisar en runtime, sin editar `.env` ni reiniciar el backend, los
# 3 umbrales que determinan que es una "consulta pesada" (ver
# app/tools/dbquery.py: _effective_bq_cost_threshold y hermanas). Cualquier
# campo en None deja de pisar el default de Settings para ESE campo puntual
# (vuelve a `.env`), no borra los otros dos.
class ConnectionThresholdsRequest(BaseModel):
    bq_cost_guardrail_bytes: int | None = None
    export_row_threshold: int | None = None
    export_preview_rows: int | None = None


@app.get("/api/config/project")
def get_project_config_endpoint() -> dict:
    from dataclasses import asdict

    return asdict(pc.get_project_config())


@app.post("/api/config/project/sharepoint")
def set_sharepoint_project_endpoint(req: SharepointProjectRequest) -> dict:
    from dataclasses import asdict

    return asdict(pc.set_sharepoint_project(req.drive_id, req.drive_name, req.folder_id, req.folder_path))


@app.delete("/api/config/project/sharepoint")
def clear_sharepoint_project_endpoint() -> dict:
    from dataclasses import asdict

    return asdict(pc.clear_sharepoint_project())


@app.get("/api/config/generation")
def get_generation_config() -> dict:
    from dataclasses import asdict

    cfg = asdict(pc.get_project_config())
    return {
        k: cfg[k]
        for k in (
            "temperature",
            "top_p",
            "max_tokens",
            "reasoning_effort",
            "verbosity",
            "small_model",
        )
    }


@app.post("/api/config/generation")
def set_generation_config(req: GenerationParamsRequest) -> dict:
    from dataclasses import asdict

    cfg = pc.set_generation_params(**req.model_dump())
    return asdict(cfg)


# ---------------------------------------------------------------------------
# Salto automatico de modelos (spec salto-automatico-modelos, REQ-001/003):
# la lista de prioridad que arma el usuario en Configuracion > Modelo e
# inteligencia, con el estado de cada modelo (disponible / en pausa hasta
# hh:mm / ya no existe en el proveedor de IA) y las opciones del catalogo para agregar.
# ---------------------------------------------------------------------------
class ModelFallbackRequest(BaseModel):
    enabled: bool | None = None
    order: list[str] | None = None
    pause_minutes: int | None = None


def _model_fallback_payload() -> dict:
    if _MODELS_CACHE.get("by_id") is None:
        list_models()  # carga el catalogo (cache de 5 min) para resolver los alias
    catalog = _MODELS_CACHE.get("by_id") or {}
    fallback = model_fallback.load_settings()
    return {
        "enabled": fallback.enabled,
        "order": list(fallback.order),
        "pause_minutes": fallback.pause_minutes,
        "items": model_fallback.status_items(fallback, catalog),
        "choices": model_fallback.catalog_choices(catalog),
    }


@app.get("/api/config/model-fallback")
def get_model_fallback_config() -> dict:
    return _model_fallback_payload()


@app.post("/api/config/model-fallback")
def set_model_fallback_config(req: ModelFallbackRequest) -> dict:
    current = model_fallback.load_settings()
    order = model_fallback.normalize_order(req.order) if req.order is not None else None
    enabled = current.enabled if req.enabled is None else req.enabled
    if enabled and not (order if order is not None else current.order):
        raise HTTPException(
            400,
            detail="Agregá al menos un modelo a la lista para activar el salto automático.",
        )
    if req.pause_minutes is not None and not (
        model_fallback.MIN_PAUSE_MINUTES <= req.pause_minutes <= model_fallback.MAX_PAUSE_MINUTES
    ):
        raise HTTPException(
            400,
            detail=(
                f"Los minutos de pausa tienen que estar entre {model_fallback.MIN_PAUSE_MINUTES} "
                f"y {model_fallback.MAX_PAUSE_MINUTES}."
            ),
        )
    pc.set_model_fallback(enabled=req.enabled, order=order, pause_minutes=req.pause_minutes)
    return _model_fallback_payload()


class ModelFallbackCheckRequest(BaseModel):
    # El modelo elegido a mano en el selector; None = "Automático".
    model: str | None = None


@app.post("/api/model-fallback/check")
def check_model_fallback(req: ModelFallbackCheckRequest) -> dict:
    """REQ-010 (spec salto-automatico-modelos): el frontend pregunta, a la hora
    en que vence una pausa (y al cargar la pagina), con que modelo seguiria el
    chat ahora, para mover el chip y avisar la vuelta sin esperar al proximo
    mensaje. POST y no GET: si la pausa vencio, "consume" el aviso de vuelta
    (el proximo turno ya no lo repite). Usa el catalogo ya cacheado, sin llamar
    al proveedor de IA: es el mismo con el que el loop registro las pausas."""
    fallback = model_fallback.load_settings()
    if not fallback.enabled:
        return {
            "enabled": False,
            "model": None,
            "home": None,
            "text": None,
            "resume_at": None,
        }
    catalog = _MODELS_CACHE.get("by_id") or {}
    return {
        "enabled": True,
        **model_fallback.check_now(
            fallback, llm_providers.canonical(req.model) if req.model else None, default_model_id(), catalog
        ),
    }


# ---------------------------------------------------------------------------
# Pestaña "Conexiones" -- umbrales de "consulta pesada" configurables sin
# tocar `.env`, mas status online/error por color de cada conector
# (reusando `check_all_connectors`, ya usado por el panel "Conectores" del
# sidebar) y una precarga simple de datasets/tablas de BigQuery. Ver spec
# controles-seguridad-optimizacion-lightspeed-bigquery.
# ---------------------------------------------------------------------------
@app.get("/api/config/connections")
def get_connection_thresholds() -> dict:
    """Devuelve tanto el OVERRIDE guardado (puede tener campos en null si
    no se pisaron) como el DEFAULT real de Settings/.env -- la UI usa el
    default como placeholder/valor efectivo cuando el override es null."""
    settings = get_settings()
    cfg = pc.get_project_config()
    return {
        "overrides": {
            "bq_cost_guardrail_bytes": cfg.bq_cost_guardrail_bytes,
            "export_row_threshold": cfg.export_row_threshold,
            "export_preview_rows": cfg.export_preview_rows,
        },
        "defaults": {
            "bq_cost_guardrail_bytes": settings.bq_cost_guardrail_bytes,
            "export_row_threshold": settings.export_row_threshold,
            "export_preview_rows": settings.export_preview_rows,
        },
        "effective": {
            "bq_cost_guardrail_bytes": cfg.bq_cost_guardrail_bytes
            if cfg.bq_cost_guardrail_bytes is not None
            else settings.bq_cost_guardrail_bytes,
            "export_row_threshold": cfg.export_row_threshold
            if cfg.export_row_threshold is not None
            else settings.export_row_threshold,
            "export_preview_rows": cfg.export_preview_rows
            if cfg.export_preview_rows is not None
            else settings.export_preview_rows,
        },
    }


@app.post("/api/config/connections")
def set_connection_thresholds(req: ConnectionThresholdsRequest) -> dict:
    cfg = pc.set_connection_thresholds(**req.model_dump())
    return {
        "bq_cost_guardrail_bytes": cfg.bq_cost_guardrail_bytes,
        "export_row_threshold": cfg.export_row_threshold,
        "export_preview_rows": cfg.export_preview_rows,
    }


def _connector_error_detail(exc: BaseException, system: str) -> str:
    """`detail` de un error de conector: primero QUE paso y QUE hacer, en
    lenguaje simple (app/friendly_errors.py, pedido del usuario 2026-09-24);
    al final el detalle tecnico recortado, porque estas pantallas de
    Configuracion > Conexiones las usa quien configura credenciales y ahi
    SI necesita el texto real del error (decision anterior de este endpoint,
    que devolvia `str(exc)` completo)."""
    friendly = describe_connector_error(exc, system)
    logger.warning("Fallo de %s [%s]: %s", system, friendly.code, exc)
    return f"{friendly.message} Detalle técnico: {technical_summary(exc)}"


@app.get("/api/config/connections/bigquery/datasets")
async def list_bigquery_datasets_for_config() -> dict:
    """Precarga de la pestaña 'Conexiones' (pedido del usuario: 'listando
    por ejemplo sus bases de datos o dataset'). Version acotada de esta
    entrega: lista PLANA de datasets -- el arbol expandible/colapsable
    completo (dataset -> tablas -> columnas, al estilo de la pestaña
    Credenciales) queda para una spec dedicada (ver progress.md)."""
    from app.connectors.bigquery_connector import (
        BigQueryConnector,
    )  # import diferido: evita ciclo

    settings = get_settings()
    if not settings.enable_bigquery:
        raise HTTPException(
            status_code=400,
            detail="BigQuery no está activado en esta instalación de ULTRON. Si lo necesitás, avisá a soporte.",
        )
    try:
        datasets = await asyncio.wait_for(
            asyncio.to_thread(lambda: BigQueryConnector(settings).list_datasets()),
            timeout=15,
        )
        return {"datasets": sorted(datasets)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_connector_error_detail(exc, "BigQuery")) from exc


@app.get("/api/config/connections/bigquery/tables")
async def list_bigquery_tables_for_config(dataset_id: str) -> dict:
    from app.connectors.bigquery_connector import (
        BigQueryConnector,
    )  # import diferido: evita ciclo

    settings = get_settings()
    if not settings.enable_bigquery:
        raise HTTPException(
            status_code=400,
            detail="BigQuery no está activado en esta instalación de ULTRON. Si lo necesitás, avisá a soporte.",
        )
    try:
        tables = await asyncio.wait_for(
            asyncio.to_thread(lambda: BigQueryConnector(settings).list_tables(dataset_id)),
            timeout=15,
        )
        return {"tables": sorted(tables)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_connector_error_detail(exc, "BigQuery")) from exc


@app.get("/api/config/connections/sharepoint/lists")
async def list_sharepoint_lists_for_config(name: str | None = None) -> dict:
    """Listas de un SharePoint. `name` = conexion con nombre (spec conexiones-con-nombre REQ-010); sin `name`, la predeterminada."""
    settings = get_settings()
    try:
        conn = connections.resolve("sharepoint", name)
    except connections.ConnectionLookupError as exc:
        if name:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        conn = None  # sin ninguna conexion: se sigue con el `.env` de siempre y el conector explica que falta
    if conn is not None and conn.from_file:
        settings = connections.settings_for(conn)
    if not settings.enable_sharepoint:
        raise HTTPException(
            status_code=400,
            detail="SharePoint no está activado en esta instalación de ULTRON. Si lo necesitás, avisá a soporte.",
        )
    try:
        lists = await asyncio.wait_for(
            asyncio.to_thread(lambda: SharePointConnector(settings).list_lists()),
            timeout=15,
        )
        result = {"lists": sorted(lists)}
        if conn is not None and conn.from_file:
            result["connection"] = (
                conn.name
            )  # solo cuando hay una conexion con nombre: sin archivo la respuesta es la de siempre
        return result
    except Exception as exc:
        detail = _connector_error_detail(exc, "SharePoint")
        raise HTTPException(status_code=502, detail=connections.redact(conn, detail) if conn else detail) from exc


# ---------------------------------------------------------------------------
# Credenciales de conectores, CIFRADAS en disco (ver app/credentials.py) --
# reemplazan la edicion manual de backend/.env desde la pestaña
# "Credenciales" de Configuracion.
# ---------------------------------------------------------------------------
@app.get("/api/config/credentials")
def get_credentials_status_endpoint() -> dict:
    """Solo indica QUE campos tienen valor guardado -- nunca el valor
    real, ni siquiera cifrado (ver docstring de get_credentials_status)."""
    return credentials.get_credentials_status()


@app.get("/api/config/credentials/schema")
def get_credentials_schema_endpoint() -> dict:
    """REQ-103 (spec configuracion-limpieza-credenciales-servidor): el
    frontend arma el acordeon de la pestaña Credenciales llamando a este
    endpoint -- no hay HTML hardcodeado por campo, la UI se genera 100%
    a partir de esta definicion."""
    return credentials.get_connector_schema()


class SaveCredentialsRequest(BaseModel):
    """REQ-105: dict generico en vez de un modelo con los 21 campos fijos
    -- el usuario decidio "Guardar por seccion" (un boton propio dentro de
    cada conector del acordeon), asi que cada request solo trae los campos
    de UN conector a la vez. `credentials.save_credentials()` ya filtra
    contra `CREDENTIAL_FIELDS` (ignora silenciosamente cualquier key que
    no sea un campo real conocido), asi que aceptar un dict abierto aca no
    abre superficie nueva -- mismo criterio de validacion que antes, solo
    que ahora cubre 21 campos en vez de 6."""

    fields: dict[str, str] = {}


@app.post("/api/config/credentials")
def save_credentials_endpoint(req: SaveCredentialsRequest) -> dict:
    try:
        credentials.save_credentials(req.fields)
    except credentials.CredentialsMovedError as exc:  # spec gestor-conexiones: la configuracion ya esta en Vision
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except credentials.CredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Bug real (2026-09-27, cambio de Claude a Gemini): la lista de modelos
    # quedaba en cache 5 minutos y el selector seguia ofreciendo los modelos
    # del proveedor anterior. Al guardar credenciales se vuelve a pedir.
    _MODELS_CACHE.update({"data": None, "fetched_at": 0.0, "by_id": None})
    return credentials.get_credentials_status()


@app.delete("/api/config/credentials")
def clear_credentials_endpoint() -> dict:
    try:
        credentials.clear_credentials()
    except credentials.CredentialsMovedError as exc:  # spec gestor-conexiones: la configuracion ya esta en Vision
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return credentials.get_credentials_status()


@app.post("/api/config/credentials/test/{connector}")
async def test_connector_credentials_endpoint(connector: str) -> dict:
    """REQ-106: "Probar conexion" DENTRO de cada seccion del acordeon --
    reusa el MISMO `test_connection()` que ya usa el panel lateral de
    Conectores (`connectors_status.py`), pero bajo demanda y acotado a un
    solo conector (no los 4 en paralelo). Usa las credenciales YA GUARDADAS
    en `.env.enc` en este momento (decision registrada en spec.md, seccion
    Reglas de negocio: si el usuario cambio un campo pero no lo guardo
    todavia, debe guardar primero -- este endpoint no lee el formulario en
    vivo, solo lo que ya esta persistido)."""
    from app.connectors.bigquery_connector import BigQueryConnector
    from app.connectors.oracle_connector import OracleConnector
    from app.connectors.sap_connector import SapConnector
    from app.connectors.sharepoint_connector import SharePointConnector

    # spec conexiones-con-nombre: Oracle/SharePoint/SAP salieron de la pestaña Credenciales y se prueban por NOMBRE en POST /api/connections/{tipo}/test.
    # Este endpoint sigue sirviendo a BigQuery (la unica conexion que conserva la pestaña) y, por compatibilidad, a los otros tres con la
    # conexion PREDETERMINADA del tipo (la misma que usan las tools sin indicar `connection`).
    testers = {
        "oracle": OracleConnector,
        "bigquery": BigQueryConnector,
        "sharepoint": SharePointConnector,
        "sap": SapConnector,
    }
    connector_cls = testers.get(connector)
    if connector_cls is None:
        raise HTTPException(
            status_code=404,
            detail=f"Conector desconocido: '{connector}' (esperado uno de {sorted(testers)})",
        )
    settings = get_settings()
    if connector != "bigquery":
        try:
            settings = connections.settings_for(connections.resolve(connector))
        except connections.ConnectionLookupError:
            pass  # sin conexion configurada: se prueba con el `.env` de siempre y el conector explica que falta
    try:
        ok = await asyncio.wait_for(asyncio.to_thread(connector_cls(settings).test_connection), timeout=15.0)
    except TimeoutError as exc:
        system = CONNECTOR_NAMES.get(connector, connector)
        raise HTTPException(
            status_code=504,
            detail=(
                f"{system} tardó más de 15 segundos en responder. Revisá que estés "
                "conectado a la red de la empresa (VPN) y probá de nuevo."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- quien configura necesita tambien el detalle tecnico
        raise HTTPException(
            status_code=502,
            detail=_connector_error_detail(exc, CONNECTOR_NAMES.get(connector, connector)),
        ) from exc
    return {"connector": connector, "ok": bool(ok)}


# ---------------------------------------------------------------------------
# Explorador de filesystem local (para elegir carpeta de proyecto/skills)
# ---------------------------------------------------------------------------
@app.get("/api/fs/roots")
def fs_roots() -> dict:
    return {"roots": pc.list_local_roots()}


@app.get("/api/fs/children")
def fs_children(path: str) -> dict:
    try:
        return {"path": path, "children": pc.list_local_children(path)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


_native_picker_lock = threading.Lock()


def _force_foreground_window(root) -> None:  # noqa: ANN001 -- root es tkinter.Tk, import diferido
    """Fuerza que la ventana de tkinter (`root`) tome foreground de VERDAD en
    Windows, no solo `-topmost` -- pedido explicito del usuario (reportado
    2026-09-19: "la ventana del explorador para seleccionar carpeta no
    aparece encima del navegador... tengo que tabular entre aplicaciones
    porque queda abierto pero por detras").

    Causa raiz: `root.attributes("-topmost", True)` (el fix ya existente,
    ver comentario arriba) mantiene la ventana "siempre por encima de las
    no-topmost", pero Windows tiene una proteccion aparte,
    `LockSetForegroundWindow`, que le PROHIBE a un proceso robarle el foco
    de foreground a OTRO proceso (aca, el navegador) salvo que el que pide
    el foco ya tenga el foreground el mismo -- exactamente el caso de este
    picker: el proceso Python/uvicorn que lo abre nunca tuvo foreground (fue
    disparado por un `fetch()` AJAX del navegador). Resultado real: la
    ventana se crea, esta "topmost", pero Windows no la trae al frente --
    queda flotando detras hasta que el usuario hace Alt+Tab manualmente.

    Fix real (tecnica estandar de win32, sin pywin32 -- mismo patron ya
    usado en este archivo para `SetConsoleCtrlHandler`/`CTRL_CLOSE_EVENT`,
    ver `launch_vision.py`): `LockSetForegroundWindow` se puede sortear
    simulando una pulsacion de la tecla ALT antes de pedir el foreground --
    Windows trata esa secuencia como si el USUARIO hubiera iniciado el
    cambio de ventana (igual que Alt+Tab), no el proceso. Se hace con
    `keybd_event` (ALT down/up) inmediatamente antes de
    `SetForegroundWindow`. `root.lift()` + `root.focus_force()` (llamados
    ANTES desde `_pick()`) resuelven el apilamiento/foco a nivel de Tk; esta
    funcion resuelve el foreground a nivel de Windows, que es lo que
    realmente tapaba el bug."""
    import os

    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())
        if not hwnd:
            return
        VK_MENU = 0x12  # tecla ALT
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 -- best-effort, nunca debe romper el picker
        logger.warning("No se pudo forzar foreground del picker nativo", exc_info=True)


@app.get("/api/fs/native-picker")
async def fs_native_picker(initial_path: str = "", mode: str = "folder") -> dict:
    """Pedido explicito del usuario (2026-09-09): navegar carpetas con el
    explorador interno de Vision (uno x uno, sin poder crear/eliminar
    subcarpetas fluido, ni Ctrl+F, ni favoritos) es tedioso comparado con
    el Explorador de Windows real -- pero un <input webkitdirectory> o
    showDirectoryPicker() del NAVEGADOR no exponen la ruta absoluta
    (C:\\...) por seguridad del browser, y el backend la necesita.

    Solucion viable SOLO porque Vision corre 100% local (backend Python y
    navegador en la MISMA maquina, ver AGENTS.md): el backend abre el
    dialogo NATIVO de Windows el mismo (tkinter.filedialog, stdlib, cero
    dependencias nuevas) -- ese dialogo SI es el Explorador real (crear,
    eliminar, renombrar, ir atras/adelante, todo). Se corre en un thread
    aparte porque tkinter necesita su propio mainloop y bloquearia el
    event loop de FastAPI si se llamara directo.

    `mode="file"` (spec programador-alarmas 0.12.0): en vez de elegir
    CARPETA (askdirectory, el caso por defecto), elige un ARCHIVO
    (askopenfilename), con filtro de ejecutables/scripts (.exe/.bat/.cmd/
    .ps1/.com) para el campo "Archivo a ejecutar" de una tarea.

    Devuelve `{"path": ""}` si el usuario cancela el dialogo (nunca
    lanza) -- el frontend interpreta "" como cancelacion, igual que el
    explorador interno.

    BUG real corregido (deteccion QA 2026-09-16, "si demoro mucho se
    cierra... despues no abre mas el explorador"): el `asyncio.wait_for`
    de mas abajo tenia un timeout de 300s -- si el usuario tardaba mas (o
    por cualquier otro motivo el await se cortaba), `wait_for` abandonaba
    la ESPERA pero el thread de fondo (`asyncio.to_thread`) seguia vivo
    con el dialogo NATIVO todavia abierto (nadie mata un thread de Python
    a la fuerza). Tcl/Tk (la libreria detras de tkinter) NO soporta tener
    DOS `tkinter.Tk()` corriendo en threads distintos del mismo proceso a
    la vez -- el clic siguiente del usuario disparaba un segundo `_pick()`
    que chocaba con el primero (huerfano, todavia esperando su propio
    dialogo), y por eso el explorador dejaba de abrir por completo hasta
    reiniciar el backend. Fix: un lock de proceso rechaza inmediatamente
    (409, mensaje claro) un segundo pedido mientras el primero sigue
    activo, en vez de dejar que ambos threads corran a la vez y corrompan
    el interprete de Tcl compartido. El timeout tambien se estira a 30
    minutos (300s era corto para elegir carpeta con calma) ya que ahora
    expirarlo es seguro -- el lock impide que un reintento choque con el
    thread huerfano que haya quedado vivo."""
    if not _native_picker_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                "Ya hay un selector de carpeta/archivo abierto -- buscalo detras de "
                "la ventana del navegador (Alt+Tab) y elegi o cancela ahi antes de "
                "volver a intentar."
            ),
        )

    import tkinter
    from tkinter import filedialog

    def _pick() -> str:
        # El lock se libera ACA (thread real), no en el `finally` del
        # `await` de abajo: si el `wait_for` se cumple primero, este
        # thread sigue vivo con el dialogo todavia abierto -- liberar el
        # lock antes de que ESTE thread realmente termine reabriria la
        # misma ventana para que un segundo pedido choque contra el.
        try:
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)  # necesario pero INSUFICIENTE en Windows -- ver _force_foreground_window
            root.lift()
            root.focus_force()
            _force_foreground_window(root)
            try:
                if mode == "any-file":
                    # spec adjuntar-archivos-chat REQ-007 c: elegir CUALQUIER archivo para
                    # leerlo en su lugar (sin filtro de ejecutables, a diferencia de "file").
                    selected = filedialog.askopenfilename(
                        initialdir=initial_path or str(Path.home()),
                        title="Elegir archivo a adjuntar - ULTRON",
                        filetypes=[("Todos los archivos", "*.*")],
                    )
                elif mode == "file":
                    selected = filedialog.askopenfilename(
                        initialdir=initial_path or str(Path.home()),
                        title="Elegir archivo a ejecutar - ULTRON",
                        filetypes=[
                            ("Ejecutables/Scripts", "*.exe;*.bat;*.cmd;*.ps1;*.com"),
                            ("Todos los archivos", "*.*"),
                        ],
                    )
                else:
                    selected = filedialog.askdirectory(
                        initialdir=initial_path or str(Path.home()),
                        title="Elegir carpeta de proyecto - ULTRON",
                    )
            finally:
                root.destroy()
            return selected or ""
        finally:
            _native_picker_lock.release()

    try:
        path = await asyncio.wait_for(asyncio.to_thread(_pick), timeout=1800)
    except Exception as exc:  # tkinter puede fallar en entornos sin display (ej. servidor headless)
        logger.warning("No se pudo abrir el explorador nativo de Windows: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="No se pudo abrir el explorador nativo de Windows en este entorno.",
        ) from exc
    return {"path": path}


class CreateFolderRequest(BaseModel):
    parent_path: str
    folder_name: str


@app.post("/api/fs/create-folder")
def fs_create_folder(req: CreateFolderRequest) -> dict:
    """Crea una subcarpeta nueva DENTRO de `parent_path` -- usado por el
    boton '+ Nueva carpeta' del explorador de Configuracion > Proyecto
    local / Skills."""
    try:
        return pc.create_local_folder(req.parent_path, req.folder_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Explorador de sitio SharePoint (bibliotecas -> carpetas)
# ---------------------------------------------------------------------------
@app.get("/api/sharepoint/drives")
def sharepoint_drives() -> dict:
    try:
        sp = SharePointConnector(settings)
        return {"drives": sp.list_drives()}
    except SharePointConnectionError as exc:
        raise HTTPException(status_code=502, detail=_connector_error_detail(exc, "SharePoint")) from exc


@app.get("/api/sharepoint/children")
def sharepoint_children(drive_id: str, item_id: str | None = None) -> dict:
    try:
        sp = SharePointConnector(settings)
        children = sp.list_folder_children(drive_id, item_id)
        path = sp.get_item_path(drive_id, item_id) if item_id else ""
        return {"children": children, "path": path}
    except SharePointConnectionError as exc:
        raise HTTPException(status_code=502, detail=_connector_error_detail(exc, "SharePoint")) from exc


# ---------------------------------------------------------------------------
# Skills: descubrimiento (menu "/" del chat) + CRUD de skills locales
# (REQ-007/008/009, spec agentes-huerfanos-y-gestion-skills)
# ---------------------------------------------------------------------------
@app.get("/api/skills")
def list_skills() -> dict:
    return {"skills": discover_skills()}


@app.get("/api/local-skills/{session_id}")
def list_local_skills_endpoint(session_id: str) -> dict:
    """Comando "/local-skills": skills de la subcarpeta `skills/` DENTRO
    de la carpeta de proyecto PROPIA de esta sesion -- separado de
        `/api/skills` (siempre globales) a pedido explicito del usuario.

    REQ-008: cada skill incluye `content` (cuerpo sin frontmatter) para que
    el panel de Configuracion > Skills pueda precargar el editor sin un
    endpoint generico de lectura de archivos -- el comando "/local-skills"
    del chat (unico otro consumidor de este endpoint) simplemente ignora
    ese campo extra."""
    from app.tools.skill import discover_local_skills_with_body

    _assert_session_owner(session_id)
    return {"skills": discover_local_skills_with_body(session_id)}


class LocalSkillSaveRequest(BaseModel):
    session_id: str
    name: str
    description: str = ""
    content: str
    original_name: str = ""  # distinto de `name` = renombrar (ver save_local_skill)


@app.post("/api/local-skills")
def save_local_skill_endpoint(req: LocalSkillSaveRequest) -> dict:
    """REQ-008: crea/edita una skill LOCAL de la carpeta de proyecto de
    `session_id` -- las skills GLOBALES son de solo lectura desde Vision
    (ver docstring de save_local_skill), no tienen endpoint de escritura."""
    from app.tools.skill import save_local_skill

    _assert_session_owner(req.session_id)
    project_path = persistence_service.project_root_for_session(req.session_id)
    if project_path is None:
        raise HTTPException(
            status_code=400,
            detail="Esta sesión todavía no tiene una carpeta de proyecto asignada.",
        )
    try:
        skills = save_local_skill(
            str(project_path),
            req.name,
            req.description,
            req.content,
            original_name=req.original_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"skills": skills}


@app.delete("/api/local-skills/{session_id}/{name}")
def delete_local_skill_endpoint(session_id: str, name: str) -> dict:
    """REQ-009: borra una skill local por nombre. El aviso de "N agente(s)
    usan esta skill" se resuelve del lado del FRONTEND antes de llamar a
    este endpoint (mismo patron ya usado para comandos, ver
    renderCommandsSettingsList en app.js) -- este endpoint solo ejecuta el
    borrado en si, sin bloquear."""
    from app.tools.skill import delete_local_skill

    _assert_session_owner(session_id)
    project_path = persistence_service.project_root_for_session(session_id)
    if project_path is None:
        return {"skills": []}
    return {"skills": delete_local_skill(str(project_path), name)}


class SkillsRepoPathRequest(BaseModel):
    path: str


@app.post("/api/skills/repo-path")
def set_skills_repo_path_endpoint(req: SkillsRepoPathRequest) -> dict:
    """REQ-007 (spec agentes-huerfanos-y-gestion-skills): fija la carpeta
    LOCAL de skills globales -- reemplaza por completo el flujo Git/SSH que
    existia antes (`POST /api/skills/sync-repo`, `backend/app/skills_repo.py`,
    ELIMINADOS). El usuario ya tenia esta ruta configurable a mano (este
    endpoint no es nuevo), pero antes convivia con el boton "Sincronizar
    repo GitHub" -- descubierto durante el build que ese flujo Git era
    innecesario: `discover_skills()` YA escanea esta ruta en vivo en cada
    listado (sin ningun botón de sync), tipicamente apuntando a una carpeta
    ya sincronizada localmente por el cliente de OneDrive/SharePoint del
    propio Windows, sin ningun llamado a Graph API."""
    from dataclasses import asdict

    return asdict(pc.set_skills_repo_path(req.path))


class SkillsConfigRequest(BaseModel):
    global_enabled: bool | None = None
    repo_path: str | None = None


@app.get("/api/skills/config")
def get_skills_config_endpoint() -> dict:
    """Config de skills a nivel Vision (DB maestra, NO por sesion): si las
    skills GLOBALES estan habilitadas (no precargadas por defecto, pedido del
    usuario 2026-09) y que carpeta/repo esta vinculada."""
    from app.persistence import master_index
    from app.tools.skill import SKILLS_GLOBAL_ENABLED_KEY, SKILLS_REPO_PATH_KEY

    return {
        "global_enabled": (master_index.get_app_setting(SKILLS_GLOBAL_ENABLED_KEY) == "true"),
        "repo_path": master_index.get_app_setting(SKILLS_REPO_PATH_KEY) or "",
    }


@app.post("/api/skills/config")
def set_skills_config_endpoint(req: SkillsConfigRequest) -> dict:
    """Persiste la config de skills a nivel Vision (DB maestra): habilita/
    deshabilita las globales y/o vincula una carpeta/repo. Persiste en
    `.vision/vision.db`, por lo que aplica a TODAS las sesiones."""
    from app.persistence import master_index
    from app.tools.skill import SKILLS_GLOBAL_ENABLED_KEY, SKILLS_REPO_PATH_KEY

    if req.global_enabled is not None:
        master_index.set_app_setting(SKILLS_GLOBAL_ENABLED_KEY, "true" if req.global_enabled else "false")
    if req.repo_path is not None:
        master_index.set_app_setting(SKILLS_REPO_PATH_KEY, req.repo_path.strip())
    return get_skills_config_endpoint()


@app.post("/api/skills/config/reset")
def reset_skills_config_endpoint() -> dict:
    """Resetea la config de skills a nivel Vision: vuelve a 'sin vínculo y
    sin precargar globales' (skills locales de sesion siguen disponibles)."""
    from app.persistence import master_index
    from app.tools.skill import SKILLS_GLOBAL_ENABLED_KEY, SKILLS_REPO_PATH_KEY

    master_index.unset_app_setting(SKILLS_GLOBAL_ENABLED_KEY)
    master_index.unset_app_setting(SKILLS_REPO_PATH_KEY)
    return {"reset": True}


# ---------------------------------------------------------------------------
# Comandos "/" (spec comandos-slash): 7 comandos del sistema (portados de
# OpenCode + /rename) + CRUD de comandos propios del usuario.
# ---------------------------------------------------------------------------
@app.get("/api/commands")
def list_commands_endpoint(session_id: str = "") -> dict:
    """REQ-001: alimenta la seccion 'Comandos' del menu '/' del chat --
    sistema (fijos) + propios (arquitectura carpeta-por-sesion: los
    propios son los de `commands/` dentro de la carpeta PROPIA de
    `session_id`, no de una carpeta global)."""
    project_path = persistence_service.project_root_for_session(session_id)
    # spec comando-update-sesiones-publicas REQ-010: `/update` solo aparece si esta carpeta tiene conversaciones publicas de otras personas.
    offer_update = bool(session_id) and persistence_service.has_foreign_public_sessions(session_id)
    return {
        "system": [c for c in commands_module.SYSTEM_COMMANDS if c["name"] != "update" or offer_update],
        "custom": commands_module.list_custom_commands(str(project_path) if project_path else None),
    }


class SaveCustomCommandRequest(BaseModel):
    session_id: str
    name: str
    prompt: str


@app.post("/api/commands/custom")
def save_custom_command_endpoint(req: SaveCustomCommandRequest) -> dict:
    """REQ-008/009: crea o actualiza (mismo nombre) un comando propio en
    la carpeta de proyecto PROPIA de `req.session_id`. REQ-006 (QA-032):
    `_assert_session_owner` -- antes faltaba, asi que quien conociera un
    `session_id` ajeno podia escribir comandos en su carpeta."""
    _assert_session_owner(req.session_id)
    project_path = persistence_service.project_root_for_session(req.session_id)
    if project_path is None:
        raise HTTPException(
            status_code=400,
            detail="Esta sesión todavía no tiene carpeta de proyecto asignada.",
        )
    try:
        commands = commands_module.save_custom_command(str(project_path), req.name, req.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"custom": commands}


@app.delete("/api/commands/custom/{name}")
def delete_custom_command_endpoint(name: str, session_id: str = "") -> dict:
    """REQ-009. REQ-006 (QA-032): `_assert_session_owner` + el nombre pasa
    por `validate_command_name` (dentro de `delete_custom_command`) antes
    de resolver la ruta -- antes un nombre con '..\\' llegaba tal cual
    (por HTTP incluso decodificado desde '%5C') y borraba un archivo fuera
    de `commands/` de la sesion."""
    _assert_session_owner(session_id)
    project_path = persistence_service.project_root_for_session(session_id)
    try:
        commands = commands_module.delete_custom_command(str(project_path) if project_path else "", name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"custom": commands}


class ImportCommandsRequest(BaseModel):
    session_id: str
    source_path: str
    # REQ-005: nombres que el usuario YA confirmo sobrescribir (2do paso).
    overwrite: list[str] = []


@app.post("/api/commands/import-path")
def import_commands_endpoint(req: ImportCommandsRequest) -> dict:
    """REQ-005 (spec quick-panel-labels-comandos): importa los `.md` de una
    carpeta local a `commands/` de la sesion. Los conflictos de nombre se
    devuelven sin sobrescribir -- el frontend confirma y reintenta con
    `overwrite`. Solo escribe dentro de la carpeta de proyecto de la sesion
    (el nombre de archivo destino se deriva del stem validado, nunca de una
    ruta que venga del cliente)."""
    _assert_session_owner(req.session_id)
    project_path = persistence_service.project_root_for_session(req.session_id)
    if project_path is None:
        raise HTTPException(
            status_code=400,
            detail="Esta sesión todavía no tiene carpeta de proyecto asignada. Elegí una carpeta primero.",
        )
    try:
        return commands_module.import_commands_from_folder(str(project_path), req.source_path, req.overwrite)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/commands/init-prompt")
def get_init_prompt_endpoint() -> dict:
    """REQ-003: expone el prompt fijo de /init -- el frontend lo manda
    como un mensaje de chat NORMAL (mismo camino que cualquier turno),
    asi el LLM lo ejecuta con sus tools ya disponibles (read/glob/grep/
    write) sin necesitar ningun endpoint de backend nuevo para la logica
    en si."""
    return {"prompt": commands_module.INIT_PROMPT}


@app.post("/api/commands/update/{session_id}")
async def update_from_public_endpoint(session_id: str) -> dict:
    """spec comando-update-sesiones-publicas REQ-008: resume lo nuevo de las conversaciones publicas de otras personas en la carpeta de esta sesion,
    con el CSID de quien las publico, y lista sus archivos. Solo la dueña de la sesion (403 si no); sin carpeta, 400 con una frase. El trabajo
    corre en un hilo: llama al modelo y usa `asyncio.run`, que no puede correr dentro del bucle del servidor."""
    _assert_session_owner(session_id)
    folder = persistence_service.master_index.get_project_path(session_id)
    if not folder:
        raise HTTPException(
            400, detail="Primero elegí la carpeta de esta sesión: sin carpeta no hay conversaciones públicas que traer."
        )
    try:
        result = await asyncio.to_thread(persistence_service.update_from_public, session_id, folder)
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log; la persona recibe un texto entendible
        logger.warning("No se pudo actualizar la sesion %s desde las publicas", session_id, exc_info=True)
        raise HTTPException(
            500, detail="No pude traer las novedades de las conversaciones públicas. Probá de nuevo en un momento."
        ) from exc
    _reload_session_memory(session_id)  # las notas ya estan en SQLite: el proximo mensaje las ve
    return result


class CompactRequest(BaseModel):
    """spec aviso-contexto-largo REQ-006: cuerpo OPCIONAL de `/compact`; sin el, el pedido es el de siempre."""

    model: str | None = None


@app.post("/api/commands/compact/{session_id}")
async def compact_session_endpoint(session_id: str, req: CompactRequest | None = None) -> dict:
    """REQ-002: ejecuta compactacion manual sin crear un turno nuevo.

    spec aviso-contexto-largo REQ-005/006: resume con el modelo elegido en el selector (o, con el salto automatico activo, con el primero
    disponible, igual que la compactacion automatica del turno) y, si el modelo falla (ej. 429 de tope de gasto), devuelve un texto para
    una persona en vez de un 500 mudo. El crudo va al log y la sesion no se toca."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_restore(session_id)  # persistencia-sqlite-sesiones REQ-003: tambien tras un reinicio
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
    if not session.messages:
        return {"compacted": False, "reason": "sin historial que compactar"}

    chosen = ((req.model if req else None) or "").strip() or None
    fallback_settings = model_fallback.load_settings()
    if fallback_settings.enabled:
        picker = model_fallback.TurnModelPicker(
            fallback_settings, chosen, default_model_id(), _MODELS_CACHE.get("by_id")
        )
        model = picker.first_available()
    else:
        model = chosen or default_model_id()
    try:
        compacted = await compact_session(session_id, session.messages, resolve_context_window(model), model)
    except Exception as exc:  # noqa: BLE001 -- el crudo va al log; la persona recibe un texto entendible
        friendly = describe_model_error(exc, model)
        logger.warning(
            "No se pudo compactar la sesion %s con %s [%s]: %s", session_id, model, friendly.code, exc, exc_info=True
        )
        raise HTTPException(
            status_code=429 if friendly.status == 429 else 502,
            detail=f"No pude resumir la conversación. {friendly.message}",
        ) from exc
    return {"compacted": compacted}


@app.post("/api/commands/qa-audit/{session_id}")
async def qa_audit_endpoint(session_id: str) -> dict:
    """REQ-004 (spec bateria-qa-integracion): comando THIN `/qa-audit`. Corre
    la bateria de QA (`scripts/qa_battery.py`) como subproceso y, por cada
    FAIL o feature SIN COBERTURA, agrega una tarea pending a `session.todos`
    (mismo shape y evento SSE que `todowrite`) para que el `/loop` la
    procese. El backend NO corre pytest dentro de su propio proceso: es un
    subproceso aislado (no infla el runtime, solo a pedido)."""
    import subprocess as _sp

    from app.tools.todowrite import _clean_todo_item

    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_restore(session_id)  # persistencia-sqlite-sesiones REQ-003: tambien tras un reinicio
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")

    # raiz del repo = parents[2] (app -> backend -> vision-web):
    # las rutas de la bateria (scripts/) y del venv viven UNA carpeta arriba
    # de backend/, no en backend/ mismo.
    repo_dir: Path = Path(__file__).resolve().parents[2]  # vision-web/
    venv_py = repo_dir / "backend" / ".venv" / "Scripts" / "python.exe"
    battery_script = repo_dir / "scripts" / "qa_battery.py"
    try:
        proc = await asyncio.wait_for(
            asyncio.to_thread(
                _sp.run,
                [str(venv_py), str(battery_script)],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=120,
            ),
            timeout=125,
        )
    except Exception as exc:  # bateria es tooling, no bloquea
        logger.warning("No se pudo correr la bateria QA: %s", exc)
        raise HTTPException(status_code=500, detail="No se pudo correr la batería QA.") from exc

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    issues = [line.strip() for line in output.splitlines() if "[FAIL]" in line or "SIN COBERTURA" in line]

    existing = {t.get("content") for t in (session.todos or []) if t.get("status") == "pending"}
    added = 0
    for issue in issues:
        content = f"QA: {issue}"
        if content in existing:
            continue
        session.todos.append(
            _clean_todo_item(
                {"content": content, "status": "pending", "priority": "medium"},
                allow_subtasks=True,
            )
        )
        existing.add(content)
        added += 1

    # Notificar al frontend (mismo evento que todowrite) y persistir.
    session.push_event("todo", {"todos": session.todos})
    if session.session_id:
        from app.persistence import service as _persistence  # diferido: evita ciclo

        _persistence.record_todos(session.session_id, session.todos)

    return {
        "ran": True,
        "exit_code": proc.returncode,
        "issues": issues,
        "added_todos": added,
    }


@app.get("/api/commands/export/{session_id}")
def export_session_command_endpoint(session_id: str):
    """REQ-004 (comando /export).

        Bug real reportado por el usuario (2026-09-08): esta ruta devolvia el
        .md como descarga del navegador (`Content-Disposition: attachment`)
        -- con la sesion YA con carpeta asignada, el navegador de todos modos
        podia disparar SU PROPIO dialogo nativo "Guardar como" (segun la
        configuracion de descargas del usuario), contradiciendo el proposito
        de la arquitectura carpeta-por-sesion: el sistema YA SABE donde
        guardar, no debia preguntar de nuevo. Ahora reusa DIRECTO
        `export_session_to_file()` (mismo mecanismo que ya usaba la
        auto-purga a 30 dias) -- escribe el .md a disco en
        `<carpeta-de-la-sesion>/sessions/<Nombre de la sesión>_session-
        <fecha-hora>.md` sin pasar nunca por el navegador, y devuelve JSON
        con la ruta real escrita para que el frontend la muestre en el chat.

        Sigue devolviendo 404 si la sesion no tiene carpeta asignada -- en
        ESE caso (y SOLO en ese caso) el frontend abre su explorador interno
    para que el usuario elija una carpeta por primera vez (ver
        validateProjectFolderOrPrompt/exportCurrentSession en app.js)."""
    _assert_session_owner(session_id)
    from app.persistence.transcript import export_session_to_file

    out_path = export_session_to_file(session_id)
    if out_path is None:
        raise HTTPException(
            status_code=404,
            detail="No se pudo exportar: esta sesión no tiene carpeta de proyecto asignada.",
        )
    return {"path": str(out_path)}


# ---------------------------------------------------------------------------
# Panel de navegador integrado: sirve el mini-proyecto (HTML/CSS/JS,
# dashboards generados con JS) que Vision escribe en browser/{session_id}/
# via las tools write/edit/bash. Contenido TEMPORAL -- ver
# app/browser_workspace.py para el ciclo de vida completo.
# ---------------------------------------------------------------------------
@app.get("/api/browser/{session_id}/fingerprint")
def browser_fingerprint(session_id: str) -> dict:
    """Huella liviana (cantidad de archivos + mtimes/tamanos) para que el
        frontend haga polling barato y detecte cuando Vision escribio/edito
        algo -- dispara el auto-refresh del iframe sin necesitar un websocket
    ni un filesystem watcher persistente en el backend."""
    _assert_session_owner(session_id)
    has_index = browser_workspace.find_index_file(session_id) is not None
    return {
        "fingerprint": browser_workspace.get_fingerprint(session_id),
        "has_index": has_index,
    }


@app.get("/browser/{session_id}/{file_path:path}")
def serve_browser_file(session_id: str, file_path: str):
    """Sirve un archivo puntual dentro de la carpeta de vista previa de la
    sesion (ej. /browser/<id>/dashboard.html, /browser/<id>/app.js).

    Bug real reportado por el usuario (2026-09): el `:path` capturante
    tambien matchea `/browser/<id>/` con `file_path=""`, y ahi se intentaba
    leer el archivo '' -> 404 "No existe '' en esta sesion" (el iframe abria
    vacio). Si `file_path` esta vacio (raiz), se delega a
    serve_browser_index para que sirva el index.html."""
    _assert_session_owner(session_id)
    from fastapi import Response

    if not file_path.strip("/ \\"):
        return serve_browser_index(session_id)

    result = browser_workspace.read_file(session_id, file_path)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No existe '{file_path}' en esta sesion.")
    content, content_type = result
    return Response(content=content, media_type=content_type)


def _browser_waiting_page():
    """Pagina que ve el iframe de vista previa cuando la sesion todavia no tiene ningun archivo HTML para mostrar."""
    from fastapi import Response

    return Response(
        content=(
            "<html><body style='font-family:monospace;background:#0b0c0e;"
            "color:#666d78;display:flex;align-items:center;justify-content:center;"
            "height:100vh;margin:0'>"
            "Aún no hay ningún archivo HTML en esta sesión.<br>"
            "Pedile a ULTRON que genere un dashboard o proyecto para previsualizar acá."
            "</body></html>"
        ),
        media_type="text/html; charset=utf-8",
    )


@app.get("/browser/{session_id}")
@app.get("/browser/{session_id}/")
def serve_browser_index(session_id: str):
    """Sirve el archivo 'de entrada' (index.html/dashboard.html) de la
    carpeta de vista previa de la sesion cuando el iframe pide la raiz."""
    _assert_session_owner(session_id)
    from fastapi import Response

    index_name = browser_workspace.find_index_file(session_id)
    if index_name is None:
        return _browser_waiting_page()
    result = browser_workspace.read_file(session_id, index_name)
    if result is None:
        # El archivo desaparecio entre la busqueda y la lectura (el agente reescribe el dashboard justo cuando el iframe pide la raiz).
        # Mismo caso que 'todavia no hay archivo': antes el desempaquetado de None lanzaba TypeError y respondia 500. (BL-003, mypy)
        return _browser_waiting_page()
    content, content_type = result
    return Response(content=content, media_type=content_type)


# Bug real reportado por el usuario durante narracion-agente-efimera
# (9na sesion, click manual en el "ojo" que "no funciono en produccion"):
# hipotesis investigada y NO descartada en su momento -- cache agresivo
# del navegador sirviendo un app.js VIEJO (de antes de agregar el listener
# de click), documentado como problema conocido del proyecto (ver
# AGENTS.md, "Ctrl+Shift+R"). `StaticFiles` de Starlette no manda ningun
# header de cache explicito -- deja el comportamiento por defecto del
# navegador, que para archivos servidos sin ETag/Last-Modified fuerte
# suele cachear agresivamente. Subclase minima que fuerza no-cache en
# TODOS los estaticos del frontend: el navegador SIEMPRE revalida contra
# el server (If-Modified-Since/ETag siguen funcionando para no re-bajar
# el archivo si no cambio), pero nunca sirve una copia vieja sin preguntar.
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Sirve el frontend vanilla (HTML/CSS/JS sin build step) como archivos
# estáticos. RESOURCE_ROOT apunta a la raíz del repo durante desarrollo y a
# la extracción temporal de PyInstaller dentro del EXE autocontenido.
_FRONTEND_DIR = RESOURCE_ROOT / "frontend"
if _FRONTEND_DIR.exists():
    app.mount(
        "/",
        _NoCacheStaticFiles(directory=str(_FRONTEND_DIR), html=True),
        name="frontend",
    )


if __name__ == "__main__":
    # REQ-008 (spec fix-detener-y-progreso-visible): mismo criterio que Iniciar-Vision.bat (`--reload-dir app`): se vigila SOLO el
    # codigo de Vision (esta carpeta, sin importar desde donde se lance). Sin esto uvicorn vigila la carpeta actual entera y un
    # `pip install` del agente dentro de backend/.venv reinicia Vision a mitad del turno (bug real del 2026-10-11).
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        reload_dirs=[str(Path(__file__).resolve().parent)],
        # Hallazgo del 2026-10-17: con un chat abierto (stream que no termina) el reinicio por reload se cuelga sin este limite; ver Iniciar-Vision.bat.
        timeout_graceful_shutdown=5,
    )
