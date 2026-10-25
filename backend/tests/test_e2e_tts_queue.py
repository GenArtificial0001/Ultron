"""E2E de la cola de voz (spec `fix-tts-respuestas-silenciosas`, APPROVED
2026-09-27).

Bug real reportado por el usuario en el servidor de dev: "la voz de Vision no
se está reproduciendo", pero "Probar voz" (Configuración > Audio) SÍ sonaba.
Causa raíz: `speakTestPhrase()` saltea la cola (llama directo a
`speechSynthesis.speak()`), mientras las respuestas pasan por `_pump()`, que
hace early-return si `_speaking` quedó trabado en `true` por una utterance que
murió SIN disparar `onend` (Chrome lo hace al cambiar el dispositivo de audio,
ej. conectar un Bluetooth). Estos tests ejercitan el módulo `tts.js` real
(el mismo que carga la app) con un `speechSynthesis` mockeado para simular esa
muerte silenciosa de forma determinista.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"

# Mock de Web Speech API: `speak()` marca speaking=true y NUNCA dispara onend
# por su cuenta (el test simula la muerte silenciosa poniendo speaking=false
# a mano, sin onend -- exactamente el bug real). `cancel()` baja speaking.
SPEECH_MOCK_SCRIPT = """
(() => {
  const mock = {
    speakCalls: 0,
    cancelCalls: 0,
    speaking: false,
    pending: false,
    paused: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: function (utter) { this.speakCalls += 1; this.speaking = true; this._last = utter; },
    cancel: function () { this.cancelCalls += 1; this.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: mock, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
  window.__ttsMock = mock;
})();
"""


class FakeLLM:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {
            "data": [
                {
                    "id": MODEL,
                    "type": "LLM",
                    "aliases": [],
                    "valid_args": [],
                    "reasoning_levels": [],
                }
            ]
        }

    def _create(self, **kwargs):
        delta = SimpleNamespace(content="[[TIPO: conversacional]]Hola.", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=delta, finish_reason=None)]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=stop, finish_reason="stop")]
                ),
            ]
        )


@pytest.fixture(scope="module")
def proveedor():
    return FakeLLM()


@pytest.fixture(scope="module")
def base_url(proveedor):
    from app.llm import loop as loop_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        # Instalar el mock DESPUES de que la app cargo (tts.js lee
        # window.speechSynthesis en cada llamada, no al importar).
        a.page.evaluate(SPEECH_MOCK_SCRIPT)
        a.page.evaluate("localStorage.setItem('vision.tts.enabled', 'true')")
        yield a


def _speak(app, text):
    # Importar el MISMO modulo que usa la app (cache de ES modules) y hablar.
    app.page.evaluate(
        "async (t) => { const m = await import('/js/tts.js'); m.speak(t); }",
        text,
    )


def _speak_calls(app):
    return app.page.evaluate("window.__ttsMock.speakCalls")


def test_stuck_queue_recovers_so_next_response_speaks(app):
    """REQ-001: si una utterance muere sin onend (speaking=false sin avisar),
    la proxima respuesta se reproduce igual -- la cola se auto-recupera."""
    app.page.evaluate("window.__ttsMock.speakCalls = 0")
    _speak(app, "primera respuesta")
    assert _speak_calls(app) == 1, "la primera debe sonar"

    # Simula la muerte silenciosa: Chrome corta la utterance al cambiar de
    # audio -- speaking pasa a false SIN disparar onend (el bug real).
    app.page.evaluate("window.__ttsMock.speaking = false")

    _speak(app, "segunda respuesta")
    assert _speak_calls(app) == 2, (
        "REQ-001: tras una utterance que murio sin onend, la nueva respuesta "
        "debe sonar igual (cola auto-recuperada), no quedar muda"
    )


def test_legit_speaking_still_queues_without_cutting(app):
    """REQ-005: con una voz REALMENTE sonando (speaking=true), una respuesta
    nueva NO debe cortarla -- se encola detras, como siempre."""
    app.page.evaluate("window.__ttsMock.speakCalls = 0")
    _speak(app, "voz en curso")
    assert _speak_calls(app) == 1
    # speaking sigue true (no la matamos) -> la siguiente se encola, no suena ya.
    _speak(app, "encolada")
    assert _speak_calls(app) == 1, (
        "no debe sonar la segunda mientras la primera sigue hablando"
    )


def test_watchdog_forces_end_when_onend_never_arrives(app):
    """REQ-002: si el onend NUNCA llega, el watchdog fuerza el fin para no
    trabar la cola para siempre."""
    # Texto corto: watchdog = 5000ms + len*120ms. Con "x" son ~5.1s.
    app.page.evaluate("window.__ttsMock.speakCalls = 0")
    app.page.evaluate("window.__ttsMock.cancelCalls = 0")
    _speak(app, "x")
    assert _speak_calls(app) == 1
    # Esperar a que el watchdog dispare (margen amplio), sin disparar onend.
    app.page.wait_for_function("window.__ttsMock.cancelCalls >= 1", timeout=15000)
    # Tras el watchdog, la cola queda libre -> la proxima respuesta suena.
    _speak(app, "otra")
    assert _speak_calls(app) == 2, "tras el watchdog, la cola debe estar libre"
