"""
Mensajes de error para personas NO tecnicas.

Pedido del usuario (2026-09-24): "mejora el manejo de errores para que los
mensajes lo entiendan las personas no tecnicas". Caso real que lo disparo: el
chat mostraba "El modelo rechazo la solicitud (HTTP 400): suele deberse a un
parametro de generacion o un schema de herramientas..." -- nadie fuera de
sistemas sabe que es un HTTP 400 ni un schema, y el mensaje no decia que
hacer.

Regla de este modulo -- todo mensaje que ve el usuario dice:
  1. QUE paso, en palabras simples (sin codigos HTTP, nombres de excepciones,
     JSON del gateway ni rutas internas), y
  2. QUE puede hacer (reintentar, elegir otro modelo, reiniciar Vision...).
El detalle crudo nunca se pierde: el llamador lo registra con
`logger.exception(...)`, y `FriendlyError.code` es una referencia corta
("MODELO-LIMITE") que se muestra aparte, en "Detalle tecnico", para que
soporte encuentre la entrada del log sin que el usuario tenga que entenderla.

La clasificacion usa el `status_code` real de la excepcion del SDK de
OpenAI/el proveedor de IA siempre que existe. La version anterior buscaba "400"/"429"
como texto dentro del mensaje, y eso confundia casos reales: el proveedor de IA
envuelve errores del proveedor (un 500 que adentro dice "400 Bad Request"),
y cualquier numero del texto ("4001 tokens") podia matchear.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = [
    "FriendlyError",
    "describe_all_models_paused",
    "describe_connector_error",
    "describe_internal_error",
    "describe_model_error",
    "describe_os_error",
    "describe_step_limit",
    "describe_task_needs_new_message",
    "describe_transcription_error",
    "describe_voice_engine_error",
    "model_recovered_notice",
    "model_switch_notice",
    "technical_summary",
]


@dataclass(frozen=True)
class FriendlyError:
    """Error listo para mostrarle a una persona no tecnica.

    `code`: referencia estable y corta para soporte (va en "Detalle tecnico",
    nunca en el texto principal). `message`: que paso + que hacer.
    `status`: codigo HTTP real si lo hubo (solo para el detalle tecnico)."""

    code: str
    message: str
    status: int | None = None

    def technical(self, *, model: str | None = None, when: str | None = None) -> str:
        """Linea corta para "Detalle tecnico": codigo, HTTP, modelo y hora
        (la hora es la misma que queda en el log, para encontrarlo rapido)."""
        parts = [self.code]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if model:
            parts.append(model)
        if when:
            parts.append(when)
        return " · ".join(parts)


# -- Senales dentro del texto del error (siempre en minusculas) --------------

_CONTEXT_TOO_LONG = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "contextwindowexceeded",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "input token count",
    "exceeds the maximum number of tokens",
)
_CONTENT_FILTER = (
    "content_filter",
    "content management policy",
    "responsibleaipolicyviolation",
    "prohibited_content",
    "harm_category",
    "safety",
)
# Historial de herramientas que el proveedor no acepta (caso real 2026-09-24,
# Gemini: "the number of function response parts is equal to the number of
# function call parts"; Claude: ids de tool_use con caracteres invalidos).
_TOOL_HISTORY = (
    "function response",
    "function call",
    "tool_call",
    "tool_use",
    "tool_result",
)
_IMAGE = ("image_url", "image", "mime type")
# spec modelo-sin-herramientas-aviso: el modelo elegido no admite el argumento de herramientas (caso real 2026-10-22: `glm-ocr`,
# «This model does not support tools argument»). Vision siempre manda sus herramientas, asi que reenviar no sirve: hay que cambiar de
# modelo. Se evalua ANTES de `_TOOL_HISTORY` (su texto contiene «tools») y despues de «conversacion larga» y «filtro».
_NO_TOOLS = (
    "does not support tools",
    "does not support function calling",
    "does not support function-calling",
    "tools is not supported",
    "tools are not supported",
    "tool use is not supported",
    "tool_choice is not supported",
)
_QUOTA = ("too much spend", "spend limit")
# Bug real (2026-09-27): con la cuenta de Anthropic sin credito, la API responde
# 400 "Your credit balance is too low to access the Anthropic API" y Vision lo
# mostraba como "no acepto este pedido tal como esta". No es del pedido ni del
# modelo: ningun modelo de ese proveedor va a responder hasta cargar credito.
# Ojo: Gemini responde "exceeded your current quota" cuando UN modelo no tiene cupo
# gratuito (ej. los Pro): eso es un limite de ese modelo, no falta de credito de la cuenta
# (medido 2026-09-27). Por eso esa frase no esta aca.
_NO_CREDIT = ("credit balance is too low", "insufficient_quota")
# Formato del tope de gasto visto en la prueba real del usuario (2026-09-25,
# 15:37): "Too many requests: you've reached the $3 per 600 seconds spend limit
# ... On-prem models are free and not spend-limited". A diferencia del formato
# documentado ("model X is limited to $N per N seconds"), no nombra un modelo:
# es un tope del USUARIO que vale para todos los modelos pagos.
_PER_MODEL_SPEND_RE = re.compile(r"\bmodel\s+\S+\s+is\s+limited")
_AUTH = (
    "401",
    "unauthorized",
    "invalid_grant",
    "invalid_client",
    "aadsts",
    "credential",
    "authentication",
    "login denied",
    "ora-01017",
    "ora-28000",
    "ora-28001",
)
_FORBIDDEN = ("403", "forbidden", "access denied", "accessdenied", "permission", "ora-01031")
_NOT_FOUND = ("404", "not found", "notfound", "does not exist", "ora-00942")
_NETWORK = (
    "connection",
    "connect",
    "unreachable",
    "resolve",
    "getaddrinfo",
    "name or service",
    "proxy",
    "ssl",
    "tls",
    "winerror 10060",
    "winerror 10061",
    "ora-12170",
    "ora-12541",
    "ora-12545",
    "dpy-6005",
)
_TIMEOUT = ("timed out", "timeout", "time out", "tardo demasiado")
# Pedido del usuario 2026-10-09: el proxy de internet de la empresa no llega a los servidores internos de la empresa (SAP
# wms.empresa.com, el repositorio de paquetes): responde 503 al pedir el tunel. Es lo primero que se mira, aunque el texto
# tambien traiga "timed out" o "connection": la solucion es otra.
_PROXY = ("unable to connect to proxy", "tunnel connection failed", "proxyerror")
# Falla del login del proveedor de IA ANTES de llamar al modelo (sdk-proveedor/MSAL, ver
# skill skill-interna troubleshooting "401 / access token errors").
_TOKEN_FETCH = (
    "error retrieving the token",
    "failed to refresh access token",
    "aadsts",
    "invalid_grant",
)

_SPEND_WINDOW_RE = re.compile(r"per\s+(\d+)\s+seconds", re.IGNORECASE)
_STATUS_IN_TEXT_RE = re.compile(r"\berror code:\s*(\d{3})\b", re.IGNORECASE)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _status_of(exc: BaseException) -> int | None:
    """Codigo HTTP real: atributo del SDK (`APIStatusError.status_code`) o,
    como respaldo, el prefijo "Error code: NNN" que el SDK pone en el texto."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_IN_TEXT_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _type_names(exc: BaseException) -> set[str]:
    """Nombres de la jerarquia de la excepcion, en minusculas -- permite
    reconocer `APITimeoutError`/`APIConnectionError` del SDK sin importar
    openai aca (este modulo tambien lo usan los conectores y la voz).
    Se compara por nombre EXACTO: `SharePointConnectionError` (el error de
    dominio de TODO problema de SharePoint, incluido un 500) no es un corte
    de red aunque su nombre contenga "ConnectionError"."""
    return {cls.__name__.lower() for cls in type(exc).__mro__}


def _is_timeout(names: set[str]) -> bool:
    return any("timeout" in name for name in names)


_NETWORK_TYPES = {"connectionerror", "apiconnectionerror", "connecterror", "proxyerror", "sslerror"}


def _is_network(names: set[str]) -> bool:
    return bool(names & _NETWORK_TYPES)


# REQ-001 (spec fix-corte-proveedor): el OTRO lado cerro la conexion a mitad de la respuesta. Caso real 2026-10-14 (log de DEV): el proveedor de IA
# empezo a responder (200 OK, en streaming) y 2 min 30 s despues llego `httpx2.RemoteProtocolError: peer closed connection without sending
# complete message body`. El SDK de OpenAI no envuelve los errores de lectura del stream (si los de ANTES de responder, como
# APIConnectionError): llega la excepcion cruda del transporte, que ningun tipo de arriba reconocia y caia en MODELO-DESCONOCIDO.
# ReadError es el mismo corte visto como «conexion reiniciada» (WinError 10054: gateway, proxy o VPN); IncompleteRead, el de la libreria
# estandar. Va DESPUES del timeout: una lectura que tarda de mas (ReadTimeout) sigue siendo MODELO-DEMORA.
_CUT_TYPES = {"remoteprotocolerror", "readerror", "incompleteread"}


def _is_cut(names: set[str]) -> bool:
    return bool(names & _CUT_TYPES)


def _model_label(model: str | None) -> str:
    return f"el modelo {model}" if model else "el modelo elegido"


def _minutes_text(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return "1 minuto" if minutes == 1 else f"{minutes} minutos"


def _describe_rate_limit(text: str, who_bare: str, status: int | None) -> FriendlyError:
    """HTTP 429. Con senal de tope de gasto: limite del USUARIO (vale para todos los modelos pagos) o POR MODELO, con el tiempo de
    espera que dice el proveedor; sin ella, simple exceso de pedidos seguidos. `text` ya viene en minusculas."""
    if _has_any(text, _QUOTA):
        window = _SPEND_WINDOW_RE.search(text)
        wait = f"unos {_minutes_text(int(window.group(1)))}" if window else "unos minutos"
        if not _PER_MODEL_SPEND_RE.search(text):
            return FriendlyError(
                "MODELO-LIMITE-USUARIO",
                "Llegaste al tope de consumo del proveedor de IA por un rato: es por usuario y vale para "
                "todos los modelos pagos. Para seguir ahora, elegí un modelo gratuito de la empresa "
                f"(por ejemplo DeepSeek) en el selector de arriba; o esperá {wait}.",
                status,
            )
        return FriendlyError(
            "MODELO-LIMITE",
            f"Llegaste al límite de uso del {who_bare} por un rato (El proveedor de IA pone un tope de consumo "
            f"por modelo). Para seguir ahora, elegí otro modelo en el selector de arriba; "
            f"o esperá {wait} y volvé a intentar con este.",
            status,
        )
    return FriendlyError(
        "MODELO-OCUPADO",
        "El servicio de inteligencia artificial recibió demasiados pedidos seguidos. "
        "Esperá unos segundos y volvé a enviar tu mensaje.",
        status,
    )


def _conversation_too_long(who: str, status: int | None) -> FriendlyError:
    """spec aviso-contexto-largo REQ-003: la conversacion no entra en la ventana del modelo. Mismo codigo y mismo texto para un 400/413/422 y para
    el error SIN codigo HTTP que llega por el streaming (caso real 2026-10-23: `openai.APIError: prompt is too long: 1015295 tokens > 1000000`)."""
    return FriendlyError(
        "MODELO-CONVERSACION-LARGA",
        f"Esta conversación ya es demasiado larga para {who}. "
        "Escribí /compact para resumirla y seguir, /clear para borrarla toda, o empezá una conversación nueva.",
        status,
    )


def _describe_rejected_request(text: str, status: int | None, who: str, who_cap: str) -> FriendlyError:
    """HTTP 400/413/422: el modelo no acepto el pedido. El orden importa y es el de siempre: conversacion demasiado larga, filtro de
    contenido, modelo sin herramientas, historial de herramientas, imagen y, si nada coincide, rechazo generico. `text` ya viene en minusculas."""
    if _has_any(text, _CONTEXT_TOO_LONG) or status == 413:
        return _conversation_too_long(who, status)
    if _has_any(text, _CONTENT_FILTER):
        return FriendlyError(
            "MODELO-FILTRO",
            f"{who_cap} no quiso responder este pedido por sus reglas de seguridad. "
            "Probá pedirlo con otras palabras.",
            status,
        )
    if _has_any(text, _NO_TOOLS):
        return FriendlyError(
            "MODELO-SIN-HERRAMIENTAS",
            f"{who_cap} no sirve para chatear con ULTRON: no admite herramientas. "
            "Reenviar el mensaje no lo arregla; elegí otro modelo en el selector de arriba.",
            status,
        )
    if _has_any(text, _TOOL_HISTORY):
        return FriendlyError(
            "MODELO-HISTORIAL",
            f"{who_cap} no aceptó el historial de esta conversación (a veces pasa después de "
            "usar herramientas). Para destrabarla, cerrá y volvé a abrir ULTRON: la conversación "
            "se vuelve a cargar ordenada. Mientras tanto podés seguir eligiendo otro modelo "
            "en el selector de arriba.",
            status,
        )
    if _has_any(text, _IMAGE):
        return FriendlyError(
            "MODELO-IMAGEN",
            f"{who_cap} no pudo leer una de las imágenes adjuntas. Probá con otra imagen "
            "(JPG o PNG) o elegí otro modelo en el selector de arriba.",
            status,
        )
    return FriendlyError(
        "MODELO-RECHAZO",
        f"{who_cap} no aceptó este pedido tal como está. Volvé a enviar tu mensaje; si se repite, "
        "elegí otro modelo en el selector de arriba o cerrá y volvé a abrir ULTRON.",
        status,
    )


def describe_model_error(exc: BaseException, model: str | None = None) -> FriendlyError:
    """Traduce una falla al llamar al modelo (chat) a un mensaje entendible."""
    status = _status_of(exc)
    text = str(exc).lower()
    names = _type_names(exc)
    who = _model_label(model)
    who_cap = who[0].upper() + who[1:]
    who_bare = who.removeprefix("el ")  # "del modelo X", no "de el modelo X"

    if "providernotconfigured" in names:  # spec vision-generico REQ-001 (app/llm/client.py)
        return FriendlyError(
            "MODELO-CONFIGURACION",
            "ULTRON todavía no tiene configurado el servicio de inteligencia artificial. "
            "Avisá a quien administra ULTRON: tiene que completar el proveedor y la clave en la "
            "configuración (archivo .env) y volver a abrir ULTRON.",
            None,
        )
    if _has_any(text, _NO_CREDIT):
        return FriendlyError(
            "MODELO-SIN-CREDITO",
            "La cuenta del servicio de inteligencia artificial se quedó sin crédito, así que ningún modelo "
            "de ese servicio va a responder. Cargá crédito en la consola del proveedor (en Anthropic: "
            "console.anthropic.com, en Plans & Billing) o elegí otro proveedor en Configuración > "
            "Credenciales.",
            status,
        )
    if _is_timeout(names):
        return FriendlyError(
            "MODELO-DEMORA",
            "El servicio de inteligencia artificial tardó demasiado en responder. "
            "Volvé a enviar tu mensaje; si pasa seguido, elegí otro modelo en el selector de arriba.",
            status,
        )
    if _is_cut(names):
        # REQ-001 (spec fix-corte-proveedor): pedido del usuario, «aclara esto al usuario: el corte vino del otro lado (El proveedor de IA) a mitad de
        # respuesta, no de Vision». Texto EXACTO de la spec; «El proveedor de IA» no es jerga para el guardian (el usuario lo nombra asi).
        return FriendlyError(
            "MODELO-CORTE",
            "El proveedor de IA (el servicio de inteligencia artificial de la empresa) cortó la conexión en medio de la respuesta. "
            "No fue un error de ULTRON. Volvé a enviar tu mensaje; si pasa seguido, elegí otro modelo en el selector de arriba.",
            status,
        )
    if "proxyerror" in names or _has_any(text, _PROXY):
        # spec proxy-autoheal REQ-006 (incidente 2026-10-18): la salida a internet guardada dejo de responder. No es «revisa la VPN» a secas: la pantalla que la cura
        # (Configuracion > Red > Proxy > Reconectar) elige otra viva y la guarda.
        return FriendlyError(
            "MODELO-PROXY",
            "La salida a internet que usa ULTRON no responde, por eso no pude llegar al servicio de inteligencia artificial de la empresa. "
            "Revisá que estés conectado a la VPN de tu empresa y tocá «Reconectar» en Configuración > Red > Proxy; "
            "ahí ULTRON busca una salida que funcione y la guarda. Después volvé a enviar tu mensaje.",
            status,
        )
    if _is_network(names):
        return FriendlyError(
            "MODELO-SIN-CONEXION",
            "No pude conectarme con el servicio de inteligencia artificial. Revisá que la "
            "computadora tenga internet (y la VPN de tu empresa, si hace falta) y volvé a enviar "
            "tu mensaje.",
            status,
        )
    if status == 401 or "authenticationerror" in names or _has_any(text, _TOKEN_FETCH):
        return FriendlyError(
            "MODELO-SESION",
            "El servicio de inteligencia artificial no aceptó la clave configurada (puede que "
            "haya vencido o esté mal copiada). Avisá a quien administra ULTRON para que la revise.",
            status,
        )
    if status == 403:
        return FriendlyError(
            "MODELO-PERMISO",
            f"Tu usuario no tiene permiso para usar {who}. "
            "Elegí otro modelo en el selector de arriba y volvé a enviar tu mensaje.",
            status,
        )
    if status == 404:
        return FriendlyError(
            "MODELO-NO-DISPONIBLE",
            f"{who_cap} no está disponible en este momento. "
            "Elegí otro modelo en el selector de arriba y volvé a enviar tu mensaje.",
            status,
        )
    if status == 429:
        return _describe_rate_limit(text, who_bare, status)
    if status in (400, 413, 422):
        return _describe_rejected_request(text, status, who, who_cap)
    if status is not None and status >= 500:
        return FriendlyError(
            "MODELO-CAIDO",
            "El servicio de inteligencia artificial está con problemas en este momento. "
            "Esperá un minuto y volvé a enviar tu mensaje, o elegí otro modelo en el selector de arriba.",
            status,
        )
    if status is None and _has_any(text, _CONTEXT_TOO_LONG):
        # spec aviso-contexto-largo REQ-003: el SDK no pone codigo HTTP a un error que llega a mitad del streaming.
        return _conversation_too_long(who, status)
    return FriendlyError(
        "MODELO-DESCONOCIDO",
        "Ocurrió un problema inesperado al preparar la respuesta. Volvé a enviar tu mensaje; "
        "si se repite, avisá a soporte con el código del detalle técnico.",
        status,
    )


def describe_transcription_error(exc: BaseException) -> FriendlyError:
    """Falla de Whisper (voz a texto). Mensajes propios: aca no hay selector
    de modelo que ofrecer, pero siempre se puede escribir el mensaje."""
    status = _status_of(exc)
    names = _type_names(exc)
    if "providernotconfigured" in names:  # spec vision-generico REQ-003
        return FriendlyError(
            "VOZ-NO-CONFIGURADA",
            "El dictado por voz no está configurado en este ULTRON. Mientras tanto podés escribir "
            "el mensaje; para activarlo, avisá a quien administra ULTRON.",
            None,
        )
    if status == 401 or "authenticationerror" in names:
        return FriendlyError(
            "VOZ-SESION",
            "No pude pasar tu audio a texto porque el servicio de voz no aceptó la clave "
            "configurada. Avisá a quien administra ULTRON; mientras tanto podés escribir el mensaje.",
            status,
        )
    if status == 429:
        return FriendlyError(
            "VOZ-LIMITE",
            "El servicio que pasa la voz a texto está saturado o llegó a su límite por un rato. "
            "Probá de nuevo en unos minutos o escribí el mensaje.",
            status,
        )
    if _is_timeout(names) or _is_network(names):
        return FriendlyError(
            "VOZ-SIN-CONEXION",
            "No pude conectarme con el servicio que pasa la voz a texto. Revisá tu conexión (y la "
            "VPN de tu empresa, si hace falta) y probá de nuevo, o escribí el mensaje.",
            status,
        )
    return FriendlyError(
        "VOZ-FALLO",
        "No pude pasar tu audio a texto. Probá grabar de nuevo o escribí el mensaje.",
        status,
    )


def describe_connector_error(exc: BaseException, system: str) -> FriendlyError:
    """Falla de un conector (BigQuery, SharePoint, Oracle, SAP, Graph).
    `system`: nombre visible del sistema, tal cual lo ve el usuario."""
    status = _status_of(exc)
    text = str(exc).lower()
    names = _type_names(exc)
    if "proxyerror" in names or _has_any(text, _PROXY):
        return FriendlyError(
            "CONECTOR-PROXY",
            f"El proxy de tu empresa no deja pasar a {system}. Si es un servidor interno de la empresa, agregá .empresa.com en "
            "Configuración > Conexiones > Red y proxy, en «Sin proxy para», y probá de nuevo.",
            status,
        )
    if _is_timeout(names) or _has_any(text, _TIMEOUT):
        return FriendlyError(
            "CONECTOR-DEMORA",
            f"{system} tardó demasiado en responder. Revisá tu conexión (y la VPN de tu empresa, "
            "si hace falta) y probá de nuevo en unos minutos.",
            status,
        )
    if status == 401 or _has_any(text, _AUTH):
        return FriendlyError(
            "CONECTOR-CREDENCIALES",
            f"{system} no aceptó las credenciales guardadas (puede que hayan vencido o cambiado). "
            "Revisalas en Configuración > Conexiones.",
            status,
        )
    if status == 403 or _has_any(text, _FORBIDDEN):
        return FriendlyError(
            "CONECTOR-PERMISO",
            f"Tu usuario no tiene permiso para ver esto en {system}. "
            f"Pedile acceso a quien administra {system}.",
            status,
        )
    if status == 404 or _has_any(text, _NOT_FOUND):
        return FriendlyError(
            "CONECTOR-NO-ENCONTRADO",
            f"{system} no encontró lo que se pidió: puede que se haya borrado o cambiado de nombre. "
            "Revisá el nombre y probá de nuevo.",
            status,
        )
    if _is_network(names) or _has_any(text, _NETWORK):
        return FriendlyError(
            "CONECTOR-SIN-CONEXION",
            f"No pude conectarme con {system}. Revisá tu conexión (y la VPN de tu empresa, si hace "
            "falta) y probá de nuevo.",
            status,
        )
    return FriendlyError(
        "CONECTOR-FALLO",
        f"{system} devolvió un error inesperado. Probá de nuevo; si se repite, avisá a soporte.",
        status,
    )


def describe_os_error(exc: OSError) -> FriendlyError:
    """Falla de Windows al abrir un archivo (boton "Ejecutar" del chat)."""
    if isinstance(exc, FileNotFoundError):
        return FriendlyError(
            "ARCHIVO-NO-ENCONTRADO",
            "No encontré el archivo: puede que se haya movido, renombrado o borrado. "
            "Revisá que siga en su lugar o volvé a elegirlo.",
        )
    if isinstance(exc, PermissionError):
        return FriendlyError(
            "ARCHIVO-PERMISO",
            "Windows no dejó abrir el archivo: falta permiso o lo tiene abierto otro programa. "
            "Cerrá ese programa y probá de nuevo.",
        )
    # WinError 1155: "No hay ninguna aplicacion asociada con el archivo".
    if getattr(exc, "winerror", None) == 1155:
        return FriendlyError(
            "ARCHIVO-SIN-PROGRAMA",
            "Windows no tiene ningún programa elegido para abrir este tipo de archivo. "
            "Abrilo una vez a mano con doble clic y elegí con qué programa abrirlo.",
        )
    return FriendlyError(
        "ARCHIVO-FALLO",
        "Windows no pudo abrir el archivo. Probá abrirlo a mano con doble clic.",
    )


def describe_internal_error() -> FriendlyError:
    """Falla dentro de Vision (no del modelo) a mitad de un turno de chat."""
    return FriendlyError(
        "VISION-INTERNO",
        "ULTRON tuvo un problema interno y no pudo terminar esta respuesta. "
        "Volvé a enviar tu mensaje; si se repite, cerrá y volvé a abrir ULTRON.",
    )


def describe_step_limit(max_steps: int) -> FriendlyError:
    """Freno de seguridad de `MAX_STEPS` en el loop de herramientas."""
    return FriendlyError(
        "VISION-PASOS",
        f"Frené esta tarea porque ya llevaba {max_steps} pasos seguidos sin terminar "
        "(es un freno de seguridad para que no quede dando vueltas). Si querés que siga, "
        "escribí «continuá»; si no, probá pedirla en partes más chicas.",
    )


# -- Salto automatico de modelos (spec salto-automatico-modelos) ---------------
# Viven aca y no en app/llm/model_fallback.py para que el guardian de jerga de
# tests/test_friendly_errors.py los cubra junto con el resto de los mensajes.


def describe_all_models_paused(model: str, back_at: str) -> FriendlyError:
    """REQ-004: todos los modelos de la lista estan en pausa. `model` es el
    primero en liberarse y `back_at` su hora ("15:10")."""
    return FriendlyError(
        "MODELO-TODOS-EN-PAUSA",
        f"Todos los modelos de tu lista de prioridad llegaron a su límite o no responden. "
        f"El primero en liberarse es {model}, a las {back_at}. Esperá hasta esa hora o agregá "
        "otro modelo a la lista en Configuración > Modelo e inteligencia.",
    )


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def describe_local_context_too_short(engine_label: str, model: str, window: int, need: int, engine_id: str) -> FriendlyError:
    """spec ia-local REQ-011: el modelo local esta cargado con menos contexto que el pedido. Mandarlo igual hace que el motor
    recorte el principio SIN avisar (instrucciones y herramientas incluidas) y responda sin sentido, o nada (caso real
    2026-10-26: 4.096 de ventana, 11.078 tokens de pedido, 3 minutos «pensando» y ninguna respuesta)."""
    if engine_id == "ollama":
        fix = (
            "Subí el contexto de Ollama: abrí una terminal, escribí «setx OLLAMA_CONTEXT_LENGTH 16384», cerrá Ollama desde "
            "la bandeja (al lado del reloj) y volvé a abrirlo. Está explicado en el README, «Conexión a LLMs», paso 4."
        )
    else:
        fix = (
            "En LM Studio, volvé a cargar el modelo con un «Context Length» de 16384 o más (en la ventana de carga del "
            "modelo) y reenviá tu mensaje."
        )
    return FriendlyError(
        "MODELO-LOCAL-CONTEXTO-CORTO",
        f"{engine_label} tiene cargado {model} con {_thousands(window)} tokens de contexto, y este pedido necesita unos "
        f"{_thousands(need)}. Si lo mando así, el motor corta el principio sin avisar y la respuesta no sirve. {fix}",
    )


def describe_empty_answer(model: str, local: bool) -> str:
    """El modelo termino el turno sin escribir nada ni usar herramientas. Antes el chat quedaba en blanco, sin explicacion."""
    text = f"{model} terminó sin escribir ninguna respuesta. Reenviá tu mensaje o probá con otro modelo."
    if local:
        text += (
            " Con un modelo local suele ser falta de contexto: revisá el paso 4 de «Conexión a LLMs» en el README"
            " (OLLAMA_CONTEXT_LENGTH)."
        )
    return text


def describe_task_needs_new_message(model: str) -> FriendlyError:
    """REQ-009: el unico disponible es Gemini y la tarea en curso la empezo otro
    modelo (Gemini exige una firma de pensamiento que los demas no generan)."""
    return FriendlyError(
        "MODELO-SALTO-PENDIENTE",
        f"Frené esta tarea porque el único modelo disponible ahora es {model} y no puede "
        f"continuar una tarea que empezó otro modelo. Mandá tu próximo mensaje (por ejemplo "
        f"«continuá») y sigo con {model}.",
    )


# spec voz-fish-audio REQ-003/009: por que un motor de voz no pudo hablar y
# que hacer. `kind` sale de `app.voice.engines.VoiceEngineError`.
_VOICE_ENGINE_ERRORS: dict[str, tuple[str, str]] = {
    "sin_clave": (
        "VOZ-MOTOR-SIN-CLAVE",
        "{engine} no tiene su clave cargada. Cargala en {where}.",
    ),
    "clave_invalida": (
        "VOZ-MOTOR-CLAVE",
        "{engine} no aceptó la clave configurada. Revisala en {where}.",
    ),
    "sin_voz": (
        "VOZ-MOTOR-SIN-VOZ",
        "{engine} no tiene una voz elegida o la voz ya no existe. Elegila en Configuración > Audio.",
    ),
    "sin_credito": (
        "VOZ-MOTOR-CREDITO",
        "{engine} se quedó sin crédito. Cargá crédito en {engine} o cambiá el orden de las voces "
        "en Configuración > Audio.",
    ),
    "limite": (
        "VOZ-MOTOR-LIMITE",
        "{engine} está recibiendo demasiados pedidos. En unos minutos vuelvo a probarlo solo.",
    ),
    "sin_conexion": (
        "VOZ-MOTOR-SIN-CONEXION",
        "No pude conectarme con {engine}. Revisá tu conexión (y el proxy de tu empresa, si hace "
        "falta); en unos minutos vuelvo a probarlo solo.",
    ),
    "no_instalado": (
        "VOZ-LOCAL-NO-INSTALADA",
        "La clonación local de voz no está instalada. Instalala desde Configuración > Audio.",
    ),
    "sin_muestra": (
        "VOZ-LOCAL-SIN-MUESTRA",
        "Falta tu muestra de voz para la clonación local. Subila en Configuración > Audio.",
    ),
    "fallo": (
        "VOZ-MOTOR-FALLO",
        "{engine} no pudo generar el audio. En unos minutos vuelvo a probarlo solo.",
    ),
}


def describe_voice_engine_error(
    kind: str, engine: str, next_engine: str | None = None, status: int | None = None
) -> FriendlyError:
    """Mensaje para el chat cuando un motor de voz falla. `next_engine`: con
    que motor sigue hablando Vision (REQ-003: nunca queda mudo)."""
    code, template = _VOICE_ENGINE_ERRORS.get(kind, _VOICE_ENGINE_ERRORS["fallo"])
    from app import credentials  # import diferido: credentials importa config y conectores

    message = template.format(engine=engine, where=credentials.where_to_load("Voz"))  # gestor-conexiones: Credenciales o Conexiones
    if next_engine:
        message = f"{message} Mientras tanto sigo hablando con {next_engine}."
    return FriendlyError(code, message, status)


_SWITCH_REASONS = {
    "MODELO-LIMITE": "llegó a su límite de uso por un rato",
    "MODELO-LIMITE-USUARIO": "llegó al tope de consumo del servicio de inteligencia artificial",
    "MODELO-OCUPADO": "está recibiendo demasiados pedidos",
    "MODELO-CAIDO": "no está respondiendo",
    # spec varios-proveedores REQ-005
    "MODELO-SIN-CREDITO": "se quedó sin crédito (cargalo en la consola de ese servicio)",
}


def model_switch_notice(
    failed: str, code: str, next_model: str, back_at: str, *, next_not_secret: bool = False
) -> str:
    """REQ-002/007: aviso del chat cuando un modelo falla y Vision sigue con otro.
    Es informativo (no pide ninguna accion), por eso no es un FriendlyError."""
    reason = _SWITCH_REASONS.get(code, "no está respondiendo")
    text = f"{failed} {reason}. Sigo con {next_model} y vuelvo a probar {failed} a las {back_at}."
    if next_not_secret:
        text += f" Atención: {next_model} no está aprobado para datos secretos de tu empresa."
    return text


def model_recovered_notice(model: str) -> str:
    """REQ-003: vencio la pausa y se vuelve a usar el modelo."""
    return f"Ya pasó la pausa de {model}: vuelvo a usarlo."


def technical_summary(exc: BaseException, limit: int = 180) -> str:
    """Una linea corta con el texto crudo del error, para pantallas de
    configuracion (Conexiones) donde quien configura SI necesita el detalle.
    Nunca se usa en el chat."""
    raw = " ".join(str(exc).split()) or type(exc).__name__
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"
