"""REQ-007 (spec fix-detener-y-progreso-visible): Detener nunca queda «Deteniendo…» sin limite, en un Chromium REAL contra el backend real.

Lo que vivio el usuario (2026-10-11, 22:49-22:57): el `uvicorn --reload` de desarrollo reinicio Vision a mitad de un turno. El proceso viejo
siguio con el turno pero ya no aceptaba conexiones: el POST de Detener quedo colgado SIN respuesta (el fetch no tenia plazo y el vigilante
arrancaba recien cuando el POST contestaba) y el boton quedo en «Deteniendo…» sin limite, sin ninguna explicacion.

Contrato observable que se prueba (no la implementacion):
  1. Si el pedido de Detener no contesta en 5 s, aparece UNA vez «Vision no responde todavía: puede estar reiniciándose. Sigo intentando…»;
     el boton NO vuelve a «Detener» (el pedido pudo haber llegado) y vuelve a «Enviar» cuando el turno termina, con «Listo: Vision ya no está
     trabajando.».
  2. Si Vision no se puede contactar (Detener y el estado fallan) y despues vuelve SIN el turno (como un proceso recien reiniciado), el boton
     vuelve a «Enviar» con «Listo…», aunque el stream del turno siga abierto.
  3. Si a los ~30 s sigue sin poder contactar a Vision, sugiere recargar con Ctrl+Shift+R y suelta el boton (nunca «Deteniendo…» sin limite);
     mientras tanto reintento el Detener cada ~5 s (no una sola vez) y la nota de «no responde» sale una sola vez.

`page.route` simula el servidor que no contesta / no conecta; el turno real lo mantiene vivo una herramienta que espera un `threading.Event`
(el test decide cuando "termina sola"). El proveedor de IA es falso (nunca hay red ni login, ADR-015).
"""

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
TOOL_MAX_S = 90  # tope de la herramienta si nadie la libera (el teardown siempre la libera antes)
UNREACHABLE = "ULTRON no responde todavía: puede estar reiniciándose. Sigo intentando…"
RECOVERED = "Listo: ULTRON ya no está trabajando."
RELOAD_HINT = "Ctrl+Shift+R"


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class Provider:
    """Pide la herramienta `wait_gate` en el primer paso; con un resultado de herramienta en el historial, contesta texto."""

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
        call = SimpleNamespace(index=0, id="call_gate", function=SimpleNamespace(name="wait_gate", arguments="{}"))
        return iter([_chunk(tool_calls=[call]), _chunk(finish="tool_calls")])


@pytest.fixture(scope="module")
def gate_holder():
    """La herramienta espera el Event VIGENTE al momento de correr; cada test pone uno nuevo y lo libera al terminar."""
    holder = SimpleNamespace(event=threading.Event())

    def execute(args, session):
        holder.event.wait(timeout=TOOL_MAX_S)
        return "datos"

    registry = get_registry()
    registry.register(
        Tool(
            name="wait_gate",
            description="herramienta de prueba que espera a que el test la libere",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        )
    )
    yield holder
    holder.event.set()
    registry._tools.pop("wait_gate", None)


@pytest.fixture()
def gate(gate_holder):
    gate_holder.event = threading.Event()
    yield gate_holder.event
    gate_holder.event.set()  # el turno del backend termina solo; no queda ningun hilo esperando


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider, gate_holder):
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
        # pedidos que el test dejo colgados a proposito: no son errores de la pagina
        a.page.unroute_all(behavior="ignoreErrors")


def _send_and_wait_tool(app):
    app.page.fill("#chat-input", "hace la tarea larga")
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    # el modelo falso ya pidio la herramienta y esta corriendo (su fila aparece en el chat)
    app.page.wait_for_selector(".tool-call-row", timeout=15_000, state="attached")


def _button_state(app) -> str:
    return app.page.get_attribute("#send-btn", "data-state")


def _count_notes(app, fragment: str) -> int:
    return sum(fragment in note for note in app.notes())


def _wait_note(app, fragment: str, timeout_ms: int) -> None:
    app.page.wait_for_function(
        "frag => [...document.querySelectorAll('.msg.system-note')].some(e => e.textContent.includes(frag))",
        arg=fragment,
        timeout=timeout_ms,
    )


def test_a_stop_without_answer_warns_in_seconds_and_the_button_returns_when_the_turn_ends(app, gate):
    hanging = []
    app.page.route("**/api/chat/*/stop", lambda route: hanging.append(route))  # nunca contesta (proceso viejo que ya no atiende)
    _send_and_wait_tool(app)

    started = time.monotonic()
    app.page.click("#send-btn")  # Detener
    app.page.wait_for_selector("#send-btn[data-state=stopping]", timeout=5_000)
    _wait_note(app, UNREACHABLE, timeout_ms=10_000)
    elapsed = time.monotonic() - started

    assert 4 <= elapsed <= 9, f"la nota tiene que salir al vencer el plazo de 5 s del pedido (salio a los {elapsed:.1f} s)"
    assert _button_state(app) == "stopping", "el pedido pudo haber llegado: el boton no vuelve a «Detener»"
    assert hanging, "el Detener tiene que haberse pedido"

    gate.set()  # el turno termina solo (como el proceso viejo que termina el turno que tenia)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
    _wait_note(app, RECOVERED, timeout_ms=10_000)

    assert app.page.inner_text("#send-btn-label") == "Enviar"
    assert _count_notes(app, UNREACHABLE) == 1


def test_when_vision_comes_back_without_the_turn_the_button_returns_to_send(app, gate):
    down = {"on": True}

    def stop_route(route):
        route.abort("connectionrefused")  # Vision caida: el pedido ni siquiera conecta

    def status_route(route):
        if down["on"]:
            route.abort("connectionrefused")
            return
        # Vision "volvio" como un proceso recien reiniciado: no conoce el turno (el stream viejo, en cambio, sigue abierto).
        resp = route.fetch()
        body = resp.json()
        body.update(busy=False, active_turn_id=None)
        route.fulfill(response=resp, json=body)

    app.page.route("**/api/chat/*/stop", stop_route)
    _send_and_wait_tool(app)
    app.page.route("**/api/session/*/status", status_route)

    app.page.click("#send-btn")  # Detener
    app.page.wait_for_selector("#send-btn[data-state=stopping]", timeout=5_000)
    _wait_note(app, UNREACHABLE, timeout_ms=8_000)
    assert _button_state(app) == "stopping", "una falla de red no devuelve el boton a «Detener»: el pedido pudo haber llegado"

    time.sleep(4)  # Vision sigue sin contestar unos segundos: el boton espera y sigue intentando
    assert _button_state(app) == "stopping"

    down["on"] = False
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=10_000)
    _wait_note(app, RECOVERED, timeout_ms=5_000)

    assert app.page.inner_text("#send-btn-label") == "Enviar"
    assert _count_notes(app, UNREACHABLE) == 1


@pytest.mark.timeout(180)
def test_if_vision_stays_unreachable_it_retries_and_after_30_s_suggests_reloading(app, gate):
    stops = []

    def stop_route(route):
        stops.append(time.monotonic())
        route.abort("connectionrefused")

    app.page.route("**/api/chat/*/stop", stop_route)
    _send_and_wait_tool(app)
    app.page.route("**/api/session/*/status", lambda route: route.abort("connectionrefused"))

    started = time.monotonic()
    app.page.click("#send-btn")  # Detener
    _wait_note(app, UNREACHABLE, timeout_ms=8_000)
    _wait_note(app, RELOAD_HINT, timeout_ms=45_000)
    elapsed = time.monotonic() - started

    assert 25 <= elapsed <= 40, f"la sugerencia de recargar va a los ~30 s sin contacto (salio a los {elapsed:.1f} s)"
    # Nunca «Deteniendo…» sin limite: el boton queda otra vez disponible. El stream del turno sigue abierto en esta pagina, asi que vuelve a
    # «Detener» (se puede volver a intentar); la nota explica que hacer.
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=3_000)
    assert len(stops) >= 4, f"el Detener se reintenta cada ~5 s mientras no hay respuesta (se pidio {len(stops)} veces)"
    gaps = [b - a for a, b in zip(stops, stops[1:], strict=False)]
    assert all(g >= 3 for g in gaps), f"reintentos demasiado seguidos: {gaps}"
    assert _count_notes(app, UNREACHABLE) == 1, "la nota de «no responde» se muestra una sola vez"
    assert _count_notes(app, RELOAD_HINT) == 1
