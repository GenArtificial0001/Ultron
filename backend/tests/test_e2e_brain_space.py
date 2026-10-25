"""E2E de la barra Espacio como boton de turnos del Modo Cerebro (spec cerebro-espacio-turnos) en un Chromium REAL contra el backend real.

Pedido del usuario (2026-10-19): Espacio ENCIENDE el Cerebro apagado; con el modo escuchando, CORTA la escucha y ENVIA lo dicho; procesando, VUELVE A ESCUCHAR y suma lo nuevo; con Vision hablando,
la INTERRUMPE y vuelve a escuchar (esto ya existia y NO se toca); y Esc lo apaga por completo en cualquier estado.

Voz sintetica controlada por el test (`window.__setVoice`), igual que `test_e2e_brain_autosend.py`. Whisper y el modelo son falsos. Que Vision "hable" se simula con `speechSynthesis.speaking`,
que es lo que lee `isSpeaking()` en la app.
LIMITE: un tono sintetico prueba la logica de estados, NO la acustica de un microfono real."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"

SYNTH_MIC = """
(() => {
  window.__voiceOn = false;
  window.__chat = [];
  window.__setVoice = (on) => { window.__voiceOn = !!on; if (window.__gain) window.__gain.gain.value = on ? 0.5 : 0; };
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
      window.__chat.push({ at: performance.now(), message });
    }
    return origFetch(input, init);
  };
  // ULTRON "habla": lo que lee isSpeaking() (speechSynthesis.speaking). cancel() lo apaga, como el navegador real.
  window.__visionSpeaking = false;
  Object.defineProperty(window.speechSynthesis, "speaking", { get: () => window.__visionSpeaking, configurable: true });
  const realCancel = window.speechSynthesis.cancel.bind(window.speechSynthesis);
  window.__cancelCalls = 0;
  window.speechSynthesis.cancel = () => { window.__cancelCalls++; window.__visionSpeaking = false; try { realCancel(); } catch (e) { /* noop */ } };
})();
"""


class ScriptedWhisper:
    def __init__(self):
        self.script: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def create(self, **kwargs):
        kwargs["file"].read()
        with self._lock:
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
        self.delay = 0.0

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
    with launch_chromium(tmp_path_factory.mktemp("brainspace"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url, provider):
    """Cerebro APAGADO, pausa de autoenvio LARGA (30 s) para que NADA se envie solo: lo que se envie lo manda Espacio."""
    provider.whisper.script.clear()
    provider.delay = 0.0
    ctx = open_app(browser, base_url)
    a = ctx.__enter__()
    a.page.add_init_script(SYNTH_MIC)
    a.page.add_init_script("localStorage.setItem('vision.brain.autosendPauseMs', '10'); localStorage.setItem('vision.brain.autosendEnabled', 'true');")
    a.open()
    yield a
    ctx.__exit__(None, None, None)


def voice(app, on: bool) -> None:
    app.page.evaluate(f"window.__setVoice({json.dumps(on)})")


def btn_state(app) -> str:
    return app.page.get_attribute("#brain-btn", "data-state") or ""


def wait_btn(app, state: str, timeout: int = 10_000) -> None:
    app.page.wait_for_function(f"document.getElementById('brain-btn').dataset.state === {state!r}", timeout=timeout)


def sent(app) -> list[dict]:
    return app.page.evaluate("window.__chat")


def turn_on_by_button(app) -> None:
    app.page.click("#brain-btn")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    app.page.wait_for_timeout(1200)  # pasa el enfriamiento inicial del detector de voz


def say(app, provider, text: str, ms: int = 900) -> None:
    """Dice `text` (Whisper falso) y deja que se transcriba, SIN soltar al autoenvio (pausa de 10 s)."""
    provider.whisper.script[:] = [(text, 0.0)]
    voice(app, True)
    app.page.wait_for_timeout(ms)
    voice(app, False)
    app.page.wait_for_timeout(1500)  # 600 ms de confirmacion de silencio + transcripcion


def focus_body(app) -> None:
    app.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")


# ------------------------------------------------------------------------------------------------------------------ REQ-001/002: Espacio enciende
def test_space_turns_the_brain_on_when_it_is_off_and_it_starts_listening(app):
    focus_body(app)
    assert btn_state(app) == "idle"

    app.page.keyboard.press("Space")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)

    assert btn_state(app) in {"idle-active", "listening", "processing"}
    assert app.page.locator("#brain-banner").is_visible()


def test_space_does_not_turn_it_on_while_typing_in_the_chat_box(app):
    app.page.click("#chat-input")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(600)

    assert btn_state(app) == "idle", "escribir un espacio en el chat no puede encender el microfono"
    assert app.page.input_value("#chat-input") == " "


def test_space_does_not_turn_it_on_while_typing_in_any_text_field(app):
    app.page.evaluate("const i = document.createElement('input'); i.id = 'zz-input'; document.body.appendChild(i); i.focus();")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(500)

    assert btn_state(app) == "idle"


def test_space_does_not_turn_it_on_with_a_modal_open(app):
    app.page.click("#settings-btn")
    app.page.wait_for_selector("#settings-overlay:not(.hidden)", timeout=10_000)
    focus_body(app)

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(500)

    assert btn_state(app) == "idle"


@pytest.mark.parametrize("modifier", ["Control", "Alt", "Meta", "Shift"])
def test_space_with_a_modifier_does_not_turn_it_on(app, modifier):
    focus_body(app)

    app.page.keyboard.down(modifier)
    app.page.keyboard.press("Space")
    app.page.keyboard.up(modifier)
    app.page.wait_for_timeout(500)

    assert btn_state(app) == "idle"


def test_holding_space_does_not_toggle_it_on_and_off_or_resend(app):
    focus_body(app)
    app.page.keyboard.down("Space")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    for _ in range(5):
        app.page.keyboard.down("Space")  # repeticion automatica del teclado: e.repeat = true
    app.page.keyboard.up("Space")
    app.page.wait_for_timeout(500)

    assert btn_state(app) != "idle", "mantener apretado no lo apaga ni lo reinicia"


def test_space_does_nothing_when_the_shortcut_is_disabled_in_settings(app):
    app.page.evaluate("localStorage.setItem('vision.brain.spaceInterruptEnabled', 'false')")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    focus_body(app)

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(600)

    assert btn_state(app) == "idle" and sent(app) == []


def test_space_does_not_turn_it_on_while_another_voice_mode_is_active(app):
    app.page.evaluate("document.getElementById('voice-mic-btn').click()")
    app.page.wait_for_function("document.getElementById('voice-mic-btn').dataset.listening === 'true'", timeout=10_000)
    focus_body(app)

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(600)

    assert btn_state(app) == "idle", "un solo modo de voz a la vez"


# ------------------------------------------------------------------------------------------------------------------ REQ-003/004: escuchando -> enviar
def test_space_while_listening_sends_what_was_said_as_one_message(app, provider):
    turn_on_by_button(app)
    say(app, provider, "mandame el resumen de hoy")
    assert sent(app) == [], "con la pausa de autoenvio larga, nada salio solo"

    app.page.keyboard.press("Space")
    app.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)

    chats = sent(app)
    assert len(chats) == 1 and chats[0]["message"].startswith("mandame el resumen de hoy")


def test_space_mid_sentence_closes_the_phrase_waits_for_the_transcription_and_sends_everything_once(app, provider):
    turn_on_by_button(app)
    say(app, provider, "primera parte")
    provider.whisper.script[:] = [("segunda parte", 0.4)]
    voice(app, True)
    app.page.wait_for_timeout(900)  # sigue hablando: hay una frase GRABANDOSE

    app.page.keyboard.press("Space")
    voice(app, False)
    app.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)
    app.page.wait_for_timeout(1500)

    chats = sent(app)
    assert len(chats) == 1, chats
    assert "primera parte" in chats[0]["message"] and "segunda parte" in chats[0]["message"]


def test_the_state_goes_to_processing_after_space_while_the_answer_is_pending(app, provider):
    provider.delay = 3.0
    turn_on_by_button(app)
    say(app, provider, "hola visión")

    app.page.keyboard.press("Space")
    wait_btn(app, "processing", timeout=10_000)


def test_space_while_listening_with_nothing_said_sends_nothing_and_changes_no_state(app):
    turn_on_by_button(app)
    before = btn_state(app)

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(1000)

    assert sent(app) == [] and btn_state(app) == before and btn_state(app) != "idle"


def test_space_cuts_the_autosend_countdown_and_sends_right_away(app, provider):
    app.page.evaluate("localStorage.setItem('vision.brain.autosendPauseMs', '9')")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    turn_on_by_button(app)
    say(app, provider, "enviame ya")
    started = app.page.evaluate("performance.now()")

    app.page.keyboard.press("Space")
    app.page.wait_for_function("window.__chat.length >= 1", timeout=8_000)

    assert sent(app)[0]["at"] - started < 4_000, "salio por Espacio, no esperando los 9 s del autoenvio"


# ------------------------------------------------------------------------------------------------------------------ REQ-005: procesando -> volver a escuchar
def test_space_while_processing_reopens_the_listening_without_cancelling_the_turn(app, provider):
    provider.delay = 4.0
    turn_on_by_button(app)
    say(app, provider, "primera pregunta")
    app.page.keyboard.press("Space")
    wait_btn(app, "processing", timeout=10_000)

    app.page.keyboard.press("Space")
    voice(app, True)
    wait_btn(app, "listening", timeout=10_000)

    assert len(sent(app)) == 1, "el turno en curso no se cancelo ni se reenvio"


def test_what_is_said_after_space_while_processing_goes_out_as_a_new_message_when_the_turn_ends(app, provider):
    provider.delay = 2.5
    turn_on_by_button(app)
    say(app, provider, "primera pregunta")
    app.page.keyboard.press("Space")
    wait_btn(app, "processing", timeout=10_000)

    app.page.keyboard.press("Space")
    provider.whisper.script[:] = [("y tambien esto otro", 0.0)]
    voice(app, True)
    app.page.wait_for_timeout(900)
    voice(app, False)
    app.page.wait_for_timeout(1500)
    app.page.keyboard.press("Space")
    app.page.wait_for_function("window.__chat.length >= 2", timeout=25_000)

    msgs = [c["message"] for c in sent(app)]
    assert msgs[0].startswith("primera pregunta") and msgs[1].startswith("y tambien esto otro"), msgs


# ------------------------------------------------------------------------------------------------------------------ REQ-006: hablando -> interrumpe (SIN CAMBIOS)
def test_space_while_vision_speaks_interrupts_it_and_goes_back_to_listening_exactly_as_before(app):
    """REGRESION de lo que ya existia: Espacio con Vision hablando la corta (cancelSpeech) y deja el microfono escuchando; NO envia nada y NO apaga."""
    turn_on_by_button(app)
    app.page.evaluate("window.__visionSpeaking = true")
    gum_before = len(app.gum())

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(500)

    assert app.page.evaluate("window.__cancelCalls") >= 1, "Espacio tiene que cortar la voz de ULTRON"
    assert app.page.evaluate("window.__visionSpeaking") is False
    assert btn_state(app) != "idle", "no apaga el modo"
    assert sent(app) == [], "interrumpir a ULTRON no envia nada"
    assert len(app.gum()) == gum_before, "no pide un microfono nuevo (blindaje original)"


def test_space_while_vision_speaks_discards_nothing_it_did_not_before_and_does_not_send_the_pending_text(app, provider):
    turn_on_by_button(app)
    say(app, provider, "texto pendiente")
    app.page.evaluate("window.__visionSpeaking = true")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(800)

    assert sent(app) == [], "mientras ULTRON hablaba, Espacio solo la interrumpe: no manda lo pendiente"


# ------------------------------------------------------------------------------------------------------------------ REQ-007/009: Esc apaga siempre; Espacio nunca apaga
@pytest.mark.parametrize("phase", ["listening", "processing", "speaking"])
def test_escape_turns_everything_off_in_any_state(app, provider, phase):
    provider.delay = 3.0
    turn_on_by_button(app)
    if phase == "listening":
        voice(app, True)
        wait_btn(app, "listening")
    elif phase == "processing":
        say(app, provider, "hola visión")
        app.page.keyboard.press("Space")
        wait_btn(app, "processing", timeout=10_000)
    else:
        app.page.evaluate("window.__visionSpeaking = true")

    app.page.keyboard.press("Escape")
    wait_btn(app, "idle")

    assert not app.page.locator("#brain-banner").is_visible()


def test_space_never_turns_the_mode_off(app, provider):
    turn_on_by_button(app)
    for _ in range(4):
        app.page.keyboard.press("Space")
        app.page.wait_for_timeout(400)

    assert btn_state(app) != "idle"


def test_space_with_a_healthy_microphone_does_not_open_a_new_one(app):
    turn_on_by_button(app)
    before = len(app.gum())
    stream_before = app.page.evaluate("window.__streams.at(-1).id")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(300)

    assert len(app.gum()) == before and app.page.evaluate("window.__streams.at(-1).id") == stream_before


# ------------------------------------------------------------------------------------------------------------------ REQ-010/011: el cartel y el titulo lo explican
def test_the_banner_explains_space_and_escape_for_each_state(app, provider):
    provider.delay = 3.0
    turn_on_by_button(app)
    provider.whisper.script[:] = [("hola visión", 0.0)]  # ANTES de hablar: Whisper transcribe la frase en cuanto se cierra
    voice(app, True)
    app.page.wait_for_function("document.getElementById('brain-banner').dataset.state === 'listening'", timeout=10_000)
    listening = app.page.inner_text("#brain-banner")
    assert "Espacio" in listening and "enviar" in listening.lower() and "Esc" in listening

    app.page.wait_for_timeout(900)  # habla lo suficiente: un tono de unos pocos cientos de ms no cuenta como voz real y no habria «nada dicho»
    voice(app, False)
    app.page.wait_for_timeout(1800)  # 600 ms de confirmacion de silencio + transcripcion: ya hay algo dicho
    app.page.keyboard.press("Space")
    app.page.wait_for_function("document.getElementById('brain-banner').dataset.state === 'processing'", timeout=15_000)
    processing = app.page.inner_text("#brain-banner")
    assert "Espacio" in processing and "seguir hablando" in processing.lower() and "Esc" in processing

    # Vision empieza a hablar con el modelo todavia trabajando: `isSpeaking()` manda sobre el estado «procesando», que es lo mismo que mira Espacio para decidir si interrumpe.
    app.page.evaluate("window.__visionSpeaking = true")
    app.page.wait_for_function("document.getElementById('brain-banner').dataset.state === 'speaking'", timeout=10_000)
    speaking = app.page.inner_text("#brain-banner")
    assert "Espacio" in speaking and "interrumpir" in speaking.lower() and "Esc" in speaking


def test_both_keys_are_shown_as_keys(app):
    turn_on_by_button(app)

    keys = [k.strip() for k in app.page.locator("#brain-banner kbd").all_inner_texts()]
    assert keys == ["Espacio", "Esc"]


def test_the_banner_stays_hidden_when_the_mode_is_off(app):
    assert not app.page.locator("#brain-banner").is_visible()


def test_the_brain_button_title_says_it_also_turns_on_with_space(app):
    assert "Espacio" in (app.page.get_attribute("#brain-btn", "title") or "")


# ------------------------------------------------------------------------------------------------------------------ cierra mutaciones S2, S3, S7 y S12 (regresion de la 0.34.1)
def test_space_while_processing_turns_the_listening_back_on_right_away_not_when_the_turn_ends(app, provider):
    """S2: con el turno largo todavia en curso, Espacio ya tiene que mostrar «escuchando» (sin esperar a hablar ni a que termine la respuesta)."""
    provider.delay = 12.0
    turn_on_by_button(app)
    say(app, provider, "primera pregunta")
    app.page.keyboard.press("Space")
    wait_btn(app, "processing", timeout=10_000)

    app.page.keyboard.press("Space")

    wait_btn(app, "listening", timeout=2_500)
    assert app.page.locator("#brain-banner").get_attribute("data-state") == "listening"
    assert len(sent(app)) == 1, "el turno en curso sigue siendo uno solo y no se cancelo"


def test_holding_space_after_saying_something_sends_it_once_and_does_not_reopen_the_listening(app, provider):
    """S3: la repeticion automatica del teclado no cuenta como nuevas pulsaciones (si contara, tras enviar pasaria a «escuchando» sin que nadie lo pida)."""
    provider.delay = 12.0
    turn_on_by_button(app)
    say(app, provider, "algo dicho")
    app.page.keyboard.down("Space")
    wait_btn(app, "processing", timeout=10_000)
    for _ in range(3):  # IMPAR a proposito: sin el filtro cada repeticion alterna «escuchar» y «enviar»; con un numero par el vaiven termina donde empezo y no se notaria
        app.page.keyboard.down("Space")  # repeticion automatica: e.repeat = true
        app.page.wait_for_timeout(60)
    app.page.keyboard.up("Space")
    app.page.wait_for_timeout(1200)

    assert len(sent(app)) == 1, "se envio una sola vez"
    assert btn_state(app) == "processing", "las repeticiones no lo pasan a «escuchando»"


def test_space_waits_for_a_slow_transcription_still_running_before_sending_everything_once(app, provider):
    """S7: hay DOS frases en vuelo y la segunda termina primero; no se envia hasta que termine la lenta, y sale todo junto en UN mensaje."""
    turn_on_by_button(app)
    provider.whisper.script[:] = [("parte lenta", 3.0), ("parte rapida", 0.0)]
    voice(app, True)
    app.page.wait_for_timeout(900)
    voice(app, False)
    app.page.wait_for_timeout(900)  # la 1.a frase se cerro y su transcripcion (lenta) esta en vuelo
    voice(app, True)
    app.page.wait_for_timeout(900)  # 2.a frase grabandose

    app.page.keyboard.press("Space")
    voice(app, False)
    app.page.wait_for_function("window.__chat.length >= 1", timeout=20_000)
    app.page.wait_for_timeout(1500)

    chats = sent(app)
    assert len(chats) == 1, f"un solo mensaje con las dos partes, no uno por frase: {chats}"
    assert "parte lenta" in chats[0]["message"] and "parte rapida" in chats[0]["message"]


def test_interrupting_vision_discards_the_pending_text_so_the_next_space_has_nothing_to_send(app, provider):
    """S12: lo que Espacio ya hacia con Vision hablando incluye BORRAR lo pendiente; si no lo borrara, el siguiente Espacio lo enviaria."""
    turn_on_by_button(app)
    say(app, provider, "texto pendiente")
    app.page.evaluate("window.__visionSpeaking = true")
    app.page.keyboard.press("Space")  # interrumpe a Vision
    app.page.wait_for_timeout(600)
    assert app.page.evaluate("window.__visionSpeaking") is False

    app.page.keyboard.press("Space")  # ahora no hay nada dicho
    app.page.wait_for_timeout(1500)

    assert sent(app) == [], "lo pendiente se descarto al interrumpir: no hay nada que enviar"
