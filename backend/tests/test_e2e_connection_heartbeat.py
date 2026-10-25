"""E2E del indicador de conexion en vivo (spec estado-conexion-en-vivo) en un Chromium REAL contra un backend real que SE APAGA y SE VUELVE A PRENDER.

Pedido del usuario (2026-10-19): «el status-dot y el conn-status-text no reflejan el verdadero status [...]. Ni bien se cierre la consola tendria que mostrarse en rojo».
Antes, el indicador se consultaba UNA sola vez al cargar la pagina: cerrar la consola del servidor lo dejaba verde para siempre.

Lo que ve la persona:
1. Con el servidor vivo: verde «Vision en línea».
2. Cierra la consola: en unos segundos (2 latidos fallidos seguidos) rojo con una frase que dice la causa probable.
3. Un solo fallo aislado NO lo pone rojo.
4. Vuelve el servidor: verde, sin recargar la pagina, y se reconsultan modelo y version.
5. Trabajando (ambar) el latido no lo pasa a verde; si el servidor murio, pasa a rojo.
6. Con la pestaña oculta no hay peticiones; al mostrarse hay una de inmediato. Nunca hay mas de un latido en vuelo y cada uno vence a los ~2,5 s.

Como se simula: el servidor es uvicorn real en loopback que el TEST apaga (`server.should_exit`) y vuelve a prender en el MISMO puerto, con la pagina abierta todo el rato.
La pestaña oculta se simula con `document.hidden` y el evento `visibilitychange`. LIMITE: no se cierra una consola de Windows real, solo el servidor que arranca el propio test."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from e2e_support import launch_chromium, open_app

pytestmark = pytest.mark.e2e

LOST_TEXT = "Sin conexión con Vision. ¿Cerraste la ventana del servidor?"
ONLINE_TEXT = "Vision en línea"

HIDDEN_HOOK = """
(() => {
  window.__hidden = false;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => window.__hidden });
  window.__setHidden = (h) => {
    window.__hidden = !!h;
    document.dispatchEvent(new Event("visibilitychange"));
  };
})();
"""


class ControlledBackend:
    """Backend real en un puerto FIJO que se puede apagar y volver a prender, como cerrar y abrir la consola."""

    def __init__(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn

        from app import main

        self._server = uvicorn.Server(
            uvicorn.Config(
                main.app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
                timeout_graceful_shutdown=1,
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline:
                pytest.fail("el backend no arrancó en 20 s")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
        self._server = None
        self._thread = None


@pytest.fixture()
def backend():
    be = ControlledBackend()
    be.start()
    try:
        yield be
    finally:
        be.stop()


@pytest.fixture()
def browser(tmp_path, backend):
    with launch_chromium(tmp_path, backend.url) as b:
        yield b


@pytest.fixture()
def app(browser, backend):
    ctx = open_app(browser, backend.url)
    a = ctx.__enter__()
    a.page.add_init_script(HIDDEN_HOOK)
    a.open()
    a.page.wait_for_function("document.getElementById('conn-status').dataset.state === 'online'", timeout=15_000)
    yield a
    ctx.__exit__(None, None, None)


def conn_state(app) -> str:
    return app.page.locator("#conn-status").get_attribute("data-state") or ""


def conn_text(app) -> str:
    return app.page.locator("#conn-status-text").inner_text()


def wait_conn(app, state: str, timeout: int = 20_000) -> None:
    app.page.wait_for_function(f"document.getElementById('conn-status').dataset.state === {state!r}", timeout=timeout)


def watch_status_requests(app) -> list[float]:
    """Hora de cada pedido a /api/status que sale de la pagina."""
    times: list[float] = []
    app.page.on("request", lambda r: times.append(time.monotonic()) if r.url.endswith("/api/status") else None)
    return times


def _page_errors_from_a_dead_server_are_expected(app) -> None:
    """Un servidor muerto hace fallar los pedidos de fondo: la pagina no debe tirar errores de JS por eso."""
    assert app.page_errors == []


# ------------------------------------------------------------------------------------------------------------------ REQ-001: vivo = verde
def test_it_is_green_and_says_online_while_the_server_is_alive(app):
    assert conn_state(app) == "online"
    assert conn_text(app) == ONLINE_TEXT


# ------------------------------------------------------------------------------------------------------------------ REQ-002: se cierra la consola = rojo
def test_closing_the_server_turns_the_indicator_red_with_the_cause_in_words(app, backend):
    backend.stop()

    wait_conn(app, "error", timeout=20_000)

    assert conn_text(app) == LOST_TEXT
    _page_errors_from_a_dead_server_are_expected(app)


def test_it_goes_red_within_a_few_seconds_not_minutes(app, backend):
    backend.stop()
    started = time.monotonic()

    wait_conn(app, "error", timeout=20_000)

    assert time.monotonic() - started < 14, (
        "ni bien se cierra la consola tiene que verse rojo (2 latidos de 3 s + plazo)"
    )


# ------------------------------------------------------------------------------------------------------------------ REQ-004: vuelve = verde sin recargar
def test_when_the_server_comes_back_it_turns_green_again_without_reloading(app, backend):
    app.page.evaluate("window.__sinRecarga = 'sigo aca'")
    backend.stop()
    wait_conn(app, "error", timeout=20_000)

    backend.start()
    wait_conn(app, "online", timeout=20_000)

    assert conn_text(app) == ONLINE_TEXT
    assert app.page.evaluate("window.__sinRecarga") == "sigo aca", "la pagina no se recargo"


def test_when_it_comes_back_the_model_and_version_are_asked_again(app, backend):
    backend.stop()
    wait_conn(app, "error", timeout=20_000)
    app.page.evaluate("document.getElementById('dev-version').textContent = 'vieja'")

    backend.start()
    wait_conn(app, "online", timeout=20_000)

    app.page.wait_for_function("document.getElementById('dev-version').textContent.startsWith('v')", timeout=10_000)
    assert app.page.inner_text("#dev-version") != "vieja"


# ------------------------------------------------------------------------------------------------------------------ REQ-003: un fallo suelto no asusta
def test_a_single_failed_beat_does_not_turn_it_red(app):
    fails = {"left": 1}

    def flaky(route):
        if fails["left"] > 0:
            fails["left"] -= 1
            route.abort("failed")
        else:
            route.continue_()

    app.page.route("**/api/status", flaky)
    seen: list[str] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        seen.append(conn_state(app))
        app.page.wait_for_timeout(200)

    assert fails["left"] == 0, "el fallo se provoco de verdad"
    assert "error" not in seen, seen
    assert conn_state(app) == "online"


# ------------------------------------------------------------------------------------------------------------------ REQ-005: ocupado
def test_while_working_the_beat_does_not_turn_it_green(app):
    app.page.evaluate(
        "() => { const el = document.getElementById('conn-status'); el.dataset.state = 'busy';"
        " document.getElementById('conn-status-text').textContent = 'Vision está trabajando…'; }"
    )

    app.page.wait_for_timeout(7_000)  # mas de 2 latidos con el servidor vivo

    assert conn_state(app) == "busy"
    assert conn_text(app) == "Vision está trabajando…"


def test_while_working_a_dead_server_still_turns_it_red(app, backend):
    app.page.evaluate(
        "() => { document.getElementById('conn-status').dataset.state = 'busy';"
        " document.getElementById('conn-status-text').textContent = 'Vision está trabajando…'; }"
    )
    backend.stop()

    wait_conn(app, "error", timeout=20_000)

    assert conn_text(app) == LOST_TEXT


# ------------------------------------------------------------------------------------------------------------------ REQ-006: pestaña oculta
def test_a_hidden_tab_makes_no_requests_and_showing_it_asks_right_away(app):
    app.page.evaluate("window.__setHidden(true)")
    app.page.wait_for_timeout(500)  # que termine un latido que ya estuviera saliendo
    times = watch_status_requests(app)

    app.page.wait_for_timeout(7_000)

    assert times == [], "oculta: ni un pedido"

    app.page.evaluate("window.__setHidden(false)")
    app.page.wait_for_timeout(700)

    assert len(times) >= 1, "al mostrarse, un latido inmediato (sin esperar los 3 s)"


def test_a_server_that_dies_while_the_tab_is_hidden_is_noticed_when_shown(app, backend):
    app.page.evaluate("window.__setHidden(true)")
    backend.stop()
    app.page.wait_for_timeout(7_000)
    assert conn_state(app) == "online", "oculta: no se entera hasta que se muestre"

    app.page.evaluate("window.__setHidden(false)")

    wait_conn(app, "error", timeout=20_000)


# ------------------------------------------------------------------------------------------------------------------ REQ-007: uno solo en vuelo, con plazo
def test_there_is_never_more_than_one_beat_in_flight_and_each_one_expires(app):
    flight = {"now": 0, "max": 0}
    started: dict[str, float] = {}
    durations: list[float] = []

    def on_request(r):
        if r.url.endswith("/api/status"):
            flight["now"] += 1
            flight["max"] = max(flight["max"], flight["now"])
            started[r.url + str(id(r))] = time.monotonic()
            started[id(r)] = time.monotonic()

    def on_done(r):
        if r.url.endswith("/api/status"):
            flight["now"] -= 1
            t0 = started.get(id(r))
            if t0 is not None:
                durations.append(time.monotonic() - t0)

    held = []
    app.page.route("**/api/status", lambda route: held.append(route))  # se cuelga: no responde nunca
    app.page.on("request", on_request)
    app.page.on("requestfailed", on_done)
    app.page.on("requestfinished", on_done)

    app.page.wait_for_timeout(12_000)

    assert held, "el latido salio"
    assert flight["max"] <= 1, "nunca dos latidos a la vez"
    assert durations, "los latidos colgados vencieron por plazo"
    assert max(durations) < 3.5, f"cada latido vence a los ~2,5 s: {durations}"
    assert conn_state(app) == "error", "dos latidos colgados seguidos = sin conexion"


# ------------------------------------------------------------------------------------------------------------------ REQ-009: accesible
def test_the_indicator_is_announced_politely_and_says_the_state_in_words(app, backend):
    assert app.page.locator("#conn-status").get_attribute("aria-live") == "polite"

    backend.stop()
    wait_conn(app, "error", timeout=20_000)

    assert conn_text(app).strip() != "", "el estado no depende solo del color"


# ------------------------------------------------------------------------------------------------------------------ REQ-008: lo que ya andaba
def test_a_chat_that_loses_the_network_still_turns_it_red_right_away(app):
    app.page.route("**/api/chat", lambda route: route.abort("failed"))
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")

    app.page.wait_for_function("document.getElementById('conn-status').dataset.state === 'error'", timeout=10_000)

    assert conn_text(app).startswith("Sin conexión con Vision")


def test_the_page_does_not_throw_js_errors_while_the_server_is_gone(app, backend):
    backend.stop()
    wait_conn(app, "error", timeout=20_000)
    app.page.wait_for_timeout(7_000)

    _page_errors_from_a_dead_server_are_expected(app)


# ------------------------------------------------------------------------------------------------------------------ cierra mutaciones M4 y M6
def test_showing_the_tab_while_a_beat_is_still_hanging_does_not_start_a_second_one(app):
    """Con plazo de 2,5 s e intervalo de 3 s los latidos del reloj no se pisan; el unico solapamiento posible es el inmediato al mostrar la pestaña."""
    held = []
    app.page.route("**/api/status", lambda route: held.append(route))  # se cuelga: no responde
    times = watch_status_requests(app)
    deadline = time.monotonic() + 8
    while not times and time.monotonic() < deadline:
        app.page.wait_for_timeout(50)
    assert len(times) == 1, "salio el primer latido"

    app.page.evaluate("window.__setHidden(false)")  # 'volvio a verse' con ese latido todavia en vuelo
    app.page.wait_for_timeout(400)

    assert len(times) == 1, "con un latido en vuelo no sale otro"


def test_a_success_between_two_failures_resets_the_count_and_never_turns_it_red(app):
    """fallo, acierto, fallo, acierto...: nunca hay 2 fallos SEGUIDOS, asi que nunca rojo (si el acierto no reiniciara la cuenta, el 2.º fallo lo pondria rojo)."""
    calls = {"n": 0}

    def alternate(route):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            route.abort("failed")
        else:
            route.continue_()

    app.page.route("**/api/status", alternate)
    seen: list[str] = []
    deadline = time.monotonic() + 16
    while time.monotonic() < deadline:
        seen.append(conn_state(app))
        app.page.wait_for_timeout(200)

    assert calls["n"] >= 4, f"hubo al menos dos fallos y dos aciertos: {calls['n']}"
    assert "error" not in seen, seen


# ------------------------------------------------------------------------------------------------------------------ cierra mutacion M13
def test_a_server_that_answers_with_an_error_counts_as_not_alive(app):
    """El servidor responde pero roto (503): no esta sano. Con `alive = true` fijo, solo un corte de red lo pondria rojo."""
    app.page.route("**/api/status", lambda route: route.fulfill(status=503, body="caido"))

    wait_conn(app, "error", timeout=20_000)

    assert conn_text(app) == LOST_TEXT
    app.page.unroute("**/api/status")
    wait_conn(app, "online", timeout=20_000)
