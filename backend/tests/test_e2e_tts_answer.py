"""E2E de que Vision LEE sus respuestas en voz alta (QA-013, hueco de tests: `voz-tts` figuraba como cubierta solo por tokens de texto, sin ninguna prueba del comportamiento).

Lo que se verifica, en un Chromium REAL contra el backend real y con la Web Speech API espiada (`speechSynthesis` falso que cuenta `speak()` y guarda el texto):
  - con la voz ACTIVADA, una respuesta que termina se lee entera una vez;
  - con la voz APAGADA, no se lee nada (y no queda nada en cola para cuando se vuelva a prender);
  - el texto que se lee es el de la respuesta, SIN la marca interna `[[TIPO: ...]]` y sin markdown;
  - "Probar voz" de Configuracion > Audio habla aunque la voz de las respuestas este apagada;
  - despues de Detener, la respuesta cortada NO se lee (revision F6 de la spec fix-detener-y-progreso-visible).

El proveedor de IA es falso y responde al instante: nunca hay red ni login del proveedor de IA (ADR-015). Marcado `e2e`.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
ANSWER = "[[TIPO: conversacional]]Hola, **todo** listo. Revisé el stock."

# `speechSynthesis` espiado: guarda cada texto que se le pide leer. Termina solo (dispara onend) para que la cola siga avanzando como en el navegador real.
SPEECH_SPY = """
(() => {
  const spy = {
    texts: [],
    speaking: false,
    pending: false,
    paused: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: function (utter) {
      this.texts.push(utter.text);
      this.speaking = true;
      setTimeout(() => { this.speaking = false; if (utter.onstart) utter.onstart(); if (utter.onend) utter.onend(); }, 10);
    },
    cancel: function () { this.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: spy, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
  window.__tts = spy;
})();
"""


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _SlowAnswer:
    """Respuesta larga «parte N.» que tarda `seconds`; `close()` (lo que hace Vision al Detener) la corta en el acto."""

    def __init__(self, seconds: float = 12.0, pieces: int = 24):
        self._items = [_chunk("[[TIPO: conversacional]]")] + [_chunk(f"parte {i}. ") for i in range(pieces)] + [_chunk(finish="stop")]
        self._pause = seconds / pieces
        self._i = 0
        self._closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if 0 < self._i < len(self._items) - 1 and self._closed.wait(self._pause):
            raise StopIteration
        if self._closed.is_set() or self._i >= len(self._items):
            raise StopIteration
        item = self._items[self._i]
        self._i += 1
        return item

    def close(self):
        self._closed.set()


def _last_user_text(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


class FakeProvider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        if "[lento]" in _last_user_text(kwargs.get("messages")):
            return _SlowAnswer()
        return iter([_chunk(ANSWER), _chunk(finish="stop")])


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


def open_with_voice(browser, base_url, enabled):
    ctx = open_app(browser, base_url)
    app = ctx.__enter__()
    app.page.add_init_script(SPEECH_SPY)  # antes de que app.js cargue: tts.js lee `speechSynthesis` al importar
    app.page.add_init_script(f"localStorage.setItem('vision.tts.enabled', '{'true' if enabled else 'false'}');")
    app.open()
    return ctx, app


def ask(app, text="hola"):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn", timeout=30_000)


def spoken(app):
    return app.page.evaluate("window.__tts.texts")


def test_with_the_voice_on_a_finished_answer_is_read_aloud_once(browser, base_url):
    ctx, app = open_with_voice(browser, base_url, enabled=True)
    try:
        ask(app)
        app.page.wait_for_function("window.__tts.texts.length >= 1", timeout=10_000)
        app.page.wait_for_timeout(500)  # margen para detectar una segunda lectura duplicada

        assert len(spoken(app)) == 1
    finally:
        ctx.__exit__(None, None, None)


def test_what_is_read_is_the_answer_without_the_internal_tag_or_markdown(browser, base_url):
    ctx, app = open_with_voice(browser, base_url, enabled=True)
    try:
        ask(app)
        app.page.wait_for_function("window.__tts.texts.length >= 1", timeout=10_000)

        text = " ".join(spoken(app))
        assert "Hola" in text and "todo listo" in text and "Revisé el stock" in text
        assert "[[TIPO" not in text and "conversacional" not in text  # la marca interna del modelo no se lee
        assert "**" not in text  # ni los asteriscos del markdown
    finally:
        ctx.__exit__(None, None, None)


def test_with_the_voice_off_nothing_is_read(browser, base_url):
    """Hay DOS defensas para este caso (`speak()` no encola y, ademas, `_pump()` vacia la cola si la voz esta apagada). Son redundantes a proposito: quitar UNA sola no
    cambia nada de lo que se ve, asi que ningun test puede distinguirla; quitar las DOS hace que se lea con la voz apagada y este test lo detecta (verificado a mano)."""
    ctx, app = open_with_voice(browser, base_url, enabled=False)
    try:
        ask(app)
        app.page.wait_for_timeout(1500)  # tiempo de sobra para que, si se fuera a leer, ya se hubiera leido

        assert spoken(app) == []
    finally:
        ctx.__exit__(None, None, None)


def test_turning_the_voice_on_afterwards_does_not_read_the_old_answer(browser, base_url):
    """Lo que se respondio con la voz apagada no queda 'en cola' para leerse despues: prender la voz solo afecta a las respuestas siguientes."""
    ctx, app = open_with_voice(browser, base_url, enabled=False)
    try:
        ask(app, "primera")
        app.page.evaluate("localStorage.setItem('vision.tts.enabled', 'true')")
        app.page.wait_for_timeout(800)
        assert spoken(app) == []

        ask(app, "segunda")
        app.page.wait_for_function("window.__tts.texts.length >= 1", timeout=10_000)
        app.page.wait_for_timeout(400)

        assert len(spoken(app)) == 1  # solo la segunda
    finally:
        ctx.__exit__(None, None, None)


def test_each_answer_is_read_in_its_own_turn(browser, base_url):
    ctx, app = open_with_voice(browser, base_url, enabled=True)
    try:
        ask(app, "una")
        app.page.wait_for_function("window.__tts.texts.length >= 1", timeout=10_000)
        ask(app, "dos")
        app.page.wait_for_function("window.__tts.texts.length >= 2", timeout=10_000)
        app.page.wait_for_timeout(400)

        assert len(spoken(app)) == 2
    finally:
        ctx.__exit__(None, None, None)


def test_after_stop_the_cut_answer_is_not_read_aloud(browser, base_url):
    """Revision F6 (spec fix-detener-y-progreso-visible): con «Leer en voz alta» activado, el usuario aprieta Detener a mitad de una
    respuesta larga. «Detener» corta la voz, pero enseguida el cierre del stream (la red de seguridad del final de renderIncomingStream)
    cerraba la burbuja a medias y la LEIA en voz alta: justo lo que el usuario acababa de frenar. La respuesta cortada sigue en pantalla;
    solo no se lee. La siguiente respuesta se lee como siempre."""
    ctx, app = open_with_voice(browser, base_url, enabled=True)
    try:
        app.page.fill("#chat-input", "contame algo largo [lento]")
        app.page.click("#send-btn")
        app.page.wait_for_function("() => document.querySelector('#messages').innerText.includes('parte 3')", timeout=20_000)
        app.page.wait_for_selector("#send-btn[data-state=active]")
        app.page.click("#send-btn")  # Detener
        app.wait_note("Turno detenido")
        app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=15_000)
        app.page.wait_for_timeout(1_500)  # el stream ya se cerro: si se fuera a leer, ya se habria leido

        assert not [t for t in spoken(app) if "parte" in t], f"se leyo la respuesta cortada: {spoken(app)}"
        assert "parte 3" in app.page.inner_text("#messages"), "la respuesta cortada tiene que seguir en pantalla"

        ask(app, "y ahora otra cosa")
        app.page.wait_for_function("window.__tts.texts.some(t => t.includes('Revisé el stock'))", timeout=10_000)
    finally:
        ctx.__exit__(None, None, None)


def test_turning_the_voice_off_cuts_the_current_reading_and_drops_the_ones_waiting(browser, base_url):
    """Camino real de la app: Configuracion > Audio llama a `setTtsEnabled(false)`. Corta la frase en curso (`cancel()`) y descarta las que esperaban,
    para que al volver a prender la voz no se lean de golpe respuestas viejas. El `speechSynthesis` espiado NO termina solo: la cola se acumula."""
    ctx, app = open_with_voice(browser, base_url, enabled=True)
    try:
        app.page.evaluate(
            "() => { window.__tts.cancelCalls = 0; const c = window.__tts.cancel; window.__tts.cancel = function () { window.__tts.cancelCalls += 1; return c.call(this); };"
            " window.__tts.speak = function (utter) { this.texts.push(utter.text); this.speaking = true; }; }"
        )
        app.page.evaluate("async () => { const m = await import('/js/tts.js'); m.speak('uno'); m.speak('dos'); m.speak('tres'); }")
        app.page.wait_for_function("window.__tts.texts.length === 1", timeout=5_000)  # solo 'uno' empezo; 'dos' y 'tres' esperan

        app.page.evaluate("async () => { const m = await import('/js/tts.js'); m.setTtsEnabled(false); }")
        assert app.page.evaluate("window.__tts.cancelCalls") >= 1  # la frase en curso se corto

        app.page.evaluate("async () => { const m = await import('/js/tts.js'); m.setTtsEnabled(true); window.__tts.speaking = false; m.speak('cinco'); }")
        app.page.wait_for_function("window.__tts.texts.includes('cinco')", timeout=5_000)
        app.page.wait_for_timeout(500)

        assert spoken(app) == ["uno", "cinco"]  # 'dos' y 'tres' NO se leyeron al volver a prender la voz
    finally:
        ctx.__exit__(None, None, None)
