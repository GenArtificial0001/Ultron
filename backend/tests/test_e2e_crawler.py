"""Recorrido automatico de la app escuchando errores de JavaScript (BL-015).

Abre Vision en un Chromium REAL contra el backend real, recorre cada pestana de Configuracion y los paneles laterales, y falla si en cualquier momento aparece un error de
JavaScript no atrapado (`pageerror`), un `console.error` o un pedido HTTP propio que termine en error del servidor (5xx). Es un detector de la clase de bugs que ningun test
dirigido ve: una pantalla que se rompe en silencio al abrirse (un `undefined.x`, un id de elemento que ya no existe despues de un cambio de HTML).

No necesita saber de antemano QUE pantallas existen: las descubre del HTML (`.settings-tab[data-tab]`). Si alguien agrega una pestana nueva, queda cubierta sola.

El proveedor de IA es falso: nunca hay red ni login del proveedor de IA (ADR-015). Se ignoran los errores de recursos EXTERNOS (fuentes, CDN) que no son de Vision.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"

# Ruido conocido que NO es un bug de Vision. Cada entrada lleva el porque; una excepcion nueva se agrega aca a proposito, con su motivo, no con un `except` general.
IGNORED_CONSOLE = (
    # Chromium de prueba sin camara/mic reales: getUserMedia/Permissions pueden avisar. No es un error de la app.
    "Permissions policy violation",
)


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class FakeProvider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        return iter([_chunk("[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])


@pytest.fixture(scope="module")
def provider():
    return FakeProvider()


@pytest.fixture(scope="module")
def base_url(provider):
    from app.llm import loop as loop_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


class Watch:
    """Junta todo lo que la pagina reporta como problema mientras se la recorre."""

    def __init__(self, page, base_url):
        self.problems: list[str] = []
        self.base_url = base_url
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("console", self._console)
        page.on("response", self._response)

    def _console(self, msg):
        if msg.type != "error":
            return
        text = msg.text
        if any(skip in text for skip in IGNORED_CONSOLE):
            return
        # "Failed to load resource" de un recurso EXTERNO (no del backend de Vision) no es un bug nuestro.
        loc = getattr(msg, "location", None) or {}
        url = loc.get("url", "") if isinstance(loc, dict) else ""
        if text.startswith("Failed to load resource") and url and not url.startswith(self.base_url):
            return
        self.problems.append(f"console.error: {text} ({url})")

    def _response(self, response):
        if response.url.startswith(self.base_url) and response.status >= 500:
            self.problems.append(
                f"HTTP {response.status}: {response.request.method} {response.url.removeprefix(self.base_url)}"
            )


@pytest.fixture()
def crawl(browser, base_url):
    ctx = open_app(browser, base_url)
    app = ctx.__enter__()
    watch = Watch(app.page, base_url)
    app.open()
    app.page.wait_for_timeout(500)
    yield app, watch
    ctx.__exit__(None, None, None)


def settings_tabs(app) -> list[str]:
    return app.page.evaluate("[...document.querySelectorAll('.settings-tab[data-tab]')].map(t => t.dataset.tab)")


def test_the_app_loads_without_any_javascript_error(crawl):
    app, watch = crawl

    assert watch.problems == [], "\n".join(watch.problems)
    assert app.page_errors == []


def test_every_settings_tab_opens_without_errors(crawl):
    app, watch = crawl
    app.page.click("#settings-btn")
    tabs = settings_tabs(app)
    assert len(tabs) >= 8, f"se esperaban al menos 8 pestanas de Configuracion y se encontraron {tabs}"

    for tab in tabs:
        app.page.click(f'.settings-tab[data-tab="{tab}"]')
        app.page.wait_for_selector(f'.settings-pane[data-pane="{tab}"]:not(.hidden)', timeout=5_000)
        app.page.wait_for_timeout(300)  # tiempo para que terminen los pedidos que dispara abrir esa pestana

    assert watch.problems == [], "\n".join(watch.problems)


def test_every_settings_tab_can_be_reopened_in_any_order(crawl):
    """Abrir una pestana dos veces (o en otro orden) no rompe: varias cargan datos al abrirse y reabrir no debe duplicar ni fallar."""
    app, watch = crawl
    app.page.click("#settings-btn")
    tabs = settings_tabs(app)

    for tab in [*tabs, *reversed(tabs)]:
        app.page.click(f'.settings-tab[data-tab="{tab}"]')
        app.page.wait_for_selector(f'.settings-pane[data-pane="{tab}"]:not(.hidden)', timeout=5_000)
    app.page.wait_for_timeout(500)

    assert watch.problems == [], "\n".join(watch.problems)


def test_sending_a_message_and_getting_the_answer_raises_no_errors(crawl):
    app, watch = crawl
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=30_000)
    app.page.wait_for_timeout(500)

    assert watch.problems == [], "\n".join(watch.problems)


def test_the_crawler_really_notices_a_broken_screen(crawl):
    """Prueba del propio detector: si una pantalla lanza un error, este recorrido tiene que enterarse. Sin esto un detector 'mudo' pasaria siempre en verde."""
    app, watch = crawl

    app.page.evaluate("setTimeout(() => { throw new Error('pantalla rota a proposito'); }, 0)")
    app.page.wait_for_timeout(300)

    assert any("pantalla rota a proposito" in p for p in watch.problems)
    app.page_errors.clear()  # lo provoque yo: sin esto el cierre de `App` (que ya vigila los errores de JS) fallaria el test


def test_the_crawler_really_notices_a_server_error(crawl, base_url):
    app, watch = crawl
    app.page.route("**/api/status", lambda route: route.fulfill(status=503, body="caido"))

    app.page.evaluate("fetch('/api/status').then(() => 0)")
    app.page.wait_for_timeout(300)

    assert any("HTTP 503" in p for p in watch.problems)
