"""Mensajes de error para personas NO tecnicas (`app/friendly_errors.py`).

Pedido del usuario (2026-09-24): "mejora el manejo de errores para que los
mensajes lo entiendan las personas no tecnicas". Caso real que lo disparo: el
chat mostraba "El modelo rechazo la solicitud (HTTP 400): suele deberse a un
parametro de generacion o un schema de herramientas...".

Contrato que se prueba:
- cada falla real (el proveedor de IA, Whisper, conectores, Windows) cae en la categoria
  correcta, clasificada por el codigo HTTP REAL y no por numeros sueltos del
  texto (la version anterior confundia un 500 que adentro dice "400");
- el texto que ve el usuario no tiene jerga tecnica, no copia el error crudo
  y siempre dice que hacer.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import openai
import pytest

from app.connectors.sharepoint_connector import SharePointConnectionError
from app.friendly_errors import (
    FriendlyError,
    describe_all_models_paused,
    describe_connector_error,
    describe_internal_error,
    describe_model_error,
    describe_os_error,
    describe_step_limit,
    describe_task_needs_new_message,
    describe_transcription_error,
    model_recovered_notice,
    model_switch_notice,
    technical_summary,
)

pytestmark = pytest.mark.unit

_REQUEST = SimpleNamespace(method="POST", url="https://proveedor.test/v1/chat/completions")
# Texto REAL del 400 de Gemini del 2026-09-24 (backend/logs/app.log, sesion 8619176d).
GEMINI_400 = (
    "Error code: 400 - {'error': {'code': 'http_400', 'message': '{\"object\": \"error\", "
    "\"message\": \"The request was rejected by the model as invalid.\", \"detail\": \"400 Bad "
    "Request. {\\'message\\': \\'{\\\\n  \\\"error\\\": {\\\\n    \\\"code\\\": 400,\\\\n    "
    "\\\"message\\\": \\\"Please ensure that the number of function response parts is equal to "
    "the number of function call parts of the function call turn.\\\",\\\\n    \\\"status\\\": "
    "\\\"INVALID_ARGUMENT\\\"\\\\n  }\\\\n}\\\\n\\', \\'status\\': \\'Bad Request\\'}\"}', "
    "'param': '', 'type': 'invalid_request_error'}}"
)
# Formato real del limite de gasto (la documentacion del proveedor, troubleshooting.md).
SPEND_429 = (
    "Error code: 429 - {'detail': 'Too much spend: model claude-sonnet-4-6 is limited to $50 "
    "per 600 seconds. Contact the el proveedor de IA team if you need a higher limit.'}"
)
USER_SPEND_429 = (
    "Error code: 429 - {'error': {'code': 'rate_limit_exceeded', 'message': \"Too many requests: you've reached the $3 per 600 seconds spend limit. This short-term guardrail resets automatically within a few minutes. On-prem models are free and not spend-limited — consider switching to one to keep working.\", 'param': '', 'type': 'rate_limit_error'}}"
)
SENTINEL = "SENTINEL-TEXTO-CRUDO-XYZ"


def status_error(cls: type[openai.APIStatusError], status: int, text: str) -> openai.APIStatusError:
    """Excepcion real del SDK con un `response` minimo (sin depender de httpx)."""
    response = SimpleNamespace(status_code=status, headers={}, request=_REQUEST)
    return cls(text, response=response, body=None)


# -- clasificacion de fallas del modelo ---------------------------------------

MODEL_CASES = [
    ("sesion-401", status_error(openai.AuthenticationError, 401, "Error code: 401 - expired"), "MODELO-SESION"),
    ("permiso-403", status_error(openai.PermissionDeniedError, 403, "Error code: 403 - no"), "MODELO-PERMISO"),
    ("no-existe-404", status_error(openai.NotFoundError, 404, "Error code: 404 - model"), "MODELO-NO-DISPONIBLE"),
    ("limite-gasto", status_error(openai.RateLimitError, 429, SPEND_429), "MODELO-LIMITE"),
    ("ocupado-429", status_error(openai.RateLimitError, 429, "Error code: 429 - slow down"), "MODELO-OCUPADO"),
    # Texto EXACTO del log de la prueba real del usuario (2026-09-25 15:37:47).
    ("tope-usuario-real", status_error(openai.RateLimitError, 429, USER_SPEND_429), "MODELO-LIMITE-USUARIO"),
    ("gemini-historial", status_error(openai.BadRequestError, 400, GEMINI_400), "MODELO-HISTORIAL"),
    (
        "claude-id-invalido",
        status_error(openai.BadRequestError, 400, "messages.3.content.0.tool_use.id: String should match pattern"),
        "MODELO-HISTORIAL",
    ),
    (
        "contexto-largo",
        status_error(openai.BadRequestError, 400, "This model's maximum context length is 200000 tokens"),
        "MODELO-CONVERSACION-LARGA",
    ),
    ("contexto-413", status_error(openai.APIStatusError, 413, "Error code: 413 - too large"), "MODELO-CONVERSACION-LARGA"),
    (
        "filtro",
        status_error(openai.BadRequestError, 400, "The response was filtered: content_filter triggered"),
        "MODELO-FILTRO",
    ),
    (
        "imagen",
        status_error(openai.BadRequestError, 400, "Invalid image_url: could not decode"),
        "MODELO-IMAGEN",
    ),
    ("rechazo-generico", status_error(openai.BadRequestError, 400, "Unsupported parameter: top_k"), "MODELO-RECHAZO"),
    (
        # el proveedor de IA envuelve errores del proveedor: un 500 que ADENTRO dice
        # "400 Bad Request" es una falla del servicio, no un rechazo del pedido.
        "500-que-menciona-400",
        status_error(openai.InternalServerError, 500, "Error code: 500 - {'detail': '500: 400 Invalid data'}"),
        "MODELO-CAIDO",
    ),
    ("503", status_error(openai.InternalServerError, 503, "Error code: 503 - unavailable"), "MODELO-CAIDO"),
    ("timeout-sdk", openai.APITimeoutError(request=_REQUEST), "MODELO-DEMORA"),
    ("timeout-python", TimeoutError(), "MODELO-DEMORA"),
    ("sin-conexion-sdk", openai.APIConnectionError(request=_REQUEST), "MODELO-SIN-CONEXION"),
    ("sin-conexion-python", ConnectionRefusedError("[WinError 10061]"), "MODELO-SIN-CONEXION"),
    (
        "login-proveedor",
        RuntimeError("Error retrieving the token - Failed to refresh access token (401)"),
        "MODELO-SESION",
    ),
    # Regresion: la version anterior buscaba "400" en el texto -> "4001" matcheaba.
    ("numero-suelto", ValueError("se enviaron 4001 tokens"), "MODELO-DESCONOCIDO"),
    ("desconocido", KeyError("choices"), "MODELO-DESCONOCIDO"),
]


@pytest.mark.parametrize(("exc", "code"), [c[1:] for c in MODEL_CASES], ids=[c[0] for c in MODEL_CASES])
def test_model_errors_are_classified_by_real_cause(exc: BaseException, code: str) -> None:
    assert describe_model_error(exc, "gemini-3.1-pro").code == code


def test_spend_limit_says_which_model_and_how_long_to_wait() -> None:
    msg = describe_model_error(status_error(openai.RateLimitError, 429, SPEND_429), "claude-sonnet-4-6").message
    assert "límite de uso del modelo claude-sonnet-4-6" in msg  # "del", nunca "de el"
    assert "10 minutos" in msg  # 600 segundos del propio mensaje del proveedor de IA
    assert "otro modelo" in msg


def test_spend_limit_without_window_still_gives_a_wait_hint() -> None:
    exc = status_error(openai.RateLimitError, 429, "Error code: 429 - Too much spend: model x")
    assert "unos minutos" in describe_model_error(exc, "x").message


def test_gemini_history_rejection_tells_how_to_unblock_the_conversation() -> None:
    msg = describe_model_error(status_error(openai.BadRequestError, 400, GEMINI_400), "gemini-3.1-pro").message
    assert "gemini-3.1-pro" in msg
    assert "volvé a abrir Vision" in msg  # recargar desde disco destraba (replay real 2026-09-24: 200)
    assert "otro modelo" in msg


def test_long_conversation_points_to_compact_command() -> None:
    exc = status_error(openai.BadRequestError, 400, "prompt is too long: 250000 tokens > 200000 maximum")
    assert "/compact" in describe_model_error(exc, "claude-sonnet-4-6").message


def test_without_model_name_the_message_is_still_a_sentence() -> None:
    msg = describe_model_error(status_error(openai.PermissionDeniedError, 403, "no"), None).message
    assert msg.startswith("Tu usuario no tiene permiso para usar el modelo elegido.")


def test_technical_line_has_code_status_model_and_time() -> None:
    friendly = describe_model_error(status_error(openai.BadRequestError, 400, GEMINI_400), "gemini-3.1-pro")
    assert friendly.technical(model="gemini-3.1-pro", when="21:45:49") == (
        "MODELO-HISTORIAL · HTTP 400 · gemini-3.1-pro · 21:45:49"
    )
    assert FriendlyError("VISION-INTERNO", "x").technical() == "VISION-INTERNO"


# -- voz, conectores, Windows ---------------------------------------------------

@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (status_error(openai.AuthenticationError, 401, "expired"), "VOZ-SESION"),
        (status_error(openai.RateLimitError, 429, "Too much spend"), "VOZ-LIMITE"),
        (openai.APITimeoutError(request=_REQUEST), "VOZ-SIN-CONEXION"),
        (openai.APIConnectionError(request=_REQUEST), "VOZ-SIN-CONEXION"),
        (RuntimeError("gateway timeout"), "VOZ-FALLO"),
    ],
    ids=["sesion", "limite", "timeout", "conexion", "otro"],
)
def test_transcription_errors(exc: BaseException, code: str) -> None:
    friendly = describe_transcription_error(exc)
    assert friendly.code == code
    assert "escrib" in friendly.message  # siempre queda la salida de escribir el mensaje


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TimeoutError(), "CONECTOR-DEMORA"),
        (RuntimeError("ORA-12170: TNS:Connect timeout occurred"), "CONECTOR-DEMORA"),
        (RuntimeError("ORA-01017: invalid username/password; logon denied"), "CONECTOR-CREDENCIALES"),
        (RuntimeError("AADSTS7000215: Invalid client secret provided"), "CONECTOR-CREDENCIALES"),
        (RuntimeError("403 Access Denied: User does not have bigquery.datasets.get permission"), "CONECTOR-PERMISO"),
        (RuntimeError("404 Not found: Dataset empresa-prj:ventas"), "CONECTOR-NO-ENCONTRADO"),
        (ConnectionRefusedError("[WinError 10061] conexion rechazada"), "CONECTOR-SIN-CONEXION"),
        (RuntimeError("DPY-6005: cannot connect to database"), "CONECTOR-SIN-CONEXION"),
        # `SharePointConnectionError` envuelve CUALQUIER falla de SharePoint:
        # un 500 del servidor no es "revisa la VPN".
        (SharePointConnectionError("Graph devolvio HTTP 500 Internal Server Error"), "CONECTOR-FALLO"),
    ],
    ids=["timeout", "ora-timeout", "ora-login", "aad-secret", "bq-403", "bq-404", "refused", "dpy", "sp-500"],
)
def test_connector_errors(exc: BaseException, code: str) -> None:
    friendly = describe_connector_error(exc, "BigQuery")
    assert friendly.code == code
    assert "BigQuery" in friendly.message


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FileNotFoundError(2, "No such file", "C:/x.xlsx"), "ARCHIVO-NO-ENCONTRADO"),
        (PermissionError(13, "Permission denied", "C:/x.xlsx"), "ARCHIVO-PERMISO"),
        (OSError(22, "No hay aplicacion asociada", "C:/x.abc", 1155), "ARCHIVO-SIN-PROGRAMA"),
        (OSError(5, "Otro"), "ARCHIVO-FALLO"),
    ],
    ids=["no-existe", "permiso", "sin-programa", "otro"],
)
def test_os_errors(exc: OSError, code: str) -> None:
    assert describe_os_error(exc).code == code


def test_step_limit_mentions_the_limit_and_how_to_continue() -> None:
    friendly = describe_step_limit(100)
    assert friendly.code == "VISION-PASOS"
    assert "100 pasos" in friendly.message and "continuá" in friendly.message


def test_technical_summary_is_one_short_line() -> None:
    assert technical_summary(RuntimeError("a\n  b\tc")) == "a b c"
    long = technical_summary(RuntimeError("x" * 500), limit=50)
    assert len(long) == 50 and long.endswith("…")
    assert technical_summary(RuntimeError()) == "RuntimeError"


# -- guardian de jerga: aplica a TODOS los mensajes -----------------------------

def _all_messages() -> list[tuple[str, str]]:
    raw = SENTINEL
    model_excs = [c[1] for c in MODEL_CASES] + [
        status_error(openai.BadRequestError, 400, f"{raw} function call"),
        status_error(openai.RateLimitError, 429, f"Too much spend {raw} per 60 seconds"),
        RuntimeError(raw),
    ]
    out: list[tuple[str, str]] = []
    for exc in model_excs:
        friendly = describe_model_error(exc, "gemini-3.1-pro")
        out.append((friendly.code, friendly.message))
    for exc in (
        status_error(openai.AuthenticationError, 401, raw),
        status_error(openai.RateLimitError, 429, raw),
        openai.APITimeoutError(request=_REQUEST),
        RuntimeError(raw),
    ):
        friendly = describe_transcription_error(exc)
        out.append((friendly.code, friendly.message))
    for exc in (
        TimeoutError(raw),
        RuntimeError(f"ORA-01017 {raw}"),
        RuntimeError(f"403 {raw}"),
        RuntimeError(f"404 {raw}"),
        ConnectionRefusedError(raw),
        RuntimeError(raw),
    ):
        friendly = describe_connector_error(exc, "Oracle")
        out.append((friendly.code, friendly.message))
    for exc in (
        FileNotFoundError(raw),
        PermissionError(raw),
        OSError(22, raw, None, 1155),
        OSError(raw),
    ):
        friendly = describe_os_error(exc)
        out.append((friendly.code, friendly.message))
    for friendly in (
        describe_internal_error(),
        describe_step_limit(100),
        # spec salto-automatico-modelos REQ-004/REQ-009
        describe_all_models_paused("claude-opus-5-5", "15:10"),
        describe_task_needs_new_message("gemini-3.1-pro"),
    ):
        out.append((friendly.code, friendly.message))
    return out


ALL_MESSAGES = _all_messages()
# Palabras que una persona no tecnica no tiene por que conocer.
_JARGON = re.compile(
    r"\bhttps?\b|\b[45]\d\d\b|schema|payload|upstream|token_cache|json|traceback|stack|"
    r"exception|excepci[oó]n|error code|base64|\bapi\b|endpoint|gateway|\bstatus\b|"
    r"\bnull\b|\bnone\b|invalid|request",
    re.IGNORECASE,
)
# Toda frase le dice al usuario que puede hacer (voseo, como el resto de la UI).
_ACTION = re.compile(
    r"\b(volvé|elegí|esperá|probá|cerrá|revisá|revisalas|escribí|pedile|abrilo|avisá|mandá)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(("code", "message"), ALL_MESSAGES, ids=[f"{i}-{c}" for i, (c, _) in enumerate(ALL_MESSAGES)])
def test_message_has_no_jargon_no_raw_text_and_an_action(code: str, message: str) -> None:
    assert not _JARGON.search(message), f"{code}: jerga tecnica en {message!r}"
    assert SENTINEL not in message, f"{code}: se filtro el texto crudo del error"
    assert _ACTION.search(message), f"{code}: no dice que hacer: {message!r}"
    assert not message.lower().startswith("error"), f"{code}: arranca con 'Error'"
    assert message.endswith("."), f"{code}: no termina como oracion"
    assert re.fullmatch(r"[A-Z]+(-[A-Z]+)+", code), f"codigo de soporte con formato raro: {code}"


# spec salto-automatico-modelos REQ-002/003/007: avisos INFORMATIVOS del chat (no
# piden ninguna accion, por eso no se les exige verbo) -- igual sin jerga.
NOTICES = [
    model_switch_notice("claude-opus-5-5", "MODELO-LIMITE", "gpt-6-sol", "15:10"),
    model_switch_notice("claude-opus-5-5", "MODELO-CAIDO", "modelo-no-apto", "15:10", next_not_secret=True),
    model_recovered_notice("claude-opus-5-5"),
]


@pytest.mark.parametrize("notice", NOTICES, ids=["limite", "caida-no-apto", "vuelve"])
def test_fallback_notices_have_no_jargon(notice: str) -> None:
    assert not _JARGON.search(notice), notice
    assert notice.endswith(".")


def test_real_user_level_spend_limit_explains_the_wait_and_the_way_out() -> None:
    msg = describe_model_error(status_error(openai.RateLimitError, 429, USER_SPEND_429), "claude-sonnet-5").message
    assert "tope de consumo" in msg and "10 minutos" in msg
    assert "otro modelo" in msg
