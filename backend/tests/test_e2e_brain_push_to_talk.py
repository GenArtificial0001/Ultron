"""E2E de «Mantener Espacio para hablar», la sensibilidad del microfono y la solapa Audio ordenada (spec cerebro-hablar-con-espacio-y-audio-agrupado) en un Chromium REAL contra el backend real.

Pedido del usuario (2026-10-20): con mucho ruido ambiente el Modo Cerebro «nunca deja de procesar» porque su detector de voz cree que siempre habla. Pidio: mantener Espacio = escucha, soltar = completamente sordo, ir directo al
evento del teclado; una sensibilidad ajustable (a veces ambientes ruidosos, a veces habla bajito con auriculares) y agrupar bien las propiedades de entrada de microfono en la solapa Audio.

Como se simula la voz, sin microfono fisico: un oscilador con una ganancia que el TEST controla (`window.__setLevel(rms)` = nivel RMS exacto; `window.__setVoice(bool)` = voz fuerte). Whisper y el modelo son falsos.
LIMITE: un tono sintetico prueba la logica (teclas, estados, umbrales), NO la acustica de un microfono ni de un ruido real."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
MODE_KEY = "vision.brain.listenMode"
SENS_KEY = "vision.brain.sensitivity"

SYNTH_MIC = """
(() => {
  window.__voiceOn = false;
  window.__level = undefined;
  window.__chat = [];
  window.__gains = [];  // abrir Configuracion crea OTRO microfono (el del medidor): el nivel se aplica a todos, no solo al ultimo
  const apply = () => { const r = window.__level !== undefined ? window.__level * 1.4142 : (window.__voiceOn ? 0.5 : 0); window.__gains.forEach((g) => { g.gain.value = r; }); };
  window.__setVoice = (on) => { window.__voiceOn = !!on; window.__level = undefined; apply(); };
  window.__setLevel = (rms) => { window.__level = rms; apply(); };
  const md = navigator.mediaDevices;
  md.getUserMedia = async (constraints) => {
    const rec = { constraints: JSON.parse(JSON.stringify(constraints)), deviceId: "synthetic", label: "Microfono sintetico" };
    window.__gum.push(rec);
    const ctx = new AudioContext();
    await ctx.resume();
    const osc = ctx.createOscillator();
    osc.frequency.value = 220;
    const gain = ctx.createGain();
    gain.gain.value = 0;
    const dest = ctx.createMediaStreamDestination();
    osc.connect(gain);
    gain.connect(dest);
    osc.start();
    window.__gains.push(gain);
    apply();
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
  window.__recStarts = 0;
  const realStart = MediaRecorder.prototype.start;
  MediaRecorder.prototype.start = function (...args) { window.__recStarts++; return realStart.apply(this, args); };
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
    with launch_chromium(tmp_path_factory.mktemp("ptt"), base_url) as b:
        yield b


def _make_app(browser, base_url, provider, prefs: dict[str, str]):
    provider.whisper.script.clear()
    provider.whisper.calls = 0
    provider.delay = 0.0
    ctx = open_app(browser, base_url)
    a = ctx.__enter__()
    a.page.add_init_script(SYNTH_MIC)
    seed = {"vision.brain.autosendPauseMs": "10", "vision.brain.autosendEnabled": "true", **prefs}
    a.page.add_init_script("; ".join(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})" for k, v in seed.items()))
    a.open()
    return ctx, a


@pytest.fixture()
def ptt(browser, base_url, provider):
    """Modo «Mantener Espacio para hablar»."""
    ctx, a = _make_app(browser, base_url, provider, {MODE_KEY: "push"})
    yield a
    ctx.__exit__(None, None, None)


@pytest.fixture()
def auto(browser, base_url, provider):
    """Modo Automatico (de fabrica: ninguna preferencia nueva guardada)."""
    ctx, a = _make_app(browser, base_url, provider, {})
    yield a
    ctx.__exit__(None, None, None)


def level(app, rms: float) -> None:
    app.page.evaluate(f"window.__setLevel({json.dumps(rms)})")


def btn_state(app) -> str:
    return app.page.get_attribute("#brain-btn", "data-state") or ""


def banner_state(app) -> str:
    return app.page.locator("#brain-banner").get_attribute("data-state") or ""


def banner_text(app) -> str:
    return app.page.locator("#brain-banner").inner_text()


def sent(app) -> list[dict]:
    return app.page.evaluate("window.__chat")


def wait_banner(app, state: str, timeout: int = 10_000) -> None:
    app.page.wait_for_function(f"document.getElementById('brain-banner').dataset.state === {state!r}", timeout=timeout)


def turn_on_with_space(app) -> None:
    """Enciende con Espacio (apagado) y SUELTA: la pulsacion que enciende no habla."""
    app.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    app.page.keyboard.press("Space")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    app.page.wait_for_timeout(1200)  # pasa el enfriamiento inicial del microfono
    wait_banner(app, "paused", timeout=3_000)  # en este modo arranca SORDO: sin esto un test pasaria tambien en el modo automatico


def hold_and_say(app, provider, text: str, ms: int = 900) -> None:
    """Mantiene Espacio `ms` con voz fuerte y SUELTA (que es lo que envia). Whisper devuelve `text`."""
    provider.whisper.script[:] = [(text, 0.0)]
    app.page.keyboard.down("Space")
    level(app, 0.2)
    app.page.wait_for_timeout(ms)
    app.page.keyboard.up("Space")
    level(app, 0.0)


# ------------------------------------------------------------------------------------------------------------------ REQ-002: entre pulsaciones, sordo
def test_in_push_mode_loud_constant_noise_is_never_heard(ptt, provider):
    turn_on_with_space(ptt)

    level(ptt, 0.2)  # ruido fuerte y constante, MUY por encima de cualquier umbral
    ptt.page.wait_for_timeout(3500)

    assert btn_state(ptt) != "processing" and btn_state(ptt) != "listening", "el ruido no activa nada"
    assert banner_state(ptt) == "paused", "el cartel dice que esta en pausa"
    assert provider.whisper.calls == 0, "no se transcribio nada"
    assert sent(ptt) == [], "no se envio nada"


def test_in_push_mode_the_noise_does_not_keep_it_in_processing_forever(ptt, provider):
    """El sintoma exacto del usuario: con ruido, el modo automatico queda grabando para siempre. Aca, tras hablar y soltar, vuelve a pausa aunque siga el ruido."""
    provider.delay = 0.5
    turn_on_with_space(ptt)
    level(ptt, 0.2)
    hold_and_say(ptt, provider, "hola vision")
    level(ptt, 0.2)  # el ruido sigue

    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=20_000)
    wait_banner(ptt, "paused", timeout=20_000)

    assert len(sent(ptt)) == 1


# ------------------------------------------------------------------------------------------------------------------ REQ-003: mantener graba, soltar envia
def test_holding_space_listens_right_away_and_releasing_sends_what_was_said_once(ptt, provider):
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("quiero saber algo", 0.0)]

    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    wait_banner(ptt, "listening", timeout=3_000)
    assert btn_state(ptt) == "listening"
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)
    ptt.page.wait_for_timeout(1500)

    chats = sent(ptt)
    assert len(chats) == 1, chats
    assert "quiero saber algo" in chats[0]["message"]


def test_the_keyboard_auto_repeat_does_not_restart_or_resend(ptt, provider):
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("una sola vez", 0.0)]
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(600)
    for _ in range(5):
        ptt.page.keyboard.down("Space")  # repeticion automatica: e.repeat = true
        ptt.page.wait_for_timeout(120)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)
    ptt.page.wait_for_timeout(1500)

    assert len(sent(ptt)) == 1, "una sola pulsacion sostenida = un solo mensaje"


def test_releasing_space_with_nothing_said_sends_nothing_and_says_so(ptt, provider):
    turn_on_with_space(ptt)

    ptt.page.keyboard.down("Space")  # sin voz: silencio digital
    ptt.page.wait_for_timeout(700)
    ptt.page.keyboard.up("Space")
    ptt.page.wait_for_timeout(1500)

    assert sent(ptt) == []
    assert provider.whisper.calls == 0, "sin voz no se llama a Whisper"
    assert banner_state(ptt) == "paused", "vuelve a pausa"


# ------------------------------------------------------------------------------------------------------------------ REQ-004: la pulsacion que enciende no habla
def test_the_press_that_turns_it_on_does_not_send_anything_when_released(ptt, provider):
    provider.whisper.script[:] = [("esto no deberia ir", 0.0)]
    ptt.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    ptt.page.keyboard.down("Space")
    ptt.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(1500)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_timeout(2500)

    assert sent(ptt) == [], "el Espacio que enciende solo enciende"
    assert banner_state(ptt) == "paused"


# ------------------------------------------------------------------------------------------------------------------ REQ-005: Vision hablando / procesando
def test_holding_space_while_vision_speaks_interrupts_it_and_records_without_discarding_pending(ptt, provider):
    turn_on_with_space(ptt)
    ptt.page.evaluate("window.__visionSpeaking = true")
    provider.whisper.script[:] = [("te interrumpo", 0.0)]

    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_function("window.__cancelCalls >= 1", timeout=3_000)
    assert ptt.page.evaluate("window.__visionSpeaking") is False, "Espacio corta la voz de ULTRON"
    wait_banner(ptt, "listening", timeout=3_000)
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)

    assert "te interrumpo" in sent(ptt)[0]["message"]


def test_holding_space_while_processing_goes_out_as_a_new_message_and_does_not_cancel_the_turn(ptt, provider):
    provider.delay = 3.0
    turn_on_with_space(ptt)
    # Los DOS textos se cargan ANTES de hablar, en orden: `hold_and_say` reemplazaria el guion mientras la 1.a transcripcion sigue en vuelo y, con la maquina cargada, la dejaria sin su texto (carrera del test, no de la app).
    provider.whisper.script[:] = [("primera pregunta", 0.0), ("y tambien esto otro", 0.0)]
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    # Espera a que la 1.a SALGA de verdad (su transcripcion termino y el turno esta en curso). Si se aprieta antes, la 2.a frase se suma a la 1.a en UN solo mensaje (comportamiento correcto de la app, pero no lo que mide este test).
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=20_000)
    ptt.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'processing'", timeout=10_000)

    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 2", timeout=40_000)

    chats = sent(ptt)
    assert len(chats) == 2, chats
    assert "primera pregunta" in chats[0]["message"] and "esto otro" in chats[1]["message"]


# ------------------------------------------------------------------------------------------------------------------ REQ-006: soltar nunca se pierde
def test_losing_the_window_focus_while_holding_space_counts_as_releasing(ptt, provider):
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("me fui de la ventana", 0.0)]
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(900)

    ptt.page.evaluate("window.dispatchEvent(new Event('blur'))")  # Alt+Tab: el navegador NO manda el keyup
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)

    assert "me fui de la ventana" in sent(ptt)[0]["message"]


def test_escape_turns_everything_off_and_the_following_keyup_does_nothing(ptt, provider):
    turn_on_with_space(ptt)
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(700)

    ptt.page.keyboard.press("Escape")
    ptt.page.keyboard.up("Space")
    ptt.page.wait_for_timeout(1500)

    assert btn_state(ptt) == "idle"
    assert sent(ptt) == []
    assert not ptt.page.locator("#brain-banner").is_visible()


# ------------------------------------------------------------------------------------------------------------------ REQ-007: no depende del interruptor de interrumpir
def test_push_mode_works_even_if_the_interrupt_shortcut_switch_is_off(browser, base_url, provider):
    ctx, a = _make_app(browser, base_url, provider, {MODE_KEY: "push", "vision.brain.spaceInterruptEnabled": "false"})
    try:
        turn_on_with_space(a)
        hold_and_say(a, provider, "con el interruptor apagado")
        a.page.wait_for_function("window.__chat.length >= 1", timeout=15_000)
        assert "con el interruptor apagado" in sent(a)[0]["message"]
    finally:
        ctx.__exit__(None, None, None)


# ------------------------------------------------------------------------------------------------------------------ REQ-008: cartel
def test_the_banner_says_what_to_do_in_each_push_state(ptt, provider):
    provider.delay = 3.0
    turn_on_with_space(ptt)
    paused = banner_text(ptt)
    assert "en pausa" in paused.lower() and "Espacio" in paused and "Esc" in paused and "mantené" in paused.lower()

    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    wait_banner(ptt, "listening", timeout=3_000)
    listening = banner_text(ptt)
    assert "soltá" in listening.lower() and "enviar" in listening.lower() and "Esc" in listening

    provider.whisper.script[:] = [("hola", 0.0)]
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    wait_banner(ptt, "processing", timeout=10_000)
    processing = banner_text(ptt)
    assert "mantené" in processing.lower() and "seguir hablando" in processing.lower()


def test_the_keys_in_the_push_banner_are_real_keys(ptt):
    turn_on_with_space(ptt)

    keys = [k.strip() for k in ptt.page.locator("#brain-banner kbd").all_inner_texts()]
    assert "Espacio" in keys and "Esc" in keys


def test_in_automatic_mode_the_banner_says_exactly_what_it_said_before(auto):
    auto.page.click("#brain-btn")
    auto.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    auto.page.wait_for_timeout(1200)

    text = banner_text(auto)
    assert "te estoy escuchando" in text and "enviar lo que dijiste" in text and "para apagarlo" in text
    assert "pausa" not in text.lower()


# ------------------------------------------------------------------------------------------------------------------ REQ-001: automatico = como antes
def test_in_automatic_mode_releasing_space_never_sends_anything_by_itself(auto, provider):
    """Con el modo Automatico, mantener y soltar Espacio NO es hablar: no cambia el comportamiento de la 0.34.1."""
    auto.page.click("#brain-btn")
    auto.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    auto.page.wait_for_timeout(1200)

    auto.page.keyboard.down("Space")
    auto.page.wait_for_timeout(500)
    auto.page.keyboard.up("Space")
    auto.page.wait_for_timeout(1500)

    assert sent(auto) == [], "sin haber dicho nada, nada se envia"
    assert banner_state(auto) in {"listening", "processing"}, "sigue el cartel de siempre, no el de pausa"


def test_the_default_mode_is_automatic_and_the_selector_says_so(auto):
    auto.open_audio_settings()

    assert auto.page.locator("#cfg-brain-listen-mode").input_value() == "auto"
    assert auto.page.locator("#cfg-brain-listen-mode option").all_inner_texts()[0].startswith("Automático")


# ------------------------------------------------------------------------------------------------------------------ REQ-009: sensibilidad
SENS = {"very_noisy": 0.05, "noisy": 0.028, "normal": 0.012, "quiet": 0.007}


def _state_at(app, rms: float) -> str:
    level(app, rms)
    app.page.wait_for_timeout(900)
    state = btn_state(app)
    level(app, 0.0)
    app.page.wait_for_timeout(900)
    return state


def _auto_with(browser, base_url, provider, sens: str | None):
    prefs = {SENS_KEY: sens} if sens is not None else {}
    ctx, a = _make_app(browser, base_url, provider, prefs)
    a.page.click("#brain-btn")
    a.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    a.page.wait_for_timeout(1500)
    return ctx, a


def test_the_default_sensitivity_is_normal_which_is_todays_threshold(browser, base_url, provider):
    ctx, a = _auto_with(browser, base_url, provider, None)
    try:
        assert _state_at(a, 0.010) == "idle-active", "por debajo de 0,012: silencio"
        assert _state_at(a, 0.014) == "listening", "por encima de 0,012: voz (como hoy)"
    finally:
        ctx.__exit__(None, None, None)


def test_noisy_sensitivity_ignores_a_soft_sound_that_normal_would_hear(browser, base_url, provider):
    ctx, a = _auto_with(browser, base_url, provider, "noisy")
    try:
        assert _state_at(a, 0.020) == "idle-active", "0,020 es ruido para «Ruidoso» (umbral 0,028)"
        assert _state_at(a, 0.035) == "listening"
    finally:
        ctx.__exit__(None, None, None)


def test_very_noisy_sensitivity_needs_a_strong_voice(browser, base_url, provider):
    ctx, a = _auto_with(browser, base_url, provider, "very_noisy")
    try:
        assert _state_at(a, 0.040) == "idle-active", "0,040 no alcanza el umbral 0,05"
        assert _state_at(a, 0.07) == "listening"
    finally:
        ctx.__exit__(None, None, None)


def test_quiet_sensitivity_hears_a_voice_normal_would_miss(browser, base_url, provider):
    ctx, a = _auto_with(browser, base_url, provider, "quiet")
    try:
        assert _state_at(a, 0.004) == "idle-active", "por debajo de 0,007 sigue siendo silencio"
        assert _state_at(a, 0.009) == "listening", "0,009 con «Normal» no se oiria (umbral 0,012)"
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("bad", ["", "xyz", "0.5", "null", "{\"a\":1}"])
def test_an_unknown_saved_sensitivity_is_treated_as_normal(browser, base_url, provider, bad):
    ctx, a = _auto_with(browser, base_url, provider, bad)
    try:
        assert _state_at(a, 0.010) == "idle-active"
        assert _state_at(a, 0.014) == "listening"
    finally:
        ctx.__exit__(None, None, None)


def test_changing_the_sensitivity_applies_live_with_the_mode_already_on(auto):
    auto.page.click("#brain-btn")
    auto.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    auto.page.wait_for_timeout(1500)
    assert _state_at(auto, 0.020) == "listening", "con Normal 0,020 es voz"

    auto.open_audio_settings()
    auto.page.select_option("#cfg-brain-sensitivity", "noisy")

    assert _state_at(auto, 0.020) == "idle-active", "con «Ruidoso» 0,020 ya es ruido, sin reiniciar el modo"


# ------------------------------------------------------------------------------------------------------------------ REQ-010: marca en el medidor
def test_the_level_meter_shows_a_mark_that_moves_with_the_sensitivity(auto):
    auto.open_audio_settings()
    mark = auto.page.locator("#cfg-mic-meter-mark")
    assert mark.count() == 1, "hay una marca del umbral en el medidor"

    def left() -> float:
        return float(auto.page.evaluate("parseFloat(document.getElementById('cfg-mic-meter-mark').style.left)"))

    auto.page.select_option("#cfg-brain-sensitivity", "normal")
    normal = left()
    auto.page.select_option("#cfg-brain-sensitivity", "very_noisy")
    very_noisy = left()
    auto.page.select_option("#cfg-brain-sensitivity", "quiet")
    quiet = left()

    assert normal == pytest.approx(3.6, abs=0.2), "0,012 x 300 = 3,6 %"
    assert very_noisy == pytest.approx(15.0, abs=0.2), "0,05 x 300 = 15 %"
    assert quiet == pytest.approx(2.1, abs=0.2), "0,007 x 300 = 2,1 %"
    assert quiet < normal < very_noisy


# ------------------------------------------------------------------------------------------------------------------ REQ-012: se guardan, y el selector se deshabilita
def test_the_two_preferences_are_saved_and_survive_a_reload(auto):
    auto.open_audio_settings()
    auto.page.select_option("#cfg-brain-sensitivity", "quiet")  # primero esta: con «Mantener Espacio» elegido queda deshabilitada
    auto.page.select_option("#cfg-brain-listen-mode", "push")
    assert auto.page.evaluate(f"localStorage.getItem({json.dumps(MODE_KEY)})") == "push"
    assert auto.page.evaluate(f"localStorage.getItem({json.dumps(SENS_KEY)})") == "quiet"

    auto.page.reload(wait_until="domcontentloaded")
    auto.wait_ready()
    auto.open_audio_settings()

    assert auto.page.locator("#cfg-brain-listen-mode").input_value() == "push"
    assert auto.page.locator("#cfg-brain-sensitivity").input_value() == "quiet"


def test_the_sensitivity_selector_is_disabled_with_push_mode_and_says_why(auto):
    auto.open_audio_settings()
    assert auto.page.locator("#cfg-brain-sensitivity").is_enabled()

    auto.page.select_option("#cfg-brain-listen-mode", "push")

    assert auto.page.locator("#cfg-brain-sensitivity").is_disabled()
    assert "Automático" in auto.page.locator("#cfg-brain-sensitivity-note").inner_text()

    auto.page.select_option("#cfg-brain-listen-mode", "auto")
    assert auto.page.locator("#cfg-brain-sensitivity").is_enabled()


def test_the_sensitivity_options_are_in_words_not_numbers(auto):
    auto.open_audio_settings()

    labels = auto.page.locator("#cfg-brain-sensitivity option").all_inner_texts()

    assert len(labels) == 4
    assert any("Muy ruidoso" in x for x in labels) and any("Normal" in x and "recomendado" in x.lower() for x in labels)
    assert not any(x.strip().replace(",", "").replace(".", "").isdigit() for x in labels)


# ------------------------------------------------------------------------------------------------------------------ REQ-011: solapa Audio agrupada
GROUPS = ["Voz de ULTRON", "Micrófono", "Modo Cerebro", "Avisos y alarmas"]


def _group_of(app, element_id: str) -> str:
    """Titulo del grupo (`.settings-subheading`) que precede al elemento en la solapa Audio."""
    return app.page.evaluate(
        """(id) => {
          const pane = document.querySelector('.settings-pane[data-pane="audio"]');
          const el = document.getElementById(id);
          if (!el) return "(no existe)";
          let current = "(sin grupo)";
          for (const node of pane.querySelectorAll('.settings-group-title, #' + CSS.escape(id))) {
            if (node === el) return current;
            current = node.textContent.trim();
          }
          return current;
        }""",
        element_id,
    )


def test_the_audio_tab_has_the_four_groups_in_order(auto):
    auto.open_audio_settings()

    titles = auto.page.eval_on_selector_all(
        '.settings-pane[data-pane="audio"] .settings-group-title', "els => els.map(e => e.textContent.trim())"
    )

    assert [t for t in titles if any(t.startswith(g) for g in GROUPS)][:4] == [
        next(t for t in titles if t.startswith(g)) for g in GROUPS
    ]


@pytest.mark.parametrize(
    "element_id, group",
    [
        ("cfg-tts-enabled", "Voz de ULTRON"),
        ("cfg-tts-voice-select", "Voz de ULTRON"),
        ("cfg-voice-chain", "Voz de ULTRON"),
        ("cfg-voice-sample", "Voz de ULTRON"),
        ("cfg-mic-select", "Micrófono"),
        ("cfg-winmic-block", "Micrófono"),
        ("cfg-mic-meter-fill", "Micrófono"),
        ("cfg-stt-language-select", "Micrófono"),
        ("cfg-meeting-source", "Micrófono"),
        ("cfg-brain-autosend-enabled", "Modo Cerebro"),
        ("cfg-brain-autosend-pause", "Modo Cerebro"),
        ("cfg-brain-space-interrupt-enabled", "Modo Cerebro"),
        ("cfg-brain-listen-mode", "Modo Cerebro"),
        ("cfg-brain-sensitivity", "Modo Cerebro"),
        ("cfg-notify-finish", "Avisos y alarmas"),
        ("cfg-loop-alarm-type", "Avisos y alarmas"),
    ],
)
def test_every_control_sits_inside_the_group_it_belongs_to(auto, element_id, group):
    auto.open_audio_settings()

    assert _group_of(auto, element_id).startswith(group)


def test_no_control_id_of_the_audio_tab_was_lost_or_duplicated(auto):
    auto.open_audio_settings()

    ids = auto.page.eval_on_selector_all('.settings-pane[data-pane="audio"] [id]', "els => els.map(e => e.id)")

    assert len(ids) == len(set(ids)), "ningun id duplicado"
    for needed in [
        "cfg-tts-enabled", "cfg-tts-tech-report", "cfg-tts-tech-level", "cfg-tts-voice-select", "cfg-tts-test-btn", "cfg-voice-chain",
        "cfg-voice-add", "cfg-voice-save", "cfg-voice-sample", "cfg-mic-select", "cfg-stt-language-select", "cfg-meeting-source", "cfg-meeting-ask",
        "cfg-winmic-block", "cfg-winmic-select", "cfg-winmic-slider", "cfg-mic-meter-fill", "cfg-mic-status", "cfg-brain-autosend-enabled",
        "cfg-brain-autosend-pause", "cfg-brain-space-interrupt-enabled", "cfg-notify-finish", "cfg-loop-alarm-type", "cfg-loop-alarm-test",
        "cfg-brain-listen-mode", "cfg-brain-sensitivity", "cfg-mic-meter-mark",
    ]:
        assert needed in ids, f"se perdio #{needed}"


# ------------------------------------------------------------------------------------------------------------------ cierra mutaciones P1, P2, P3, P4, P6, P9, P10, P11 y P19
def test_in_push_mode_the_voice_detector_never_starts_a_phrase_on_its_own(ptt, provider):
    """P1: el detector esta FUERA. Sin apretar nada, ni siquiera dibuja las ondas ni marca «hay voz» aunque el ruido sea fortisimo."""
    turn_on_with_space(ptt)

    level(ptt, 0.2)
    ptt.page.wait_for_timeout(2500)

    assert ptt.page.evaluate("window.__recStarts") == 0, "el detector no abrio ninguna grabacion"
    assert provider.whisper.calls == 0
    assert "hidden" in (ptt.page.get_attribute("#brain-waveform", "class") or ""), "las ondas no se dibujan sin apretar"


def test_keyboard_repeat_while_held_neither_restarts_the_recording_nor_stacks_phrases(ptt, provider):
    """P2: con la repeticion del teclado, la grabacion en curso sigue siendo la MISMA (no se reinicia ni se apilan frases)."""
    turn_on_with_space(ptt)
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(500)
    assert ptt.page.evaluate("window.__recStarts") == 1, "apretar abrio UNA grabacion"

    for _ in range(4):
        ptt.page.keyboard.down("Space")  # repeticion automatica: e.repeat = true
        ptt.page.wait_for_timeout(150)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)
    ptt.page.wait_for_timeout(1200)

    assert ptt.page.evaluate("window.__recStarts") == 1, "la repeticion no abre grabaciones nuevas"


def test_the_release_of_the_press_that_turned_it_on_does_not_send_even_with_noise_and_text_pending(ptt, provider):
    """P3 y P4: con voz fuerte durante TODA la pulsacion que enciende, al soltarla no se envia nada ni se llama a Whisper."""
    provider.whisper.script[:] = [("no deberia ir", 0.0)]
    ptt.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    ptt.page.keyboard.down("Space")
    ptt.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(1500)

    ptt.page.keyboard.up("Space")
    ptt.page.wait_for_timeout(2500)

    assert ptt.page.evaluate("window.__recStarts") == 0, "esa pulsacion no abrio ninguna grabacion"
    assert provider.whisper.calls == 0, "no se transcribio nada"
    assert sent(ptt) == []


def test_hiding_the_tab_while_holding_space_counts_as_releasing(ptt, provider):
    """P6: el navegador oculta la pestana (cambio de pestana) con la barra apretada y no manda el keyup."""
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("cambie de pestana", 0.0)]
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(900)

    ptt.page.evaluate("Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange'));")
    level(ptt, 0.0)
    ptt.page.wait_for_function("window.__chat.length >= 1", timeout=30_000)

    assert "cambie de pestana" in sent(ptt)[0]["message"]


def test_holding_space_while_vision_speaks_keeps_what_was_already_said_and_pending(ptt, provider):
    """P9: lo dicho mientras Vision procesaba y esta en cola NO se descarta al interrumpirla (a diferencia del modo automatico)."""
    provider.delay = 6.0
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("primera pregunta", 0.0), ("lo que quedo en cola", 0.0), ("y ahora te interrumpo", 0.0)]
    for _ in range(2):  # 1.a: sale y queda procesando; 2.a: queda EN COLA (hay un turno en curso)
        ptt.page.keyboard.down("Space")
        level(ptt, 0.2)
        ptt.page.wait_for_timeout(900)
        ptt.page.keyboard.up("Space")
        level(ptt, 0.0)
        ptt.page.wait_for_timeout(1800)
    assert len(sent(ptt)) == 1, "la 2.a frase espera su turno: todavia no salio"

    ptt.page.evaluate("window.__visionSpeaking = true")
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_function("window.__cancelCalls >= 1", timeout=3_000)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)

    ptt.page.wait_for_function("window.__chat.length >= 2", timeout=30_000)  # si interrumpir la hubiera borrado, esto nunca llegaria
    assert any("lo que quedo en cola" in c["message"] for c in sent(ptt)), "interrumpir a ULTRON no borra lo que dijiste"


def test_in_push_mode_a_finished_turn_sends_the_queued_text_right_away_not_after_the_autosend_pause(browser, base_url, provider):
    """P10: con auto-envio y una pausa LARGA (30 s), en modo push lo que quedo en cola sale al terminar el turno, no tras 30 s de «silencio»."""
    ctx, a = _make_app(browser, base_url, provider, {MODE_KEY: "push", "vision.brain.autosendPauseMs": "30000"})
    try:
        provider.delay = 3.0
        turn_on_with_space(a)
        provider.whisper.script[:] = [("primera pregunta", 0.0), ("segunda en cola", 0.0)]
        for _ in range(2):
            a.page.keyboard.down("Space")
            level(a, 0.2)
            a.page.wait_for_timeout(900)
            a.page.keyboard.up("Space")
            level(a, 0.0)
            a.page.wait_for_timeout(1500)

        a.page.wait_for_function("window.__chat.length >= 2", timeout=20_000)

        assert "segunda en cola" in sent(a)[1]["message"]
    finally:
        ctx.__exit__(None, None, None)


def test_escape_while_holding_space_leaves_nothing_held_and_the_release_does_nothing(ptt, provider):
    """P11: Esc con la barra apretada deja el estado limpio; encender de nuevo y soltar no envia nada."""
    turn_on_with_space(ptt)
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)
    ptt.page.wait_for_timeout(600)

    ptt.page.keyboard.press("Escape")

    assert btn_state(ptt) == "idle", "Esc apaga todo"
    ptt.page.keyboard.up("Space")
    ptt.page.wait_for_timeout(800)
    assert sent(ptt) == []
    turn_on_with_space(ptt)  # el modo vuelve a encenderse limpio y sordo
    assert banner_state(ptt) == "paused"


def test_releasing_space_with_something_in_flight_but_nothing_said_warns_in_the_banner(ptt, provider):
    """P19: se aprieta con ruido de fondo pero sin voz (Whisper devuelve vacio): se avisa «Todavia no dijiste nada.» en el cartel."""
    turn_on_with_space(ptt)
    provider.whisper.script[:] = [("", 0.0)]  # Whisper no entiende nada
    ptt.page.keyboard.down("Space")
    level(ptt, 0.2)  # hay energia: la frase SI se graba y se transcribe, pero vuelve vacia
    ptt.page.wait_for_timeout(900)
    ptt.page.keyboard.up("Space")
    level(ptt, 0.0)

    ptt.page.wait_for_function("document.getElementById('brain-banner-text').textContent.includes('Todavía no dijiste nada.')", timeout=10_000)

    assert sent(ptt) == []
