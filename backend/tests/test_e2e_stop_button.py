"""E2E de Detener TAJANTE (pedido del usuario 2026-10-05: «el boton Detener se queda pegado sin detener, debe ser tajante»).

Dos causas distintas, dos pruebas en un Chromium REAL contra el backend real:
  1. Una herramienta lenta que no sabe cortarse (consulta a Oracle/SAP/..., aca `slow_query`): antes el turno esperaba a que terminara aunque
     el usuario hubiera apretado Detener. Ahora el boton vuelve a «Enviar» a los pocos segundos (plazo de gracia + un poco).
  2. El Detener que no llega al servidor (o el evento SSE 'stopped' que se pierde): el boton quedaba en «Deteniendo…» para siempre. Ahora un vigilante
     consulta el estado de la sesion, reintenta el Detener y devuelve el boton a «Enviar» solo.

El proveedor de IA es falso (nunca hay red ni login, ADR-015)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from app.tools import get_registry
from app.tools.base import Tool
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SLOW_TOOL_S = 40  # lo que tardaria la consulta si nadie la cortara
BUTTON_DEADLINE_MS = 12_000  # plazo de gracia (2 s) + vigilante (1 s) + margen: mucho menos que SLOW_TOOL_S
RETRY_DEADLINE_MS = 25_000    # reintento del vigilante (6 s) + plazo de gracia (2 s) + vigilante (1 s) + margen


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class Provider:
    """Pide la herramienta lenta en el primer paso; si ya hay un resultado de herramienta, contesta texto."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            return iter([_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])
        call = SimpleNamespace(index=0, id="call_slow", function=SimpleNamespace(name="slow_query", arguments="{}"))
        return iter([_chunk(tool_calls=[call]), _chunk(finish="tool_calls")])


@pytest.fixture(scope="module")
def slow_query_tool():
    finished = threading.Event()

    def execute(args, session):
        time.sleep(SLOW_TOOL_S)
        finished.set()
        return "datos"

    registry = get_registry()
    registry.register(
        Tool(
            name="slow_query",
            description="consulta lenta de prueba",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
    )
    yield finished
    registry._tools.pop("slow_query", None)


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider, slow_query_tool):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def _send_and_wait_tool(app):
    app.page.fill("#chat-input", "hace la consulta lenta")
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    # la herramienta ya esta corriendo: el modelo falso la pidio y su fila aparece en el chat
    app.page.wait_for_selector(".tool-call-row", timeout=15_000, state="attached")


def test_stop_while_a_slow_tool_runs_returns_the_button_to_send_in_seconds(app):
    _send_and_wait_tool(app)

    started = time.monotonic()
    app.page.click("#send-btn")  # Detener
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=BUTTON_DEADLINE_MS)

    assert time.monotonic() - started < SLOW_TOOL_S / 2, "el Detener no puede esperar a que termine la consulta"
    assert app.page.inner_text("#send-btn-label") == "Enviar"


def test_if_the_first_stop_never_reaches_the_server_the_watchdog_retries_and_stops_it(app):
    """El primer POST /stop se pierde (el navegador cree que paro, el servidor no se entero). Sin el vigilante el boton quedaba en «Deteniendo…»
    para siempre; ahora consulta el estado, ve que sigue ocupada y REINTENTA el Detener."""
    lost = {"n": 0}

    def swallow_first(route):
        if lost["n"] == 0:
            lost["n"] += 1
            route.fulfill(status=200, content_type="application/json", body='{"stopped": true}')  # miente: nunca llego al servidor
        else:
            route.continue_()

    app.page.route("**/api/chat/*/stop", swallow_first)
    _send_and_wait_tool(app)

    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=stopping]", timeout=5_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=RETRY_DEADLINE_MS)

    assert lost["n"] == 1 and app.page.inner_text("#send-btn-label") == "Enviar"


def test_after_stopping_the_user_can_send_a_new_message(app):
    _send_and_wait_tool(app)
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=BUTTON_DEADLINE_MS)

    app.page.fill("#chat-input", "otra pregunta")
    app.page.click("#send-btn")

    app.page.wait_for_function(
        "[...document.querySelectorAll('.msg.user')].some(m => m.innerText.includes('otra pregunta'))", timeout=10_000
    )
