"""E2E de la voz con motores del backend (spec voz-fish-audio) en un Chromium
REAL contra el backend real.

El motor "fish" se reemplaza por uno falso que devuelve un WAV de verdad (un
tono), asi se prueba el camino completo del frontend: pedir la frase, decodificar
el audio, reproducirlo con Web Audio (con el efecto "IA" activo), cortarlo con ⏹
y caer a la voz del navegador cuando el motor falla. La voz del navegador es un
mock que registra lo que se le pidio decir. Nunca hay red externa.
"""

from __future__ import annotations

import io
import json
import math
import struct
import urllib.request
import wave
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
ANSWER = (
    "Esta es una respuesta de prueba bastante larga para leer en voz alta. "
    "Tiene varias frases, así el lector la parte en tramos y pide cada uno por separado."
)

# Voz del navegador falsa: registra cada texto y "termina" sola a los 150 ms.
SPEECH_MOCK = """
(() => {
  window.__spoken = [];
  const fake = {
    speaking: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: (utter) => {
      window.__spoken.push(utter.text);
      fake.speaking = true;
      setTimeout(() => { fake.speaking = false; utter.onend && utter.onend(); }, 150);
    },
    cancel: () => { fake.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""


def tone_wav(seconds: float = 3.0, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 330 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return buf.getvalue()


class FakeFishEngine:
    """Motor falso con la misma interfaz que los reales (app/voice/engines.py)."""

    name = "fish"
    label = "Fish Audio"
    models = ("s2.1-pro", "s2.1-pro-free")
    cloud = True

    def __init__(self):
        self.texts: list[str] = []
        self.fail_kind: str | None = None
        self.audio = tone_wav()

    def synthesize(self, text, *, model, voice_id):
        from app.voice.engines import Audio, VoiceEngineError

        self.texts.append(text)
        if self.fail_kind:
            raise VoiceEngineError(self.fail_kind, "simulado", 402)
        return Audio(self.audio, "audio/wav")

    def list_voices(self):
        return []


class FakeProvider:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        delta = SimpleNamespace(content=f"[[TIPO: conversacional]]{ANSWER}", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=stop, finish_reason="stop")]),
            ]
        )


@pytest.fixture(scope="module")
def fish():
    return FakeFishEngine()


@pytest.fixture(scope="module")
def base_url(fish):
    from app.llm import loop as loop_module
    from app.voice import service

    provider = FakeProvider()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        mp.setitem(service.ENGINES, "fish", fish)
        with serve_backend(provider) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


# Opener SIN proxy: el backend de prueba esta en loopback. `urllib.request.urlopen` toma el
# proxy del sistema (en esta PC, el PAC de la VPN de tu empresa) y, segun el estado de la red,
# intentaba salir por `proxy.empresa.com` incluso para 127.0.0.1 -- el guardian de red de
# los tests lo bloquea y el fixture fallaba con "Acceso a red bloqueado" (intermitente,
# dependia de la VPN). Con ProxyHandler({}) el pedido va siempre directo.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with _LOOPBACK.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def configure(base_url: str, chain: list[dict], **extra) -> None:
    post_json(
        base_url + "/api/voice/config",
        {"chain": chain, "consent": {"fish": bool(chain)}, "fish_voice_id": "voz-de-prueba", **extra},
    )


@pytest.fixture
def app(browser, base_url, fish):
    """Cadena [Fish] con efecto "IA", "Leer en voz alta" activo y la pagina lista."""
    from app.voice import service

    service.PAUSES.clear()
    fish.fail_kind = None
    fish.texts.clear()
    configure(base_url, [{"engine": "fish", "model": "s2.1-pro"}], effect=True)
    with open_app(browser, base_url) as a:
        a.page.add_init_script(SPEECH_MOCK)
        a.page.add_init_script("localStorage.setItem('vision.tts.enabled', 'true')")
        a.open()
        yield a
    configure(base_url, [], effect=False)


def send(app, text="hola"):
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")  # clic real: habilita el audio del navegador (autoplay)
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn")


def is_speaking(app) -> bool:
    return app.page.evaluate("(async () => (await import('/js/tts.js')).isSpeaking())()")


def test_the_answer_is_read_with_the_engine_and_stop_cuts_it_at_once(app, fish):
    """REQ-001/004/008/010: la respuesta se pide por tramos al motor, suena con
    Web Audio (efecto "IA" activo) y ⏹ la corta al instante."""
    send(app)
    btn = ".msg.assistant:not(.streaming) .msg-play-btn"
    app.page.wait_for_function("sel => document.querySelector(sel).dataset.playing === 'true'", arg=btn)
    app.page.wait_for_function("(async () => (await import('/js/tts.js')).isSpeaking())()")
    assert fish.texts, "el motor no recibio ninguna frase"
    assert fish.texts[0] in ANSWER and len(fish.texts[0]) < len(ANSWER), "tiene que pedirse por tramos"
    assert app.page.evaluate("window.__spoken") == [], "con el motor andando no habla la voz del navegador"

    app.page.click(btn)  # ⏹
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'false'", arg=btn, timeout=1000
    )
    assert is_speaking(app) is False
    assert app.page_errors == []


def test_when_the_engine_fails_the_browser_voice_speaks_and_warns_once(app, fish):
    """REQ-003: sin credito -> habla la voz del navegador y avisa UNA sola vez."""
    fish.fail_kind = "sin_credito"
    send(app, "primera")
    app.page.wait_for_function("window.__spoken.length > 0")
    assert app.page.evaluate("window.__spoken")[0] in ANSWER
    app.wait_note("sin crédito")

    send(app, "segunda")
    app.page.wait_for_function("window.__spoken.length > 1")
    notes = [n for n in app.notes() if "sin crédito" in n]
    assert len(notes) == 1
    assert "la voz del navegador" in notes[0]
    # El motor en pausa ya no se prueba en cada frase.
    assert len(fish.texts) == 1


def test_adding_a_cloud_engine_asks_for_consent(app, base_url):
    """REQ-002: sin confirmar, Fish no entra a la lista; confirmado y guardado,
    queda la cadena con el consentimiento."""
    configure(base_url, [])
    page = app.page
    page.evaluate("document.getElementById('settings-btn').click()")
    page.evaluate("document.querySelector('[data-tab=\"audio\"]').click()")
    page.wait_for_function("document.querySelectorAll('#cfg-voice-add-engine option').length > 0")
    assert page.locator("#cfg-voice-chain li").count() == 1  # solo la voz del navegador

    page.select_option("#cfg-voice-add-engine", "fish")
    page.click("#cfg-voice-add")
    page.wait_for_selector("#confirm-overlay:not(.hidden)")
    # spec informe-tecnico-hablado REQ-006: la confirmacion avisa que tambien
    # se envia el informe tecnico.
    assert "informe técnico" in page.inner_text("#confirm-message")
    page.click("#confirm-cancel-btn")
    assert page.locator("#cfg-voice-chain li").count() == 1
    assert page.is_checked("#cfg-voice-consent-fish") is False

    page.click("#cfg-voice-add")
    page.wait_for_selector("#confirm-overlay:not(.hidden)")
    page.click("#confirm-accept-btn")
    assert page.locator("#cfg-voice-chain li").count() == 2
    assert page.is_checked("#cfg-voice-consent-fish") is True
    page.fill("#cfg-voice-fish-id", "voz-de-prueba")
    page.click("#cfg-voice-save")
    page.wait_for_function("document.getElementById('cfg-voice-status').textContent.includes('Guardado')")

    with _LOOPBACK.open(base_url + "/api/voice/config", timeout=10) as resp:
        saved = json.loads(resp.read())
    assert saved["chain"] == [{"engine": "fish", "model": "s2.1-pro"}]
    assert saved["consent"]["fish"] is True
    assert app.page_errors == []


# Voz del navegador "tipo Google": habla pero NUNCA avisa que termino (ni onend ni onerror).
# Es el sintoma reportado por el usuario: "la voz de Google espanol no se escucha, sabina y raul si
# -- el Modo Cerebro me escucha la primera vez y despues no". Sin tope, `_speaking` quedaba en true
# para siempre y la cola (y el microfono del Cerebro) no volvian a moverse.
SILENT_END_SPEECH_MOCK = """
(() => {
  window.__spoken = [];
  const fake = {
    speaking: false, pending: false, paused: false,
    getVoices: () => [{ name: "Google espanol", lang: "es-ES", localService: false, voiceURI: "google-es" }],
    speak: (utter) => { window.__spoken.push(utter.text); fake.speaking = true; /* no hay onend */ },
    cancel: () => { fake.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""


def test_a_browser_voice_that_never_reports_its_end_does_not_freeze_the_queue(app, fish):
    """Motor caido -> cae a la voz del navegador, que nunca avisa el fin: el tope la da por
    terminada, `isSpeaking()` vuelve a false y la siguiente respuesta suena (camino de respaldo)."""
    app.page.evaluate(SILENT_END_SPEECH_MOCK)
    fish.fail_kind = "sin_credito"

    send(app, "primera")
    app.page.wait_for_function("window.__spoken.length > 0")
    # Con un texto de ~160 caracteres el tope es 5 s + 160*120 ms ~ 24 s; se espera con margen.
    app.page.wait_for_function(
        "(async () => !(await import('/js/tts.js')).isSpeaking())()", timeout=60_000
    )

    before = len(app.page.evaluate("window.__spoken"))
    send(app, "segunda")
    app.page.wait_for_function(f"window.__spoken.length > {before}", timeout=30_000)
    assert app.page_errors == []


# Voz del navegador REMOTA (tipo "Google espanol") que no tiene conexion: acepta `speak()` pero NUNCA
# empieza (ni onstart ni onend). El usuario se queda en silencio sin saber por que.
NEVER_STARTS_SPEECH_MOCK = """
(() => {
  window.__spoken = [];
  const fake = {
    speaking: false, pending: false, paused: false,
    getVoices: () => [{ name: "Google espanol", lang: "es-ES", localService: false, voiceURI: "google-es" }],
    speak: (utter) => { window.__spoken.push(utter.text); fake.speaking = true; /* ni onstart ni onend */ },
    cancel: () => { fake.speaking = false; },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""


def test_a_remote_browser_voice_that_never_starts_tells_the_user_why_once(app, fish):
    """La voz remota no arranca: se avisa UNA vez, en lenguaje claro, con la causa probable (necesita
    conexion) y la salida (elegir una voz instalada en Windows). Sin esto el usuario solo oye silencio."""
    app.page.evaluate(NEVER_STARTS_SPEECH_MOCK)
    fish.fail_kind = "sin_credito"  # el motor de la nube cae -> habla la voz del navegador

    send(app, "primera")
    app.wait_note("no empezó a hablar")
    notes = app.page.evaluate(
        "[...document.querySelectorAll('.msg.system-note')].map(n => n.textContent).filter(t => t.includes('no empezó a hablar'))"
    )
    assert len(notes) == 1
    assert "conexión" in notes[0] and "instalada en Windows" in notes[0]

    send(app, "segunda")  # no se repite el aviso
    app.page.wait_for_timeout(8000)
    notes_after = app.page.evaluate(
        "[...document.querySelectorAll('.msg.system-note')].map(n => n.textContent).filter(t => t.includes('no empezó a hablar'))"
    )
    assert len(notes_after) == 1
    assert app.page_errors == []


def test_a_voice_that_does_start_never_triggers_the_warning(app, fish):
    """Una voz sana (dispara onstart) no debe generar el aviso: ni falsos positivos."""
    healthy = NEVER_STARTS_SPEECH_MOCK.replace(
        "/* ni onstart ni onend */",
        "setTimeout(() => { utter.onstart && utter.onstart(); }, 50); "
        "setTimeout(() => { fake.speaking = false; utter.onend && utter.onend(); }, 300);",
    )
    app.page.evaluate(healthy)
    fish.fail_kind = "sin_credito"

    send(app, "hola")
    app.page.wait_for_function("window.__spoken.length > 0")
    app.page.wait_for_timeout(7000)
    assert app.page.evaluate("[...document.querySelectorAll('.msg.system-note')].filter(n => n.textContent.includes('no empezó a hablar')).length") == 0


# ---------------------------------------------------------------------------
# Modo Cerebro + motor de voz: el microfono NO debe grabar la voz de Vision (se auto-escucharia) y
# debe volver a escuchar cuando Vision termina. El micro falso de Chromium reproduce un tono de 440 Hz
# en bucle: si el VAD estuviera abierto mientras habla Fish, empezaria a grabar con ese tono.
# ---------------------------------------------------------------------------
RECORDER_SPY = """
(() => {
  window.__recStarts = [];
  const Original = window.MediaRecorder;
  window.MediaRecorder = function (stream, options) {
    const rec = new Original(stream, options);
    const start = rec.start.bind(rec);
    rec.start = (...a) => { window.__recStarts.push(performance.now()); return start(...a); };
    return rec;
  };
  window.MediaRecorder.prototype = Original.prototype;
  window.MediaRecorder.isTypeSupported = Original.isTypeSupported.bind(Original);
})();
"""


def test_brain_mode_does_not_record_while_a_voice_engine_speaks_and_listens_again_after(app, fish):
    """REQ-010 (voz-fish-audio): `isSpeaking()` ve el audio del motor (Web Audio), asi el VAD del Modo
    Cerebro se pausa mientras Vision habla y no se auto-escucha; al terminar vuelve a abrirse."""
    fish.audio = tone_wav(seconds=4.0)  # la respuesta dura 4 s por tramo: ventana clara para medir
    app.page.add_init_script(RECORDER_SPY)
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()

    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_function("(async () => (await import('/js/tts.js')).isSpeaking())()", timeout=30_000)
    app.start_listening("#brain-btn")  # el Cerebro arranca CON Vision hablando
    spoke_from = app.page.evaluate("performance.now()")

    # Mientras suena el motor, el VAD esta pausado: ninguna grabacion nueva.
    app.page.wait_for_timeout(1500)
    assert is_speaking(app) is True
    during = app.page.evaluate("window.__recStarts.filter(t => t >= " + str(spoke_from) + ").length")
    assert during == 0, f"el Cerebro empezo a grabar {during} vez/veces mientras ULTRON hablaba (se auto-escucha)"

    # Cuando termina de hablar (y pasa el enfriamiento de 900 ms), vuelve a escuchar el tono del micro falso.
    app.page.wait_for_function("(async () => !(await import('/js/tts.js')).isSpeaking())()", timeout=60_000)
    app.page.wait_for_function("window.__recStarts.length > 0", timeout=15_000)
    assert app.page_errors == []


RECORDER_SPY_WITH_STOPS = """
(() => {
  window.__recStarts = [];
  window.__recStops = [];
  const Original = window.MediaRecorder;
  window.MediaRecorder = function (stream, options) {
    const rec = new Original(stream, options);
    const start = rec.start.bind(rec);
    const stop = rec.stop.bind(rec);
    rec.start = (...a) => { window.__recStarts.push(performance.now()); return start(...a); };
    rec.stop = (...a) => { window.__recStops.push(performance.now()); return stop(...a); };
    return rec;
  };
  window.MediaRecorder.prototype = Original.prototype;
  window.MediaRecorder.isTypeSupported = Original.isTypeSupported.bind(Original);
})();
"""


def test_a_phrase_being_recorded_is_closed_as_soon_as_vision_starts_speaking(app, fish):
    """spec fix-cerebro-escucha-y-microfono REQ-002: lo que el usuario venia diciendo se cierra (y se manda, como antes) apenas
    Vision empieza a hablar, en vez de seguir grabando la voz de Vision, que la cancelacion de eco no resta."""
    fish.audio = tone_wav(seconds=3.0)
    app.page.add_init_script(RECORDER_SPY_WITH_STOPS)
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.start_listening("#brain-btn")
    app.page.wait_for_function("window.__recStarts.length >= 1", timeout=10_000)  # el tono del microfono falso abre una frase

    app.page.evaluate("(async () => (await import('/js/tts.js')).speak('respuesta de prueba'))()")
    app.page.wait_for_function("(async () => (await import('/js/tts.js')).isSpeaking())()", timeout=20_000)
    spoke_at = app.page.evaluate("performance.now()")

    app.page.wait_for_function("window.__recStops.length >= 1", timeout=3_000)
    stopped_at = app.page.evaluate("window.__recStops[0]")
    assert stopped_at - spoke_at < 700, f"la frase siguio grabando {stopped_at - spoke_at:.0f} ms con ULTRON hablando"
    app.page.keyboard.press("Escape")
