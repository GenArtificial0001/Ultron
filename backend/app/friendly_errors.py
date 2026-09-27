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
_QUOTA = ("too much spend", "spend limit")
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
# Falla del login ANTES de llamar al modelo (proveedores con OAuth, ej. un
# gateway corporativo detras de Azure AD / MSAL).
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


def _model_label(model: str | None) -> str:
    return f"el modelo {model}" if model else "el modelo elegido"


def _minutes_text(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return "1 minuto" if minutes == 1 else f"{minutes} minutos"


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
            "Vision todavía no tiene configurado el servicio de inteligencia artificial. "
            "Avisá a quien administra Vision: tiene que completar el proveedor y la clave en la "
            "configuración (archivo .env) y volver a abrir Vision.",
            None,
        )
    if _is_timeout(names):
        return FriendlyError(
            "MODELO-DEMORA",
            "El servicio de inteligencia artificial tardó demasiado en responder. "
            "Volvé a enviar tu mensaje; si pasa seguido, elegí otro modelo en el selector de arriba.",
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
            "haya vencido o esté mal copiada). Avisá a quien administra Vision para que la revise.",
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
        if _has_any(text, _QUOTA):
            window = _SPEND_WINDOW_RE.search(text)
            wait = f"unos {_minutes_text(int(window.group(1)))}" if window else "unos minutos"
            if not _PER_MODEL_SPEND_RE.search(text):
                return FriendlyError(
                    "MODELO-LIMITE-USUARIO",
                    "Llegaste al tope de consumo del servicio de inteligencia artificial por un rato. "
                    f"Esperá {wait} y volvé a enviar tu mensaje, o elegí otro modelo en el selector "
                    "de arriba.",
                    status,
                )
            return FriendlyError(
                "MODELO-LIMITE",
                f"Llegaste al límite de uso del {who_bare} por un rato (el servicio pone un tope de "
                f"consumo por modelo). Para seguir ahora, elegí otro modelo en el selector de arriba; "
                f"o esperá {wait} y volvé a intentar con este.",
                status,
            )
        return FriendlyError(
            "MODELO-OCUPADO",
            "El servicio de inteligencia artificial recibió demasiados pedidos seguidos. "
            "Esperá unos segundos y volvé a enviar tu mensaje.",
            status,
        )
    if status in (400, 413, 422):
        if _has_any(text, _CONTEXT_TOO_LONG) or status == 413:
            return FriendlyError(
                "MODELO-CONVERSACION-LARGA",
                f"Esta conversación ya es demasiado larga para {who}. "
                "Escribí /compact para resumirla y seguir, o empezá una conversación nueva.",
                status,
            )
        if _has_any(text, _CONTENT_FILTER):
            return FriendlyError(
                "MODELO-FILTRO",
                f"{who_cap} no quiso responder este pedido por sus reglas de seguridad. "
                "Probá pedirlo con otras palabras.",
                status,
            )
        if _has_any(text, _TOOL_HISTORY):
            return FriendlyError(
                "MODELO-HISTORIAL",
                f"{who_cap} no aceptó el historial de esta conversación (a veces pasa después de "
                "usar herramientas). Para destrabarla, cerrá y volvé a abrir Vision: la conversación "
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
            "elegí otro modelo en el selector de arriba o cerrá y volvé a abrir Vision.",
            status,
        )
    if status is not None and status >= 500:
        return FriendlyError(
            "MODELO-CAIDO",
            "El servicio de inteligencia artificial está con problemas en este momento. "
            "Esperá un minuto y volvé a enviar tu mensaje, o elegí otro modelo en el selector de arriba.",
            status,
        )
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
            "El dictado por voz no está configurado en este Vision. Mientras tanto podés escribir "
            "el mensaje; para activarlo, avisá a quien administra Vision.",
            None,
        )
    if status == 401 or "authenticationerror" in names:
        return FriendlyError(
            "VOZ-SESION",
            "No pude pasar tu audio a texto porque el servicio de voz no aceptó la clave "
            "configurada. Avisá a quien administra Vision; mientras tanto podés escribir el mensaje.",
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
        "Vision tuvo un problema interno y no pudo terminar esta respuesta. "
        "Volvé a enviar tu mensaje; si se repite, cerrá y volvé a abrir Vision.",
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


def describe_task_needs_new_message(model: str) -> FriendlyError:
    """REQ-009: el unico disponible es Gemini y la tarea en curso la empezo otro
    modelo (Gemini exige una firma de pensamiento que los demas no generan)."""
    return FriendlyError(
        "MODELO-SALTO-PENDIENTE",
        f"Frené esta tarea porque el único modelo disponible ahora es {model} y no puede "
        f"continuar una tarea que empezó otro modelo. Mandá tu próximo mensaje (por ejemplo "
        f"«continuá») y sigo con {model}.",
    )


_SWITCH_REASONS = {
    "MODELO-LIMITE": "llegó a su límite de uso por un rato",
    "MODELO-LIMITE-USUARIO": "llegó al tope de consumo del servicio de inteligencia artificial",
    "MODELO-OCUPADO": "está recibiendo demasiados pedidos",
    "MODELO-CAIDO": "no está respondiendo",
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
