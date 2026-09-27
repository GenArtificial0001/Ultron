"""E2E de mensajes de error en un Chromium REAL contra el backend real (pedido del
usuario 2026-09-24: "mejora el manejo de errores para que los mensajes lo
entiendan las personas no tecnicas").

Lo que se ve en pantalla cuando algo falla: nunca "HTTP 500", "Failed to fetch"
ni el texto crudo del proveedor; siempre que paso y que hacer, con el codigo de
soporte plegado en "Detalle tecnico". Y las fallas que antes eran MUDAS (lista
vacia, "Guardado ✓" aunque fallara, microfono que no hace nada) ahora avisan.

El error del modelo recorre el camino completo: el proveedor de IA fake -> loop -> SSE ->
burbuja. Los cortes de red y los status HTTP se simulan con `page.route`.
Infraestructura compartida en `e2e_support.py`.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import openai
import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SPEND_429 = f"Error code: 429 - Too much spend: model {MODEL} is limited to $50 per 600 seconds. RAW-PROVEEDOR"
OFFLINE = "No hay conexión con Vision."
# Lo que NUNCA debe aparecer en el texto principal de un error.
JARGON = re.compile(r"HTTP|\b[45]\d\d\b|Failed to fetch|TypeError|RAW-PROVEEDOR|Error code|undefined|\[object", re.I)


class FakeProvider:
    """Cliente del proveedor de IA falso: roster de modelos, chat (que puede fallar) y Whisper."""

    def __init__(self):
        self.chat_exc: BaseException | None = None
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        if self.chat_exc is not None:
            raise self.chat_exc
        delta = SimpleNamespace(content="[[TIPO: conversacional]]Hola", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=stop, finish_reason="stop")]),
            ]
        )


@pytest.fixture(scope="module")
def proveedor():
    return FakeProvider()


@pytest.fixture(scope="module")
def base_url(proveedor):
    # El loop importa `get_client` por su cuenta: se reemplaza tambien ahi.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url, proveedor):
    proveedor.chat_exc = None
    proveedor.whisper.exc = None
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def send(app, text: str = "hola") -> None:
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")


def last_error(app) -> dict:
    """Espera la burbuja de error y devuelve su texto, detalle tecnico y tipo."""
    app.page.wait_for_selector(".msg.error")
    return app.page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('.msg.error')].at(-1);
          const details = el.querySelector('details.msg-error-technical');
          return {
            text: el.querySelector('.msg-text').textContent,
            technical: details ? details.querySelector('code').textContent : null,
            technicalOpen: details ? details.open : null,
            summary: details ? details.querySelector('summary').textContent : null,
            kind: el.dataset.errorKind,
            solved: el.classList.contains('msg-solved'),
          };
        }"""
    )


def revalidate_last_error(app) -> str:
    app.page.locator(".msg.error .btn-reset").last.click()
    app.page.wait_for_function(
        "() => [...document.querySelectorAll('.msg.error .msg-text')].at(-1).textContent.includes('Vision')"
    )
    app.page.wait_for_function("() => !document.querySelector('.msg.error .btn-reset.copied')")
    return app.page.evaluate("() => [...document.querySelectorAll('.msg.error .msg-text')].at(-1).textContent")


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
def test_model_limit_reaches_the_chat_in_plain_language_with_support_code(app, proveedor):
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    proveedor.chat_exc = openai.RateLimitError(SPEND_429, response=response, body=None)
    send(app)

    error = last_error(app)
    # El codigo de soporte existe pero plegado, aparte del mensaje. El modelo es
    # el default del backend (la pestaña no eligio ninguno en el selector).
    assert error["summary"] == "Detalle técnico" and error["technicalOpen"] is False
    match = re.fullmatch(r"MODELO-LIMITE · HTTP 429 · (\S+) · \d\d:\d\d:\d\d", error["technical"])
    assert match, error["technical"]
    assert error["text"].startswith(f"Llegaste al límite de uso del modelo {match.group(1)} por un rato")
    assert "10 minutos" in error["text"] and "otro modelo" in error["text"]
    assert not JARGON.search(error["text"]), error["text"]


def test_revalidating_a_model_error_never_claims_it_was_solved(app, proveedor):
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    proveedor.chat_exc = openai.RateLimitError(SPEND_429, response=response, body=None)
    send(app)
    last_error(app)

    text = revalidate_last_error(app)

    # Antes: "✓ Solucionado: ..." con solo que el backend respondiera.
    assert "Solucionado" not in text and "volvió a responder" not in text
    assert "volvé a enviar tu mensaje para reintentar" in text
    assert last_error(app)["solved"] is False


def test_lost_connection_is_explained_and_revalidation_confirms_recovery(app):
    app.page.route("**/api/chat", lambda route: route.abort("failed"))
    send(app)

    error = last_error(app)
    assert error["text"].startswith(OFFLINE)
    assert not JARGON.search(error["text"]), error["text"]
    assert error["kind"] == "offline"
    assert error["technical"].startswith("TypeError")  # el detalle crudo, plegado
    assert app.page.text_content("#conn-status-text") == "Sin conexión con Vision"

    # El backend sigue vivo (solo se corto /api/chat): revalidar lo confirma.
    text = revalidate_last_error(app)
    assert text == "✓ Vision volvió a responder. Ya podés seguir."
    assert last_error(app)["solved"] is True


def test_server_error_without_detail_is_explained(app):
    app.page.route(
        "**/api/chat",
        lambda route: route.fulfill(status=500, body="Internal Server Error", content_type="text/plain"),
    )
    send(app)

    error = last_error(app)
    assert error["text"].startswith("Vision tuvo un problema interno.")
    assert not JARGON.search(error["text"]), error["text"]
    assert error["technical"] == "HTTP 500"


def test_error_bubble_keeps_its_technical_detail_after_reload(app, proveedor):
    response = SimpleNamespace(status_code=429, headers={}, request=None)
    proveedor.chat_exc = openai.RateLimitError(SPEND_429, response=response, body=None)
    send(app)
    before = last_error(app)
    assert before["technical"], "la burbuja nueva debe traer su Detalle técnico"

    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    after = last_error(app)
    assert after["text"] == before["text"]
    assert after["technical"] == before["technical"]


# ---------------------------------------------------------------------------
# Voz (antes: fallas mudas)
# ---------------------------------------------------------------------------
def test_dictation_transcription_failure_is_explained(app, proveedor):
    proveedor.whisper.exc = RuntimeError("gateway timeout RAW-PROVEEDOR")
    app.start_listening("#voice-mic-btn")
    app.page.wait_for_timeout(600)  # algo de audio grabado
    app.page.click("#voice-mic-btn")

    app.wait_note("No pude pasar tu audio a texto.")
    note = next(n for n in app.notes() if "No pude pasar tu audio a texto." in n)
    assert "escribí el mensaje" in note
    assert not JARGON.search(note), note


def test_dictation_without_mic_permission_is_explained(app):
    app.page.evaluate("window.__gumFail = 'NotAllowedError'")
    app.page.click("#voice-mic-btn")

    app.wait_note("Vision no tiene permiso para usar el micrófono.")
    assert app.page.get_attribute("#voice-mic-btn", "data-listening") == "false"


def test_brain_mode_without_mic_permission_does_not_promise_to_wait_for_a_mic(app):
    app.page.evaluate("window.__gumFail = 'NotAllowedError'")
    app.page.click("#brain-btn")

    app.wait_note("Modo Cerebro: Vision no tiene permiso para usar el micrófono.")
    assert not any("apenas conectes uno" in n for n in app.notes())
    app.page.keyboard.press("Escape")


# ---------------------------------------------------------------------------
# Configuracion (antes: lista vacia sin aviso / "Guardado ✓" aunque fallara)
# ---------------------------------------------------------------------------
def open_settings_tab(app, tab: str) -> None:
    app.page.click("#settings-btn")
    app.page.click(f'.settings-tab[data-tab="{tab}"]')
    app.page.wait_for_selector(f'.settings-pane[data-pane="{tab}"]:not(.hidden)')


def test_failed_model_settings_save_is_not_reported_as_saved(app):
    app.page.route(
        "**/api/config/generation",
        lambda route: route.fulfill(status=500, body="boom")
        if route.request.method == "POST"
        else route.continue_(),
    )
    open_settings_tab(app, "model")
    app.page.click("#cfg-model-save")

    app.page.wait_for_function("() => document.querySelector('#cfg-model-status').classList.contains('error')")
    status = app.page.text_content("#cfg-model-status")
    assert "Guardado" not in status
    assert status.startswith("Vision tuvo un problema interno.")


def test_connector_listing_error_is_shown_instead_of_an_empty_list(app):
    detail = "Tu usuario no tiene permiso para ver esto en BigQuery. Pedile acceso a quien administra BigQuery."
    app.page.route(
        "**/api/config/connections/bigquery/datasets",
        lambda route: route.fulfill(status=502, body=json.dumps({"detail": detail}), content_type="application/json"),
    )
    open_settings_tab(app, "connections")
    app.page.click("#conn-bq-datasets-toggle")

    app.page.wait_for_selector("#conn-bq-datasets-list .conn-tree-error")
    assert app.page.text_content("#conn-bq-datasets-list .conn-tree-error") == detail


def test_validation_error_never_shows_object_object(app):
    # 422 de FastAPI: `detail` es una LISTA de objetos, no un texto.
    body = json.dumps({"detail": [{"loc": ["body", "name"], "msg": "field required", "type": "missing"}]})
    app.page.route(
        "**/api/config/generation",
        lambda route: route.fulfill(status=422, body=body, content_type="application/json")
        if route.request.method == "POST"
        else route.continue_(),
    )
    open_settings_tab(app, "model")
    app.page.click("#cfg-model-save")

    app.page.wait_for_function("() => document.querySelector('#cfg-model-status').classList.contains('error')")
    status = app.page.text_content("#cfg-model-status")
    assert status.startswith("Falta un dato o alguno no tiene el formato esperado.")
    assert not JARGON.search(status), status
