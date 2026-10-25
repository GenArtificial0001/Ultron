"""
Vision - backend FastAPI del asistente superinteligente.

Levantar con:
    python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8030
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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import browser_workspace, credentials, lsp
from app import commands as commands_module
from app import project_context as pc
from app.auth import AccessTokenMiddleware
from app.config import RESOURCE_ROOT, get_settings
from app.connectors.sharepoint_connector import (
    SharePointConnectionError,
    SharePointConnector,
)
from app.connectors_status import CONNECTOR_NAMES, check_all_connectors
from app.friendly_errors import (
    describe_connector_error,
    describe_internal_error,
    describe_os_error,
    describe_transcription_error,
    describe_voice_engine_error,
    technical_summary,
)
from app.llm import model_fallback
from app.llm import providers as llm_providers
from app.llm.client import get_client, get_stt_client
from app.llm.loop import run_turn
from app.meeting_gate import MeetingGateState, check_invocation
from app.persistence import purge as persistence_purge
from app.persistence import service as persistence_service
from app.scheduler import agents as agents_scheduler
from app.scheduler import alarms as alarms_scheduler
from app.session.compaction import compact_session
from app.session.events import get_session_store
from app.token_estimate import estimate_tokens
from app.tools.skill import discover_local_skills, discover_skills
from app.version import __version__
from app.voice import local_clone
from app.voice import service as voice_service
from app.voice.engines import VoiceEngineError
from app.voice.settings import VoiceConfigError
from app.voice.settings import load_config as voice_load_config
from app.voice.settings import update_config as voice_update_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
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
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logging.getLogger().addHandler(_file_handler)
logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title="Vision API", version=__version__)

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
    # REQ-006/009 (spec asistente-contextual-voz, Modo Cerebro): True si
    # este turno se origino en una intervencion del modo Cerebro (voz
    # continua) -- activa la confirmacion hablada antes de ejecutar tools
    # peligrosas (ver run_turn en app/llm/loop.py). False (default) = chat
    # de texto normal, ejecuta sin confirmar (comportamiento sin cambios).
    voice_confirm: bool = False
    # spec informe-tecnico-hablado: el frontend avisa si el informe tecnico
    # hablado esta prendido (ver UserTurn.narrate).
    narrate: bool = False


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

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "audio.webm"  # la API de audio infiere el formato del nombre de archivo

    stt_model = "?"
    try:
        client, stt_model = get_stt_client()
        # `language` solo se fuerza si el usuario eligio uno explicito en
        # Configuracion > Audio -- el SDK espera "str | Omit" (no admite
        # None), asi que se arman 2 ramas en vez de un **kwargs generico
        # (el type-checker no puede resolver los overloads de `create`
        # contra un dict desestructurado). Omitir el parametro por
        # completo es lo que le dice a Whisper que haga auto-deteccion
        # (comportamiento default pedido).
        if req.language:
            response = client.audio.transcriptions.create(
                model=stt_model,
                file=audio_file,
                response_format="text",
                language=req.language,
            )
        else:
            response = client.audio.transcriptions.create(
                model=stt_model,
                file=audio_file,
                response_format="text",
            )
    except Exception as exc:
        # El texto crudo del proveedor va solo al log; el usuario ve que paso
        # y que hacer (ver app/friendly_errors.py).
        friendly = describe_transcription_error(exc)
        logger.exception("Error transcribiendo audio con %s [%s]", stt_model, friendly.code)
        raise HTTPException(502, detail=friendly.message) from None

    # response_format="text" del SDK de OpenAI devuelve el string directo
    # (no un objeto con .text) -- pero por las dudas de que el proveedor lo
    # envuelva distinto, se soportan ambas formas.
    text = (
        response
        if isinstance(response, str)
        else getattr(response, "text", str(response))
    )
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
            raise HTTPException(
                400, detail=f"Primero confirmá que el texto que Vision lee se envía a {engine.label}."
            )
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
    brevity_instruction = (
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
    confusion_protocol_instruction = (
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
    chat_context_instruction = (
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
    role_scope_instruction = (
        "[Alcance de tu rol, Modo reunión] Estás en el Modo reunión: tu función acá es "
        "escuchar la conversación (potencialmente de varias personas) y opinar cuando te "
        "lo piden, usando tus herramientas de consulta (Oracle/BigQuery/SharePoint/skills) "
        "si hace falta fundamento. Si te piden específicamente operar sobre un archivo o "
        'ruta de un proyecto (ej. "revisá el archivo X", "corregí el bug en Y"), '
        "aclará brevemente que ese tipo de trabajo puntual sobre archivos se hace mejor "
        "en el chat de texto o en el Modo Cerebro (pensado para eso), en vez de intentarlo "
        "acá sin el contexto adecuado de una sesión de trabajo dedicada."
    )
    transcript_context = req.full_transcript.strip()
    if transcript_context:
        user_message = (
            f"{brevity_instruction}\n\n"
            f"{confusion_protocol_instruction}\n\n"
            f"{chat_context_instruction}\n\n"
            f"{role_scope_instruction}\n\n"
            "[Transcripción completa de audio en vivo, Modo reunión, hasta este momento]\n"
            f"{transcript_context}\n\n"
            "[Última frase transcrita -- acaban de mencionarte y pedirte algo, "
            "respondé basándote en TODO el contexto de arriba]\n"
            f"{req.fragment.strip()}"
        )
    else:
        user_message = (
            f"{brevity_instruction}\n\n"
            f"{confusion_protocol_instruction}\n\n"
            f"{chat_context_instruction}\n\n"
            f"{role_scope_instruction}\n\n"
            f"[Transcripción de audio en vivo, Modo reunión] {req.fragment.strip()}"
        )
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
                llm_error = event.get("data", {}).get(
                    "message", "Error desconocido del LLM"
                )
    except Exception:
        logger.exception("Error invocando al LLM desde el Modo reunion")
        raise HTTPException(
            502,
            detail=(
                "No pude preparar mi respuesta en la reunión por un problema interno de "
                "Vision. La reunión sigue escuchando; si se repite, cerrá y volvé a abrir Vision."
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
            detail="Vision está terminando otra respuesta en esta conversación. Esperá a que termine y probá de nuevo.",
        )

    summarize_prompt = (
        "Se terminó una reunión (Modo reunión de Vision). A continuación está la "
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
                llm_error = event.get("data", {}).get(
                    "message", "Error desconocido del LLM"
                )
    except Exception:
        logger.exception("Error generando el resumen del Modo reunion")
        raise HTTPException(
            502,
            detail=(
                "No pude armar el resumen de la reunión por un problema interno de Vision. "
                "Probá de nuevo; si se repite, cerrá y volvé a abrir Vision."
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
            ids, error = [], "no respondió (revisá la clave o la conexión)"
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
    if (
        _MODELS_CACHE.get("by_id") is None
        or (time.monotonic() - _MODELS_CACHE["fetched_at"]) >= _MODELS_CACHE_TTL
    ):
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


async def _drain_session_queue(
    session, wait_for_turn_id: str | None = None, queue=None
):
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
            yield _sse_format({k: v for k, v in event.items() if k != "_turn_id"})
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
        "temperature": req.temperature
        if req.temperature is not None
        else cfg.temperature,
        "top_p": req.top_p if req.top_p is not None else cfg.top_p,
        "max_tokens": req.max_tokens if req.max_tokens is not None else cfg.max_tokens,
        "reasoning_effort": req.reasoning_effort or cfg.reasoning_effort or None,
        "verbosity": req.verbosity or cfg.verbosity or None,
    }


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

    async def _run_in_background():
        # REQ-005 (spec clasificador-tipo-respuesta): comando /plan
        # REQ-005 (spec clasificador-tipo-respuesta): comando /plan
        # override manual que fuerza la clasificacion a Planificable. Se
        # instrumenta agregandole una instruccion fuerte e in-place al texto
        # del usuario (el LLM se acopla a las instrucciones).
        final_text = turn.text
        if final_text.lstrip().lower().startswith("/plan "):
            pedido = final_text.replace("/plan ", "", 1).strip()
            final_text = (
                "OVER@ride de clasificacion: este mensaje llego con el comando /plan. "
                "Obligatoriamente printea como PRIMER TOKEN [[TIPO: Planificable]] y luego "
                f"responde este pedido: {pedido}"
            )

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
            ):
                session.push_event(event["type"], event.get("data", {}))
        except Exception:  # turno en background: si no se avisa aca, nadie lo ve
            # Antes solo habia `finally`: si algo fallaba DENTRO de Vision (no
            # en el modelo -- esas fallas ya llegan como evento "error" del
            # loop), la excepcion moria dentro de la Task y el chat terminaba
            # EN SILENCIO, sin ningun aviso (pedido del usuario 2026-09-24:
            # errores entendibles para personas no tecnicas).
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
        finally:
            turn.status = "done"
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
                _launch_turn(session, session_id, next_turn)
            elif session.loop_active:
                # spec modo-plan-loop REQ-006/007: sin nada en la cola
                # HUMANA, pero con el loop activo -- se sintetiza el
                # PROXIMO turno automaticamente (mismo mecanismo de
                # auto-encadenamiento de arriba, reusado tal cual).
                _maybe_continue_loop(session, session_id)

    session.current_task = asyncio.create_task(_run_in_background())


LOOP_MAX_TURNS = 30


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
        id=f"loop-{uuid.uuid4()}",
        text=(
            "Continuá automáticamente con la próxima tarea 'pending' de la lista de tareas "
            "(todowrite). Marcala 'completed' cuando termines. Si necesitás tomar una decisión "
            "sin una opción claramente mejor, preguntá con la tool question -- si SÍ hay una "
            "opción claramente mejor, marcala como recomendada y seguí sin preguntar."
        ),
    )
    _launch_turn(session, session_id, synthetic_turn)


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
        voice_confirm=req.voice_confirm,
        narrate=req.narrate,
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


@app.post("/api/chat/{session_id}/stop")
def stop_turn_endpoint(session_id: str) -> dict:
    """REQ-001: pide detener el turno activo de la sesion. NO cancela la
        tarea a la fuerza (asyncio.Task.cancel()) -- eso cortaria una tool en
        ejecucion a mitad de camino. En cambio, marca `stop_requested = True`,
        que `run_turn` chequea entre pasos del loop y tambien durante el
    streaming de texto (ver app/llm/loop.py), garantizando que cualquier
        tool ya en curso termine de correr por completo antes de cortar."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None or not session.is_busy:
        return {"stopped": False, "reason": "no_active_turn"}
    session.stop_requested = True
    session.edit_pending_text = None  # asegura que sea un STOP real, no una edicion
    # spec modo-plan-loop REQ-007b: STOP tambien apaga el loop autonomo --
    # sin esto, _maybe_continue_loop sintetizaria el proximo turno apenas
    # termine el actual, ignorando la intencion real del usuario de frenar.
    session.loop_active = False
    session.loop_turns_count = 0
    session.loop_paused_for_question = False
    session.loop_auto = False  # REQ-011: detener el loop apaga el modo auto
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
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, detail="Esta conversación ya no está abierta en Vision (puede que se haya reiniciado). Recargá la página.")
    if session.is_busy:
        raise HTTPException(
            409,
            detail="Vision todavía está respondiendo en esta conversación. Frenala con el botón Detener y después limpiala.",
        )

    session.messages = []
    session.turns = {}
    session.queue_order = []
    session.push_event("conversation-cleared", {})
    persistence_service.clear_messages(session_id)
    return {"cleared": True}


@app.post("/api/session/{session_id}/loop")
async def start_loop_endpoint(
    session_id: str, req: StartLoopRequest | None = None
) -> dict:
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
        raise HTTPException(
            400, detail="No hay tareas pendientes para ejecutar en loop."
        )

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


@app.patch("/api/chat/{session_id}/message/{message_id}")
async def edit_message_endpoint(
    session_id: str, message_id: str, req: EditMessageRequest
) -> dict:
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
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail="Esta conversación ya no está abierta en Vision (puede que se haya reiniciado). Recargá la página."
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
    if turn is not None and turn.messages_start_index is not None:
        start = turn.messages_start_index
    elif message_id.startswith("idx:"):
        occurrence = int(message_id.removeprefix("idx:"))
        start = session.find_user_message_index(occurrence)
    else:
        start = None

    if start is None:
        raise HTTPException(
            status_code=404,
            detail="No encontré ese mensaje en la conversación: puede que ya se haya borrado. Recargá la página.",
        )

    # Trunca desde ese mensaje user en adelante (descarta su respuesta
    # original y todo lo posterior) y arranca un turno nuevo con el texto
    # editado, como si fuera el mensaje mas reciente de la conversacion.
    del session.messages[start:]
    turn_id = message_id
    from app.session.events import UserTurn

    new_turn = UserTurn(id=turn_id, text=req.text)
    if session.is_busy:
        session.enqueue_turn(new_turn)
        session.push_event(
            "queued", {"message_id": turn_id, "position": session.queue_length - 1}
        )
    else:
        _launch_turn(session, session_id, new_turn)
    return {"case": "history_truncated_and_relaunched", "message_id": message_id}


@app.delete("/api/chat/{session_id}/message/{message_id}")
def delete_message_endpoint(session_id: str, message_id: str) -> dict:
    """Elimina un mensaje. Solo tiene sentido para mensajes EN COLA
    (REQ-005/007) o de historial viejo (REQ-009) -- no se puede 'eliminar'
    el turno activo ahora mismo sin mas contexto (para eso esta STOP)."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get_or_create(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail="Esta conversación ya no está abierta en Vision (puede que se haya reiniciado). Recargá la página."
        )

    if session.remove_queued_turn(message_id):
        return {"case": "queued_turn_removed", "message_id": message_id}

    turn = session.turns.get(message_id)
    if turn is not None and turn.messages_start_index is not None:
        start = turn.messages_start_index
    elif message_id.startswith("idx:"):
        start = session.find_user_message_index(int(message_id.removeprefix("idx:")))
    else:
        start = None

    if start is None:
        raise HTTPException(
            status_code=404,
            detail="No encontré ese mensaje en la conversación: puede que ya se haya borrado. Recargá la página.",
        )

    del session.messages[start:]
    return {"case": "history_truncated", "message_id": message_id}


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
            status_code=404, detail="Esta conversación ya no está abierta en Vision (puede que se haya reiniciado). Recargá la página."
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
        or user_idx
        >= len(session.messages)  # FIX (2026-09): selo desbordaba -> IndexError 500
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
        user_idx = None
        cnt = 0
        for i, m in enumerate(session.messages):
            if m.get("role") == "user":
                if cnt == occurrence:
                    user_idx = i
                    break
                cnt += 1
        if user_idx is not None:
            end = user_idx + 1
            while end < len(session.messages) and session.messages[end].get("role") in (
                "assistant",
                "tool",
            ):
                end += 1
            if end > user_idx + 1:
                del session.messages[user_idx + 1 : end]
        persistence_service.delete_assistant_response(session_id, user_occurrence)
        return {"case": "response_deleted", "message_id": message_id}

    # REQ-004: el bloque a borrar es TODO lo que sigue inmediatamente al
    # mensaje 'user' mientras sea 'assistant' o 'tool' -- se detiene apenas
    # aparece el PROXIMO 'user' (inicio de otro turno) o se acaba la lista.
    end = user_idx + 1
    while end < len(session.messages) and session.messages[end].get("role") in (
        "assistant",
        "tool",
    ):
        end += 1

    if end == user_idx + 1:
        raise HTTPException(
            status_code=404,
            detail="Ese turno todavia no tiene ninguna respuesta para eliminar.",
        )

    del session.messages[user_idx + 1 : end]

    # REQ-005: consistencia SQLite -- se identifica el turno por CUANTOS
    # mensajes 'user' lo preceden (0-indexed), NO por el indice absoluto.
    user_occurrence = sum(
        1 for m in session.messages[:user_idx] if m.get("role") == "user"
    )
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
            status_code=404, detail="Esta conversación ya no está abierta en Vision (puede que se haya reiniciado). Recargá la página."
        )
    ok = session.reorder_queue(req.message_id, req.new_position)
    if not ok:
        raise HTTPException(
            status_code=404, detail="Ese mensaje ya no está en la cola (puede que ya se haya enviado)."
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

    return StreamingResponse(
        _drain_session_queue(session), media_type="text/event-stream"
    )


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
    session = store.get(session_id)
    return {"todos": session.todos if session else []}


# ---------------------------------------------------------------------------
# Programador de alarmas (spec programador-alarmas) -- persistencia GLOBAL en
# el DB de Vision (pedido del usuario 2026-09: no por-sesion). La tabla
# `schedules` vive en el DB maestro (`master_index.py`), valida sin importar
# la sesion activa. El motor de disparo es FRONTEND (timers, REQ-005) que
# persiste via esta API.
# ---------------------------------------------------------------------------
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
        tool_base._audit(
            "run_file", {"path": path, "status": "error", "error": str(exc)}
        )
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
def run_agent_now_endpoint(agent_id: str) -> dict:
    """Pedido del usuario: "Ejecutar ahora" desde el panel lateral de la
    sesion o la pestaña Agentes -- dispara este agente YA, sin esperar su
    horario. Cuenta como un disparo real (marca `last_fired_at`, ver
    `run_agent_now`), asi un 'once' no se dispara DE NUEVO mas tarde."""
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
        "persistence_available": persistence_service.is_persistence_available(
            session_id
        ),
        # proyecto-por-sesion REQ-006: ruta a mostrar en el chip del
        # command-bar -- carpeta propia, default sugerido, o raiz de
        # Vision como referencia visual si no hay ninguna.
        "project_path": persistence_service.resolve_project_path_for_display(
            session_id
        ),
        # spec modo-plan-loop REQ-001/005: modo actual ("plan"/"active") --
        # el frontend lo usa para pintar el chip correcto sin depender de
        # un estado local que se desincronice al recargar/cambiar de sesion.
        "mode": session.mode if session else "active",
        # REQ-006/009: el frontend usa esto para saber si debe mostrar el
        # banner+alarma de "el loop necesita tu ayuda" al reconectarse.
        "loop_active": session.loop_active if session else False,
        "loop_paused_for_question": session.loop_paused_for_question
        if session
        else False,
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
        "persistence_available": persistence_service.is_persistence_available(
            session_id or None
        ),
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


@app.post("/api/sessions/{session_id}/project-path")
def assign_session_project_path_endpoint(
    session_id: str, req: AssignProjectPathRequest
) -> dict:
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
    try:
        persistence_service.assign_project_path(session_id, path, title=req.title)
    except FileNotFoundError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"assigned": True, "path": path}


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
    available = persistence_service.is_persistence_available(session_id or None)
    if not term:
        return {"results": [], "persistence_available": available}
    return {
        "results": persistence_service.search_sessions(term, session_id or None),
        "persistence_available": available,
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
    casos normales, no fallas."""
    _assert_session_owner(session_id)
    return {
        "messages": persistence_service.load_session_messages_for_display(session_id)
    }


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
        raise HTTPException(
            status_code=404, detail="Esta sesión no tiene carpeta de proyecto asignada."
        )

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
    return {
        "agent_count": count,
        "is_fallback_session": is_fallback,
        "is_fallback_blocked": is_fallback and count > 0,
        "fallback_session_id": fallback_id,
        "will_reassign_to_fallback": count > 0
        and bool(fallback_id)
        and fallback_id != session_id,
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
    if (
        agent_count > 0
        and fallback_id
        and fallback_id != session_id
        and mi.session_index_exists(fallback_id)
    ):
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
def resolve_context_window(model_id: str) -> int:
    """Resuelve el context_window real cacheado o usa fallback seguro."""
    context_window = 200_000
    for entry in (_MODELS_CACHE.get("by_id") or {}).values():
        if entry.get("id") == model_id:
            context_window = entry.get("context_window") or context_window
            break
    return context_window


@app.get("/api/context/{session_id}")
def get_context_usage(session_id: str) -> dict:
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None:
        return {"tokens": 0, "context_window": 0, "pct_used": 0, "estimated": True}

    tokens = estimate_tokens(session.messages)
    context_window = resolve_context_window(default_model_id())

    pct = min(100, round((tokens / context_window) * 100)) if context_window else 0
    return {
        "tokens": tokens,
        "context_window": context_window,
        "pct_used": pct,
        "estimated": True,
    }


# ---------------------------------------------------------------------------
# Estado de conectores (panel lateral, reemplazo funcional de "LSP")
# ---------------------------------------------------------------------------
@app.get("/api/connectors/status")
async def connectors_status() -> dict:
    return {"connectors": await check_all_connectors()}


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

    return asdict(
        pc.set_sharepoint_project(
            req.drive_id, req.drive_name, req.folder_id, req.folder_path
        )
    )


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
            400, detail="Agregá al menos un modelo a la lista para activar el salto automático."
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
        return {"enabled": False, "model": None, "home": None, "text": None, "resume_at": None}
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
            detail="BigQuery no está activado en esta instalación de Vision. Si lo necesitás, avisá a soporte.",
        )
    try:
        datasets = await asyncio.wait_for(
            asyncio.to_thread(lambda: BigQueryConnector(settings).list_datasets()),
            timeout=15,
        )
        return {"datasets": sorted(datasets)}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=_connector_error_detail(exc, "BigQuery")
        ) from exc


@app.get("/api/config/connections/bigquery/tables")
async def list_bigquery_tables_for_config(dataset_id: str) -> dict:
    from app.connectors.bigquery_connector import (
        BigQueryConnector,
    )  # import diferido: evita ciclo

    settings = get_settings()
    if not settings.enable_bigquery:
        raise HTTPException(
            status_code=400,
            detail="BigQuery no está activado en esta instalación de Vision. Si lo necesitás, avisá a soporte.",
        )
    try:
        tables = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: BigQueryConnector(settings).list_tables(dataset_id)
            ),
            timeout=15,
        )
        return {"tables": sorted(tables)}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=_connector_error_detail(exc, "BigQuery")
        ) from exc


@app.get("/api/config/connections/sharepoint/lists")
async def list_sharepoint_lists_for_config() -> dict:
    settings = get_settings()
    if not settings.enable_sharepoint:
        raise HTTPException(
            status_code=400,
            detail="SharePoint no está activado en esta instalación de Vision. Si lo necesitás, avisá a soporte.",
        )
    try:
        lists = await asyncio.wait_for(
            asyncio.to_thread(lambda: SharePointConnector(settings).list_lists()),
            timeout=15,
        )
        return {"lists": sorted(lists)}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=_connector_error_detail(exc, "SharePoint")
        ) from exc


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
    except credentials.CredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Bug real (2026-09-27, cambio de Claude a Gemini): la lista de modelos
    # quedaba en cache 5 minutos y el selector seguia ofreciendo los modelos
    # del proveedor anterior. Al guardar credenciales se vuelve a pedir.
    _MODELS_CACHE.update({"data": None, "fetched_at": 0.0, "by_id": None})
    return credentials.get_credentials_status()


@app.delete("/api/config/credentials")
def clear_credentials_endpoint() -> dict:
    credentials.clear_credentials()
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
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(connector_cls(settings).test_connection), timeout=15.0
        )
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
            root.attributes(
                "-topmost", True
            )  # necesario pero INSUFICIENTE en Windows -- ver _force_foreground_window
            root.lift()
            root.focus_force()
            _force_foreground_window(root)
            try:
                if mode == "file":
                    selected = filedialog.askopenfilename(
                        initialdir=initial_path or str(Path.home()),
                        title="Elegir archivo a ejecutar - Vision",
                        filetypes=[
                            ("Ejecutables/Scripts", "*.exe;*.bat;*.cmd;*.ps1;*.com"),
                            ("Todos los archivos", "*.*"),
                        ],
                    )
                else:
                    selected = filedialog.askdirectory(
                        initialdir=initial_path or str(Path.home()),
                        title="Elegir carpeta de proyecto - Vision",
                    )
            finally:
                root.destroy()
            return selected or ""
        finally:
            _native_picker_lock.release()

    try:
        path = await asyncio.wait_for(asyncio.to_thread(_pick), timeout=1800)
    except (
        Exception
    ) as exc:  # tkinter puede fallar en entornos sin display (ej. servidor headless)
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
        raise HTTPException(
            status_code=502, detail=_connector_error_detail(exc, "SharePoint")
        ) from exc


@app.get("/api/sharepoint/children")
def sharepoint_children(drive_id: str, item_id: str | None = None) -> dict:
    try:
        sp = SharePointConnector(settings)
        children = sp.list_folder_children(drive_id, item_id)
        path = sp.get_item_path(drive_id, item_id) if item_id else ""
        return {"children": children, "path": path}
    except SharePointConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=_connector_error_detail(exc, "SharePoint")
        ) from exc


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
        "global_enabled": (
            master_index.get_app_setting(SKILLS_GLOBAL_ENABLED_KEY) == "true"
        ),
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
        master_index.set_app_setting(
            SKILLS_GLOBAL_ENABLED_KEY, "true" if req.global_enabled else "false"
        )
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
    return {
        "system": commands_module.SYSTEM_COMMANDS,
        "custom": commands_module.list_custom_commands(
            str(project_path) if project_path else None
        ),
    }


class SaveCustomCommandRequest(BaseModel):
    session_id: str
    name: str
    prompt: str


@app.post("/api/commands/custom")
def save_custom_command_endpoint(req: SaveCustomCommandRequest) -> dict:
    """REQ-008/009: crea o actualiza (mismo nombre) un comando propio en
    la carpeta de proyecto PROPIA de `req.session_id`."""
    project_path = persistence_service.project_root_for_session(req.session_id)
    if project_path is None:
        raise HTTPException(
            status_code=400,
            detail="Esta sesión todavía no tiene carpeta de proyecto asignada.",
        )
    try:
        commands = commands_module.save_custom_command(
            str(project_path), req.name, req.prompt
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"custom": commands}


@app.delete("/api/commands/custom/{name}")
def delete_custom_command_endpoint(name: str, session_id: str = "") -> dict:
    """REQ-009."""
    project_path = persistence_service.project_root_for_session(session_id)
    commands = commands_module.delete_custom_command(
        str(project_path) if project_path else "", name
    )
    return {"custom": commands}


@app.get("/api/commands/init-prompt")
def get_init_prompt_endpoint() -> dict:
    """REQ-003: expone el prompt fijo de /init -- el frontend lo manda
    como un mensaje de chat NORMAL (mismo camino que cualquier turno),
    asi el LLM lo ejecuta con sus tools ya disponibles (read/glob/grep/
    write) sin necesitar ningun endpoint de backend nuevo para la logica
    en si."""
    return {"prompt": commands_module.INIT_PROMPT}


@app.post("/api/commands/compact/{session_id}")
async def compact_session_endpoint(session_id: str) -> dict:
    """REQ-002: ejecuta compactacion manual sin crear un turno nuevo."""
    _assert_session_owner(session_id)
    store = get_session_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
    if not session.messages:
        return {"compacted": False, "reason": "sin historial que compactar"}

    model = default_model_id()
    compacted = await compact_session(
        session_id, session.messages, resolve_context_window(model), model
    )
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
    session = store.get(session_id)
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
        raise HTTPException(
            status_code=500, detail="No se pudo correr la batería QA."
        ) from exc

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    issues = [
        line.strip()
        for line in output.splitlines()
        if "[FAIL]" in line or "SIN COBERTURA" in line
    ]

    existing = {
        t.get("content") for t in (session.todos or []) if t.get("status") == "pending"
    }
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
        raise HTTPException(
            status_code=404, detail=f"No existe '{file_path}' en esta sesion."
        )
    content, content_type = result
    return Response(content=content, media_type=content_type)


@app.get("/browser/{session_id}")
@app.get("/browser/{session_id}/")
def serve_browser_index(session_id: str):
    """Sirve el archivo 'de entrada' (index.html/dashboard.html) de la
    carpeta de vista previa de la sesion cuando el iframe pide la raiz."""
    _assert_session_owner(session_id)
    from fastapi import Response

    index_name = browser_workspace.find_index_file(session_id)
    if index_name is None:
        return Response(
            content=(
                "<html><body style='font-family:monospace;background:#0b0c0e;"
                "color:#666d78;display:flex;align-items:center;justify-content:center;"
                "height:100vh;margin:0'>"
                "Aún no hay ningún archivo HTML en esta sesión.<br>"
                "Pedile a Vision que genere un dashboard o proyecto para previsualizar acá."
                "</body></html>"
            ),
            media_type="text/html; charset=utf-8",
        )
    result = browser_workspace.read_file(session_id, index_name)
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
    uvicorn.run(
        "app.main:app", host=settings.app_host, port=settings.app_port, reload=True
    )
