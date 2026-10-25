"""REQ-014 (spec fix-detener-y-progreso-visible): lo que el usuario VE de la cola, en un Chromium REAL contra el backend real.

Como se encola un mensaje desde la pagina: el boton Enviar no deja mandar mientras Vision trabaja, pero las opciones de una pregunta
(tool `question`) si; el turno sigue respondiendo despues de mostrar la pregunta, asi que tocar una opcion ENCOLA ese mensaje
(«En cola · posición N»). Mismo camino que el Modo Cerebro o un comando propio mandados con Vision ocupada.

Contrato observable que se prueba (no la implementacion):
  (c) borrar con 🗑 un mensaje que sigue en cola: el modelo nunca lo recibe y, cuando el turno que estaba respondiendo termina, el
      boton vuelve a «Enviar» y «Vision está trabajando…» se va (antes la conexion del mensaje borrado quedaba abierta para siempre:
      el boton seguia en «Detener» y el estado en «Vision está trabajando…» sin que corriera nada).
  (d) cuando un mensaje sale de la cola y arranca, su cartel «En cola» desaparece y los que siguen esperando se renumeran
      (antes el cartel quedaba pegado: el aviso 'dequeued' nunca se emitia).
  (b) borrar con 🗑 un mensaje que YA arranco (el cartel ya se fue y la respuesta empezo a llegar) lo frena y no deja nada: ni su
      pregunta ni su respuesta a medias, tampoco despues de recargar la pagina, y el boton vuelve a «Enviar».
  (f) Detener nombra el turno que el usuario esta viendo responder (no el que espera en la cola), y cuando la cola avanza nombra
      al que arranco.

El proveedor de IA es falso (nunca hay red ni login, ADR-015) y duerme de verdad entre pedazos de texto."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
PREGUNTA_1 = ("¿Cuál preferís?", ["Opción uno", "Opción dos"])
PREGUNTA_2 = ("¿Y ahora?", ["Tercera", "Cuarta"])
ACTIVE_TEXT_S = 5.0  # lo que tarda el turno que pregunta en terminar de escribir, despues de la(s) pregunta(s)
QUEUED_TEXT_S = 3.0  # lo que tarda en responderse cada mensaje que estaba en cola
SECOND_QUESTION_DELAY_S = 1.5  # pausa antes de la segunda pregunta: da tiempo a tocar la primera opcion


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _question_call(n: int, question: tuple[str, list[str]]):
    text, labels = question
    args = json.dumps({"question": text, "options": [{"label": lab, "description": ""} for lab in labels]})
    call = SimpleNamespace(index=0, id=f"call_q{n}", function=SimpleNamespace(name="question", arguments=args))
    return [_chunk(tool_calls=[call]), _chunk(finish="tool_calls")]


def _slow_text(text: str, seconds: float, delay: float = 0.0):
    """Generador: espera `delay`, despues escribe `text` en pedazos repartidos en `seconds` (el loop lo consume en un hilo)."""
    if delay:
        time.sleep(delay)
    yield _chunk("[[TIPO: conversacional]]")
    pieces = 20
    for i in range(pieces):
        time.sleep(seconds / pieces)
        yield _chunk(f"{text} {i}. ")
    yield _chunk(finish="stop")


def _delayed(chunks, delay: float):
    time.sleep(delay)
    yield from chunks


class Provider:
    """El mensaje que dice «pregunta» hace 1 o 2 preguntas (`question`) y despues sigue escribiendo ACTIVE_TEXT_S; cualquier otro
    mensaje (la opcion tocada, que llega encolada) se responde en QUEUED_TEXT_S. Anota que textos de usuario le llegaron."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.received: list[str] = []
        self._lock = threading.Lock()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def asked(self, text: str) -> bool:
        with self._lock:
            return any(text in r for r in self.received)

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        user_text = str(messages[last_user].get("content") or "")
        tools_after = sum(1 for m in messages[last_user:] if m.get("role") == "tool")
        if tools_after == 0:
            with self._lock:
                self.received.append(user_text)
        if "pregunta" not in user_text:
            return _slow_text("Respuesta a tu opción", QUEUED_TEXT_S)
        wants = 2 if "dos preguntas" in user_text else 1
        if tools_after == 0:
            return iter(_question_call(1, PREGUNTA_1))
        if tools_after < wants:
            return _delayed(_question_call(2, PREGUNTA_2), SECOND_QUESTION_DELAY_S)
        return _slow_text("Sigo con lo que pediste", ACTIVE_TEXT_S)


@pytest.fixture(scope="module")
def provider():
    return Provider()


@pytest.fixture(scope="module")
def base_url(provider):
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


def _wrap(app, label: str):
    """El mensaje de usuario que dice `label` (su contenedor tiene el cartel «En cola» y los botones)."""
    return app.page.locator(".user-turn-wrap").filter(has=app.page.locator(".msg.user", has_text=label))


def _badge(app, label: str):
    return _wrap(app, label).locator(".user-turn-badge")


def _badge_text(app, label: str) -> str | None:
    loc = _badge(app, label)
    return " ".join((loc.text_content() or "").split()) if loc.count() else None  # el CSS parte el numero en otra linea


def _ask_and_queue(app, prompt: str, option: str) -> None:
    """Manda `prompt` (el modelo pregunta y sigue escribiendo) y toca `option` mientras Vision sigue trabajando: queda en cola."""
    app.page.fill("#chat-input", prompt)
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    app.page.locator("#question-panel:not(.hidden) .q-options button", has_text=option).click(timeout=15_000)
    _badge(app, option).filter(has_text="En cola").wait_for(timeout=10_000)


def test_deleting_a_queued_message_returns_the_button_and_the_status_when_the_turn_ends(app, provider):
    _ask_and_queue(app, "hacé una pregunta [c]", "Opción uno")
    assert _badge_text(app, "Opción uno") == "En cola · posición 1"

    _wrap(app, "Opción uno").locator(".user-turn-btn.danger").click()
    app.page.click("#confirm-accept-btn")
    _wrap(app, "Opción uno").wait_for(state="detached", timeout=5_000)
    deleted_at = time.monotonic()

    # El turno que pregunto sigue escribiendo unos segundos mas: mientras tanto el boton sigue en «Detener».
    assert app.page.get_attribute("#send-btn", "data-state") == "active"
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=int((ACTIVE_TEXT_S + 8) * 1000))
    assert app.page.inner_text("#send-btn-label") == "Enviar"
    app.page.wait_for_function(
        "() => !document.getElementById('conn-status-text').textContent.includes('trabajando')", timeout=3_000
    )
    assert app.page.get_attribute("#conn-status", "data-state") != "busy"
    assert time.monotonic() - deleted_at < ACTIVE_TEXT_S + 8
    time.sleep(1.0)  # margen: si el borrado se mandara igual, ya habria llegado al modelo
    assert not provider.asked("Opción uno"), "el mensaje borrado llegó al modelo"
    # Y se puede seguir usando: Enviar manda de verdad.
    app.page.fill("#chat-input", "otra cosa")
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=int((QUEUED_TEXT_S + 8) * 1000))


def test_the_queued_badge_goes_away_when_the_message_starts_and_the_rest_are_renumbered(app, provider):
    _ask_and_queue(app, "hacé dos preguntas [d]", "Opción uno")
    app.page.locator("#question-panel:not(.hidden) .q-options button", has_text="Tercera").click(timeout=15_000)
    _badge(app, "Tercera").filter(has_text="En cola").wait_for(timeout=10_000)
    assert _badge_text(app, "Opción uno") == "En cola · posición 1"
    assert _badge_text(app, "Tercera") == "En cola · posición 2"

    # Termina el turno que pregunto: arranca «Opción uno» (su respuesta empieza a llegar)...
    assert _wait(lambda: provider.asked("Opción uno"), ACTIVE_TEXT_S + 8), "el primero de la cola nunca arranco"
    _badge(app, "Opción uno").wait_for(state="detached", timeout=3_000)
    # ...y «Tercera», que sigue esperando, pasa a ser la primera de la cola.
    _badge(app, "Tercera").filter(has_text="posición 1").wait_for(timeout=3_000)
    assert _badge_text(app, "Tercera") == "En cola · posición 1"

    # Cuando arranca la ultima, su cartel tambien se va, y al terminar todo el boton vuelve a «Enviar».
    assert _wait(lambda: provider.asked("Tercera"), QUEUED_TEXT_S + 8), "el segundo de la cola nunca arranco"
    _badge(app, "Tercera").wait_for(state="detached", timeout=3_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=int((QUEUED_TEXT_S + 8) * 1000))
    assert app.page.locator(".user-turn-badge").count() == 0


def test_deleting_a_message_that_already_started_stops_it_and_leaves_nothing_behind(app, provider):
    _ask_and_queue(app, "hacé una pregunta [b]", "Opción dos")
    # El turno que pregunto termina y «Opción dos» arranca: su respuesta empieza a llegar debajo de el.
    assert _wait(lambda: provider.asked("Opción dos"), ACTIVE_TEXT_S + 8), "el mensaje en cola nunca arranco"
    _badge(app, "Opción dos").wait_for(state="detached", timeout=3_000)
    _wrap(app, "Opción dos").locator("xpath=..").locator(".msg.assistant", has_text="Respuesta a tu opción").wait_for(timeout=5_000)

    _wrap(app, "Opción dos").locator(".user-turn-btn.danger").click()
    app.page.click("#confirm-accept-btn")

    # Se frena (no se responde entero: QUEUED_TEXT_S todavia no paso) y el boton vuelve a «Enviar».
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=int(QUEUED_TEXT_S * 1000))
    app.page.wait_for_function(
        "() => !document.getElementById('conn-status-text').textContent.includes('trabajando')", timeout=3_000
    )
    assert not [n for n in app.notes() if "Turno detenido" in n], "lo borro el usuario: no es un «Turno detenido» suelto en el chat"
    assert app.page.locator(".msg.user", has_text="Opción dos").count() == 0
    assert app.page.locator(".msg.assistant", has_text="Respuesta a tu opción").count() == 0
    # Tampoco vuelve al recargar (la copia local de la conversacion no se quedo con la respuesta a medias del borrado).
    time.sleep(QUEUED_TEXT_S)  # si el borrado siguiera respondiendo, aca ya habria terminado
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.locator(".msg.assistant", has_text="Sigo con lo que pediste").first.wait_for(timeout=10_000)
    assert app.page.locator(".msg.user", has_text="Opción dos").count() == 0
    assert app.page.locator(".msg.assistant", has_text="Respuesta a tu opción").count() == 0


def _stop_request_body(app) -> dict | None:
    """Toca Detener y devuelve el cuerpo JSON del POST /stop que salio (None si salio sin cuerpo)."""
    with app.page.expect_request(lambda r: r.url.endswith("/stop") and r.method == "POST", timeout=5_000) as info:
        app.page.click("#send-btn")
    raw = info.value.post_data
    return json.loads(raw) if raw else None


def test_stop_names_the_turn_on_screen_also_after_the_queue_moves(app, provider):
    _ask_and_queue(app, "hacé una pregunta [f]", "Opción uno")
    asking_id = _wrap(app, "hacé una pregunta [f]").get_attribute("data-message-id")
    queued_id = _wrap(app, "Opción uno").get_attribute("data-message-id")
    assert asking_id and queued_id and asking_id != queued_id

    # Vision sigue escribiendo la respuesta del que pregunto: Detener lo nombra a EL, no al que espera en la cola.
    body = _stop_request_body(app)
    assert body is not None and body.get("turn_id") == asking_id, body

    # Detener como hoy: el siguiente de la cola arranca solo. Su cartel se va y Detener ahora lo nombra a el.
    assert _wait(lambda: provider.asked("Opción uno"), 10), "Detener no arranco el siguiente de la cola"
    _badge(app, "Opción uno").wait_for(state="detached", timeout=3_000)
    _wrap(app, "Opción uno").locator("xpath=..").locator(".msg.assistant", has_text="Respuesta a tu opción").wait_for(timeout=5_000)
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=5_000)
    body = _stop_request_body(app)
    assert body is not None and body.get("turn_id") == queued_id, body
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=int(QUEUED_TEXT_S * 1000))


def _wait(pred, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
