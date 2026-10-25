"""E2E de Configuracion > Red (spec proxy-autoheal REQ-007/008) en un Chromium REAL contra el backend real, con el mundo de red FALSO (`proxy_health` sin sockets).

Lo que ve la persona:
1. La pestana que se llamaba «Servidor» ahora es «Red» y trae DOS secciones: Servidor (lo de siempre, intacto) y Proxy.
2. Proxy muestra lo que se esta usando, lo guardado y la lista de salidas encontradas con su estado (responde / no responde).
3. «Probar» una direccion no cambia nada; «Guardar y reconectar» la aplica; «Reconectar» corre el chequeo completo y cuenta en palabras que paso.
4. Nada de texto crudo ni de contrasenas: lo que falla se ve como frase."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import proxy_health as ph
from app import secret_store
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows"),
]

MODEL = "claude-sonnet-4-6"


class Provider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **k: iter([])))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}


class World:
    def __init__(self):
        self.alive: set[tuple[str, int]] = set()
        self.dns: list[str] = []


@pytest.fixture(scope="module")
def world():
    return World()


@pytest.fixture(scope="module")
def base_url(world):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ph, "_tcp_alive", lambda host, port: 0.012 if (host, port) in world.alive else None)
        mp.setattr(ph, "_resolve", lambda host: list(world.dns))
        mp.setattr(ph, "_pac_proxies", lambda: [])
        with serve_backend(Provider()) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("proxy"), base_url) as b:
        yield b


@pytest.fixture(autouse=True)
def _reset(world, monkeypatch):
    world.alive = set()
    world.dns = []
    ph.reset_for_tests()
    # ULTRON viene sin proxy predeterminado; estos e2e (heredados) prueban la pantalla con una empresa que SI tiene uno.
    monkeypatch.setattr(ph, "DEFAULT_PROXY", "http://proxy.empresa.com:83")
    monkeypatch.setattr(ph, "_FALLBACK_NO_PROXY", ".empresa.com,empresa.com,19.0.0.0/8,10.0.0.0/8")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    yield
    ph.reset_for_tests()


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_network_tab(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="server"]')
    # la lista vacia se oculta a proposito (`.proxy-list:empty`): se espera a que EXISTA, y a que el boton Reconectar (siempre visible) se pueda tocar
    app.page.wait_for_selector('.settings-pane[data-pane="server"] #cfg-proxy-list', state="attached", timeout=10_000)
    app.page.wait_for_selector("#cfg-proxy-reconnect", state="visible", timeout=10_000)


def wait_status(app, contains: str):
    app.page.wait_for_function(
        "(t) => document.getElementById('cfg-proxy-status').textContent.includes(t)", arg=contains, timeout=15_000
    )


def rows(app) -> list[str]:
    return app.page.eval_on_selector_all(
        "#cfg-proxy-list .proxy-row", "els => els.map(e => e.textContent.replace(/\\s+/g, ' ').trim())"
    )


def test_the_tab_is_called_red_and_keeps_the_server_section_intact(app):
    open_network_tab(app)

    assert app.page.inner_text('.settings-tab[data-tab="server"]').strip() == "Red"
    assert app.page.locator("#cfg-server-url").is_visible() and app.page.locator("#cfg-server-test").is_visible()
    assert app.page.locator("#cfg-server-save").is_visible()
    assert "Servidor" in app.page.inner_text('.settings-pane[data-pane="server"] [data-section="server"] h3')
    assert "Proxy" in app.page.inner_text('.settings-pane[data-pane="server"] [data-section="proxy"] h3')


def test_the_proxy_section_lists_what_was_found_with_its_state(app, world):
    world.dns = ["19.12.80.237", "19.12.64.237"]
    world.alive = {("19.12.80.237", 83), ("login.microsoftonline.com", 443)}
    open_network_tab(app)

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "Listo")
    lines = rows(app)

    assert any("19.12.80.237:83" in r and "responde" in r and "no responde" not in r for r in lines)
    assert any("19.12.64.237:83" in r and "no responde" in r for r in lines)
    assert any("Conexión directa" in r for r in lines)


def test_reconnect_with_nothing_wrong_says_so_in_plain_words(app, world):
    world.dns = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}
    open_network_tab(app)

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "Listo")

    text = app.page.inner_text("#cfg-proxy-status")
    assert "Traceback" not in text and "ProxyError" not in text


def test_reconnect_with_no_way_out_explains_it_and_points_to_the_vpn(app, world):
    open_network_tab(app)

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "VPN")

    assert app.page.locator("#cfg-proxy-status.error").count() == 1


def test_test_one_address_reports_it_without_changing_what_is_in_use(app, world):
    world.alive = {("10.1.1.1", 83)}
    open_network_tab(app)
    before = app.page.inner_text("#cfg-proxy-inuse")

    app.page.fill("#cfg-proxy-address", "10.1.1.1:83")
    app.page.click("#cfg-proxy-test")
    wait_status(app, "responde")

    assert app.page.inner_text("#cfg-proxy-inuse") == before


def test_test_one_dead_address_says_it_does_not_answer(app, world):
    open_network_tab(app)

    app.page.fill("#cfg-proxy-address", "10.9.9.9:83")
    app.page.click("#cfg-proxy-test")
    wait_status(app, "no responde")

    assert app.page.locator("#cfg-proxy-status.error").count() == 1


def test_an_address_that_is_not_a_proxy_is_explained_not_sent(app):
    open_network_tab(app)

    app.page.fill("#cfg-proxy-address", "esto no es una direccion")
    app.page.click("#cfg-proxy-test")
    wait_status(app, "no es válida")

    assert "422" not in app.page.inner_text("#cfg-proxy-status")


def test_save_and_reconnect_applies_a_live_address_now(app, world):
    world.alive = {("10.1.1.1", 83)}
    open_network_tab(app)

    app.page.fill("#cfg-proxy-address", "10.1.1.1:83")
    app.page.click("#cfg-proxy-save")
    wait_status(app, "Listo")

    assert "10.1.1.1:83" in app.page.inner_text("#cfg-proxy-inuse")


def test_save_and_reconnect_refuses_a_dead_address_and_keeps_what_was_in_use(app, world):
    open_network_tab(app)
    before = app.page.inner_text("#cfg-proxy-inuse")

    app.page.fill("#cfg-proxy-address", "10.9.9.9:83")
    app.page.click("#cfg-proxy-save")
    wait_status(app, "no responde")

    assert app.page.inner_text("#cfg-proxy-inuse") == before


def test_buttons_are_disabled_while_working_so_a_double_click_cannot_double_the_work(app, world):
    open_network_tab(app)

    app.page.evaluate(
        "() => { window.__calls = 0; const f = window.fetch; window.fetch = (u, o) => { if (String(u).includes('/api/proxy/reconnect')) window.__calls++; return f(u, o); }; }"
    )
    app.page.dblclick("#cfg-proxy-reconnect")
    wait_status(app, "VPN")

    assert app.page.evaluate("window.__calls") == 1


def test_a_failing_backend_shows_a_sentence_not_a_status_code(app):
    open_network_tab(app)
    app.page.route(
        "**/api/proxy/reconnect",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body='{"detail": "No se pudo revisar la salida a internet. Probá de nuevo."}',
        ),
    )

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "No se pudo revisar")

    assert "500" not in app.page.inner_text("#cfg-proxy-status")


def test_no_password_is_ever_drawn(app, world, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://usuario:clave-secreta@19.12.80.237:83")
    world.alive = {("19.12.80.237", 83)}
    open_network_tab(app)

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "Listo")

    assert "clave-secreta" not in app.page.content()


def test_a_kept_saved_proxy_is_explained_when_vision_goes_direct(app, world):
    """0.33.1: guardado que no responde + salida directa: «Guardada» y «En uso» difieren a proposito y la pantalla lo dice (no parece un error)."""
    from app import config_store as cs

    cs.set_general({"https_proxy": "http://19.12.64.237:83"})
    world.alive = {("login.microsoftonline.com", 443)}
    open_network_tab(app)

    app.page.click("#cfg-proxy-reconnect")
    wait_status(app, "Listo")

    saved = app.page.inner_text("#cfg-proxy-saved")
    assert "19.12.64.237:83" in saved and "se conserva" in saved
    assert cs.get_general().get("https_proxy") == "http://19.12.64.237:83", "no se borro"
