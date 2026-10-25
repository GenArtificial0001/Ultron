"""E2E del autoenvio del Modo Cerebro (spec autoenvio-cerebro-sin-voz) en un Chromium REAL
contra el backend real.

Pedido del usuario (2026-10-03): el mensaje se enviaba cortado aunque la pausa de autoenvio estuviera
configurada. El envio dependia de un temporizador armado cuando termina de TRANSCRIBIRSE una frase, y
nada lo desarmaba si la voz volvia.

Como se simula la voz, sin microfono fisico (a diferencia de `test_e2e_microphone.py`, que usa un tono
fijo): un script inyectado reemplaza `getUserMedia` por un oscilador con una ganancia que el TEST
controla (`window.__setVoice(true|false)`). Asi el test decide exactamente cuando "habla" y cuando
"calla". Whisper y el modelo son falsos: nunca hay red ni login del proveedor de IA (ADR-015).

LIMITE: un tono sintetico prueba la logica de temporizadores y compuertas, NO la acustica de una voz ni
de un microfono reales. Eso queda para la prueba manual del usuario.

Marcado `e2e`. Se saltea solo si Playwright o su Chromium no estan instalados.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
PAUSE_S = 2  # la pausa de autoenvio configurada en estos tests

# Voz sintetica controlada por el test + registro de cada pedido a /api/chat con el estado de la voz
# EN ESE INSTANTE (`voice`): asi se ve si un envio ocurrio mientras el usuario seguia hablando.
SYNTH_MIC = """
(() => {
  window.__voiceOn = false;
  window.__chat = [];
  window.__voiceOffAt = 0;
  window.__setVoice = (on) => { window.__voiceOn = !!on; if (!on) window.__voiceOffAt = performance.now(); if (window.__gain) window.__gain.gain.value = on ? 0.5 : 0; };
  const md = navigator.mediaDevices;
  md.getUserMedia = async (constraints) => {
    const rec = { constraints: JSON.parse(JSON.stringify(constraints)), deviceId: "synthetic", label: "Microfono sintetico" };
    window.__gum.push(rec);
    const ctx = new AudioContext();
    await ctx.resume();
    const osc = ctx.createOscillator();
    osc.frequency.value = 220;
    const gain = ctx.createGain();
    gain.gain.value = window.__voiceOn ? 0.5 : 0;
    const dest = ctx.createMediaStreamDestination();
    osc.connect(gain);
    gain.connect(dest);
    osc.start();
    window.__gain = gain;
    window.__streams.push(dest.stream);
    rec.done = true;
    return dest.stream;
  };
  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (String(url).includes("/api/chat")) {
      let message = "";
      try { message = JSON.parse(init.body).message; } catch (e) { /* noop */ }
      window.__chat.push({ at: performance.now(), voice: window.__voiceOn, message });
    }
    return origFetch(input, init);
  };
})();
"""


class ScriptedWhisper:
    """Transcriptor falso: cada llamada toma el siguiente (texto, demora en segundos) de `script`."""

    def __init__(self):
        self.script: list[tuple[str, float]] = []
        self.calls = 0
        self._lock = threading.Lock()

    def create(self, **kwargs):
        kwargs["file"].read()
        with self._lock:
            self.calls += 1
            text, delay = self.script.pop(0) if self.script else ("", 0.0)
        time.sleep(delay)
        return text


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class FakeProvider:
    def __init__(self):
        self.whisper = ScriptedWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.delay = 0.0  # segundos que tarda en responder (para ver el icono mientras Vision procesa)

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        def stream():
            if self.delay:
                time.sleep(self.delay)
            yield _chunk("[[TIPO: conversacional]]Listo.")
            yield _chunk(finish="stop")

        return stream()


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


@pytest.fixture()
def brain(browser, base_url, provider):
    """Modo Cerebro prendido sobre el microfono sintetico, pausa de PAUSE_S s y autoenvio activado."""
    provider.whisper.script.clear()
    provider.whisper.calls = 0
    provider.delay = 0.0
    ctx = open_app(browser, base_url)
    app = ctx.__enter__()
    app.page.add_init_script(SYNTH_MIC)
    app.page.add_init_script(
        f"localStorage.setItem('vision.brain.autosendPauseMs', '{PAUSE_S}');"
        "localStorage.setItem('vision.brain.autosendEnabled', 'true');"
    )
    app.open()
    app.page.click("#brain-btn")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'")
    app.page.wait_for_timeout(1200)  # pasa el cooldown inicial del detector de voz
    yield app
    ctx.__exit__(None, None, None)


def voice(app, on: bool) -> None:
    app.page.evaluate(f"window.__setVoice({json.dumps(on)})")


def sent(app) -> list[dict]:
    return app.page.evaluate("window.__chat")


def wait_sent(app, count: int, timeout_ms: int) -> list[dict]:
    app.page.wait_for_function(f"window.__chat.length >= {count}", timeout=timeout_ms)
    return sent(app)


# ---------------------------------------------------------------------------
# Referencia: lo que ya funcionaba y no debe romperse
# ---------------------------------------------------------------------------
def test_one_phrase_is_sent_once_after_the_pause(brain, provider):
    provider.whisper.script[:] = [("primera parte", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    chats = wait_sent(brain, 1, 15_000)
    assert len(chats) == 1 and chats[0]["message"].startswith("primera parte")
    brain.page.wait_for_timeout(1500)
    assert len(sent(brain)) == 1  # una sola vez


def test_the_pause_is_counted_from_the_last_voice_not_from_the_end_of_the_transcription(brain, provider):
    """REQ-001 (decision D2): el envio ocurre N s despues de la ULTIMA voz. Si se contara desde que termina la transcripcion llegaria
    unos 0,6 s (confirmacion de silencio) mas la demora de Whisper despues, y este test lo detecta."""
    provider.whisper.script[:] = [("primera parte", 0.4)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    chats = wait_sent(brain, 1, 15_000)
    since_last_voice_ms = chats[0]["at"] - brain.page.evaluate("window.__voiceOffAt")
    assert PAUSE_S * 1000 - 150 <= since_last_voice_ms <= PAUSE_S * 1000 + 450, since_last_voice_ms


def test_with_autosend_off_saying_ultron_still_sends(brain, provider):
    """ULTRON (spec portar-desde-origen REQ-010): la palabra que llama al asistente es «Ultron»."""
    brain.page.evaluate("localStorage.setItem('vision.brain.autosendEnabled', 'false')")
    provider.whisper.script[:] = [("hola ultrón", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    chats = wait_sent(brain, 1, 10_000)
    assert chats[0]["message"].startswith("hola ultrón")


def test_with_autosend_off_saying_vision_no_longer_sends(brain, provider):
    """ULTRON ya no responde a «visión» (la palabra de la version de origen)."""
    brain.page.evaluate("localStorage.setItem('vision.brain.autosendEnabled', 'false')")
    provider.whisper.script[:] = [("hola visión", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(3000)
    assert sent(brain) == []


def test_with_autosend_off_and_no_mention_nothing_is_sent(brain, provider):
    brain.page.evaluate("localStorage.setItem('vision.brain.autosendEnabled', 'false')")
    provider.whisper.script[:] = [("sin la palabra clave", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout((PAUSE_S + 2) * 1000)
    assert sent(brain) == []


# ---------------------------------------------------------------------------
# El corte reportado: la voz vuelve antes de que venza la cuenta
# ---------------------------------------------------------------------------
def test_voice_coming_back_before_the_pause_ends_does_not_send_mid_sentence(brain, provider):
    """REQ-001/002/003 y T3: hablo, pauso >600 ms (se cierra y se transcribe la frase), sigo hablando. Mientras hablo no se envia
    nada, y al callar N s se envia UNA vez con las dos partes."""
    provider.whisper.script[:] = [("primera parte", 0.0), ("segunda parte", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(1500)  # >600 ms de silencio: la primera frase se cierra y se transcribe
    voice(brain, True)  # sigue hablando ANTES de que venza la cuenta de la primera frase
    brain.page.wait_for_timeout(3500)  # hoy la cuenta vence aca, en plena frase
    assert [c for c in sent(brain) if c["voice"]] == [], "se envio mientras el usuario seguia hablando"
    assert sent(brain) == []
    voice(brain, False)
    chats = wait_sent(brain, 1, 15_000)
    assert len(chats) == 1
    assert "primera parte" in chats[0]["message"] and "segunda parte" in chats[0]["message"]


def test_slow_transcription_does_not_send_while_the_user_is_speaking(brain, provider):
    """T2: Whisper tarda mas que la pausa; hoy el texto se envia en cuanto llega aunque ya hayas vuelto a hablar."""
    provider.whisper.script[:] = [("primera parte", 3.0), ("segunda parte", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(1500)
    voice(brain, True)
    brain.page.wait_for_timeout(5500)  # la transcripcion lenta llega a mitad de esta frase
    assert [c for c in sent(brain) if c["voice"]] == [], "se envio mientras el usuario seguia hablando"
    voice(brain, False)
    chats = wait_sent(brain, 1, 20_000)
    assert len(chats) == 1 and "segunda parte" in chats[0]["message"]


def test_a_pending_transcription_holds_the_send_even_if_the_user_is_silent(brain, provider):
    """REQ-003: dos frases transcribiendose a la vez. La segunda termina primero; si se enviara en ese momento saldria solo la segunda
    (la primera todavia esta en Whisper). Aislado de las otras compuertas: el usuario ya callo y no hay ninguna frase grabandose."""
    provider.whisper.script[:] = [("primera parte", 4.0), ("segunda parte", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(1200)  # la primera frase se cierra y queda 4 s en Whisper
    voice(brain, True)
    brain.page.wait_for_timeout(800)
    voice(brain, False)
    brain.page.wait_for_timeout(PAUSE_S * 1000 + 600)  # pasa la pausa entera con la primera transcripcion todavia en vuelo
    assert sent(brain) == [], "se envio con una transcripcion pendiente: el mensaje saldria cortado"
    chats = wait_sent(brain, 1, 20_000)
    assert len(chats) == 1
    assert "primera parte" in chats[0]["message"] and "segunda parte" in chats[0]["message"]


def test_the_pause_ending_inside_the_silence_confirmation_window_does_not_send(brain, provider):
    """REQ-002/003: la cuenta de la primera frase vence JUSTO en los 600 ms en que la segunda ya callo pero todavia se esta grabando
    (la voz no esta activa, la transcripcion aun no empezo). Solo la voz que cancela la cuenta o la compuerta de 'frase grabandose'
    lo evitan; cada una cubre a la otra, asi que un test que apague UNA sola no veria la diferencia (por eso se mutan juntas)."""
    provider.whisper.script[:] = [("primera parte", 0.0), ("segunda parte", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)  # t0: la cuenta de la primera frase vence a t0 + PAUSE_S
    brain.page.wait_for_timeout(1500)
    voice(brain, True)
    brain.page.wait_for_timeout(300)
    voice(brain, False)  # la segunda frase calla a t0 + 1,8 s: se sigue grabando hasta t0 + 2,4 s, y la cuenta vence a t0 + 2 s
    brain.page.wait_for_timeout(1200)
    assert sent(brain) == [], "se envio con la segunda frase todavia grabandose: saldria cortado"
    chats = wait_sent(brain, 1, 15_000)
    assert len(chats) == 1
    assert "primera parte" in chats[0]["message"] and "segunda parte" in chats[0]["message"]


def test_a_stalled_transcription_does_not_hold_the_already_transcribed_text_forever(brain, provider):
    """Incidente real 2026-10-03 (Modo Cerebro 'se quedo amarillo y no contesto'): Whisper del proveedor de IA tardaba ~1 minuto por fragmento
    (500 con reintentos). La compuerta 'hay una transcripcion en vuelo' de la v0.22.17 no tenia limite, asi que el texto ya
    transcripto no se enviaba hasta que la ULTIMA transcripcion terminara. Con un limite, se envia lo que ya hay y el fragmento que
    llegue tarde queda para el mensaje siguiente."""
    provider.whisper.script[:] = [("primera parte", 0.0), ("segunda parte", 40.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(1500)  # la primera frase se transcribe al instante
    voice(brain, True)
    brain.page.wait_for_timeout(800)
    voice(brain, False)  # la segunda frase queda 40 s en Whisper
    chats = wait_sent(brain, 1, 25_000)  # con el bug no se envia nada en 25 s (el fragmento colgado dura 40)
    assert chats[0]["message"].startswith("primera parte") and "segunda parte" not in chats[0]["message"]


def test_noise_after_the_first_phrase_does_not_strand_the_text(brain, provider):
    """T4 (guardia del arreglo): si la voz que volvio era ruido y Whisper devuelve vacio, el texto previo igual se envia a los N s.
    Hoy pasa porque el temporizador nunca se cancela; despues del arreglo solo pasa si se re-evalua el envio en TODOS los finales."""
    provider.whisper.script[:] = [("primera parte", 0.0), ("", 0.0)]
    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    brain.page.wait_for_timeout(1500)
    voice(brain, True)  # ruido: dura poco y Whisper no devuelve texto
    brain.page.wait_for_timeout(900)
    voice(brain, False)
    chats = wait_sent(brain, 1, 15_000)
    assert len(chats) == 1 and chats[0]["message"].startswith("primera parte")


# ---------------------------------------------------------------------------
# El campo de segundos: guardado a la vista (REQ-005)
# ---------------------------------------------------------------------------
@pytest.fixture()
def settings(browser, base_url):
    ctx = open_app(browser, base_url)
    app = ctx.__enter__()
    app.open()
    app.open_audio_settings()
    yield app
    ctx.__exit__(None, None, None)


def stored(app):
    return app.page.evaluate("localStorage.getItem('vision.brain.autosendPauseMs')")


def test_typing_saves_and_shows_a_visible_confirmation(settings):
    settings.page.fill("#cfg-brain-autosend-pause", "4")  # dispara 'input', NO 'change'
    settings.page.wait_for_function(
        "document.getElementById('cfg-brain-autosend-pause-status')?.textContent.includes('Guardado: 4 s')"
    )
    assert stored(settings) == "4"


@pytest.mark.parametrize(
    ("typed", "saved", "fragment"),
    [("0", "1", "se guardó 1 s"), ("15", "10", "se guardó 10 s"), ("", "2", "se guardó 2 s")],
)
def test_an_invalid_value_says_what_it_was_corrected_to(settings, typed, saved, fragment):
    settings.page.fill("#cfg-brain-autosend-pause", typed)
    settings.page.wait_for_function(
        "frag => document.getElementById('cfg-brain-autosend-pause-status')?.textContent.includes(frag)", arg=fragment
    )
    assert stored(settings) == saved


# Almacenamiento lleno/bloqueado: leer funciona, escribir tira (lo que hace Chromium con la cuota agotada). Como funcion para
# que `evaluate` no intente devolver la funcion asignada.
FULL_STORAGE_JS = """() => {
    localStorage.setItem('vision.brain.autosendPauseMs', '3');
    Storage.prototype.setItem = function () { throw new DOMException('lleno', 'QuotaExceededError'); };
}"""


def test_without_storage_the_typed_value_applies_until_reload_and_is_not_called_saved(settings):
    """REQ-005: si el navegador no deja guardar, el aviso lo dice (sin «Guardado», que seria falso) y lo escrito rige hasta
    recargar. Antes el aviso decia «Guardado: 6 s (no se pudo guardar...)» y la pausa seguia siendo la anterior (3 s)."""
    settings.page.evaluate(FULL_STORAGE_JS)
    settings.page.fill("#cfg-brain-autosend-pause", "6")
    settings.page.wait_for_function(
        "document.getElementById('cfg-brain-autosend-pause-status')?.textContent.includes('No se pudo guardar; vale solo hasta recargar')"
    )
    status = settings.page.text_content("#cfg-brain-autosend-pause-status")
    assert "Guardado" not in status and "6 s" in status
    assert stored(settings) == "3", "no se pudo escribir"

    settings.page.click("#settings-close")
    settings.open_audio_settings()  # la pantalla vuelve a leer la pausa vigente
    assert settings.page.input_value("#cfg-brain-autosend-pause") == "6", "rige el valor escrito, no el guardado antes"


def test_without_storage_an_invalid_value_says_both_things(settings):
    settings.page.evaluate(FULL_STORAGE_JS)
    settings.page.fill("#cfg-brain-autosend-pause", "0")
    settings.page.wait_for_function(
        "document.getElementById('cfg-brain-autosend-pause-status')?.textContent.includes('vale solo hasta recargar')"
    )
    status = settings.page.text_content("#cfg-brain-autosend-pause-status")
    assert "“0” no es válido" in status and "1 s" in status
    assert "Guardado" not in status and "se guardó" not in status, "no se guardo: el aviso no puede decir que si"


def test_while_typing_the_field_is_not_rewritten(settings):
    """REQ-005: en `input` no se reescribe lo que el usuario tipea (escribir «10» pasa por «1»); la correccion llega en `change`."""
    settings.page.fill("#cfg-brain-autosend-pause", "0")  # 'input', sin 'change'
    settings.page.wait_for_function(
        "document.getElementById('cfg-brain-autosend-pause-status')?.textContent.includes('se guardó 1 s')"
    )
    assert settings.page.input_value("#cfg-brain-autosend-pause") == "0"
    settings.page.dispatch_event("#cfg-brain-autosend-pause", "change")
    settings.page.wait_for_function("document.getElementById('cfg-brain-autosend-pause').value === '1'")


# ---------------------------------------------------------------------------
# spec fix-cerebro-escucha-y-microfono: el microfono que solo entrega CEROS exactos (palanca del auricular arriba un rato,
# driver dormido) se reabre solo con el Cerebro sin nada pendiente, y el espacio lo reabre al instante. El microfono
# sintetico en silencio entrega ceros exactos; uno real, nunca (siempre hay piso de ruido).
# ---------------------------------------------------------------------------
def test_a_brain_mic_that_only_delivers_digital_silence_is_reopened_by_itself_and_keeps_working(brain, provider):
    provider.whisper.script[:] = [("despues de reabrir", 0)]
    voice(brain, False)

    brain.page.wait_for_function("window.__gum.length >= 2", timeout=20_000)

    voice(brain, True)
    brain.page.wait_for_timeout(1000)
    voice(brain, False)
    chats = wait_sent(brain, 1, 15_000)
    assert chats[0]["message"].startswith("despues de reabrir"), "el microfono reabierto escucha de verdad"
    assert not any("escuchando por" in n for n in brain.notes()), "el reintento silencioso no llena el chat de avisos"


def test_space_reopens_a_brain_mic_that_only_delivers_digital_silence(brain):
    voice(brain, False)
    brain.page.wait_for_timeout(2_000)  # ceros sostenidos (recien abierto, un instante en ceros no cuenta)
    before = brain.page.evaluate("window.__gum.length")

    brain.page.keyboard.press("Space")

    brain.page.wait_for_function(f"window.__gum.length > {before}", timeout=2_500)


def test_the_brain_icon_turns_yellow_while_vision_processes_any_turn(brain, provider):
    """REQ-006: tambien un mensaje escrito a mano (antes solo los del Cerebro ponian el amarillo)."""
    provider.delay = 3.0
    brain.page.fill("#chat-input", "hola por escrito")
    brain.page.keyboard.press("Enter")

    brain.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'processing'", timeout=2_500)
    brain.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'idle-active'", timeout=15_000)
