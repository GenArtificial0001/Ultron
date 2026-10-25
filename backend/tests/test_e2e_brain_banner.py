"""E2E del cartel de estado del Modo Cerebro (spec cerebro-cartel-estado) en un Chromium REAL contra el backend real.

Pedido del usuario (2026-10-19): «cuando activo modo cerebro nadie sabe como apagarlo y es tan sencillo como presionar la tecla escape [...] debe mostrar una leyenda parecida pero en verde si esta
escuchando, amarillo si esta procesando y no visible si esta apagado el modo cerebro con un diseño parecido a persistence-banner».

Lo que ve la persona:
1. Con el modo apagado no hay cartel. Al encenderlo aparece, y dice como apagarlo (Esc, que es la UNICA forma: tocar el icono con el modo activo no lo apaga).
2. Verde mientras escucha, ambar mientras procesa. En el hueco en que el boton se pone rojo (activo, en silencio) el cartel sigue verde: NUNCA rojo.
3. Esc lo apaga y el cartel desaparece; tocar el icono con el modo activo NO lo apaga y el cartel sigue. Se ve como el banner de persistencia (misma franja) y no lo pisa.
4. Es accesible (`role="status"`, `aria-live`) y el estado se dice con texto, no solo con color.

Como se simula la voz, sin microfono fisico: igual que `test_e2e_brain_autosend.py`, un oscilador con una ganancia que el TEST controla (`window.__setVoice`). Whisper y el modelo son falsos.
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
    with launch_chromium(tmp_path_factory.mktemp("brainbanner"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url, provider):
    provider.whisper.script.clear()
    provider.delay = 0.0
    ctx = open_app(browser, base_url)
    a = ctx.__enter__()
    a.page.add_init_script(SYNTH_MIC)
    a.page.add_init_script("localStorage.setItem('vision.brain.autosendPauseMs', '2'); localStorage.setItem('vision.brain.autosendEnabled', 'true');")
    a.open()
    yield a
    ctx.__exit__(None, None, None)


def voice(app, on: bool) -> None:
    app.page.evaluate(f"window.__setVoice({json.dumps(on)})")


def turn_on(app) -> None:
    app.page.click("#brain-btn")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state !== 'idle'", timeout=10_000)
    app.page.wait_for_timeout(1200)  # pasa el enfriamiento inicial del detector de voz


def banner_visible(app) -> bool:
    return app.page.locator("#brain-banner").is_visible()


def banner_state(app) -> str:
    return app.page.locator("#brain-banner").get_attribute("data-state") or ""


def banner_text(app) -> str:
    return app.page.locator("#brain-banner").inner_text()


def wait_state(app, state: str, timeout: int = 10_000) -> None:
    app.page.wait_for_function(f"document.getElementById('brain-banner').dataset.state === {state!r}", timeout=timeout)


# ------------------------------------------------------------------------------------------------------------------ REQ-001/002: apagado = invisible
def test_the_banner_exists_hidden_and_takes_no_space_when_the_mode_is_off(app):
    assert app.page.locator("#brain-banner").count() == 1
    assert not banner_visible(app)
    box = app.page.locator("#brain-banner").bounding_box()
    assert box is None or box["height"] == 0, "oculto no ocupa lugar"


def test_turning_the_mode_on_shows_the_banner(app):
    turn_on(app)

    assert banner_visible(app)


# ------------------------------------------------------------------------------------------------------------------ REQ-003: verde / ambar, nunca rojo
def test_it_is_green_and_says_it_is_listening_while_the_voice_is_detected(app):
    turn_on(app)
    voice(app, True)
    wait_state(app, "listening")

    assert banner_state(app) == "listening"
    assert "escuchando" in banner_text(app).lower()
    assert app.page.evaluate("getComputedStyle(document.getElementById('brain-banner')).color") == app.page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--signal-green').trim() ? (()=>{const s=document.createElement('span');s.style.color=getComputedStyle(document.documentElement).getPropertyValue('--signal-green').trim();document.body.appendChild(s);const c=getComputedStyle(s).color;s.remove();return c;})() : ''")


def test_it_is_amber_and_says_it_is_processing_while_the_model_works(app, provider):
    provider.delay = 3.0
    provider.whisper.script[:] = [("hola visión", 0.0)]
    turn_on(app)
    voice(app, True)
    app.page.wait_for_timeout(900)
    voice(app, False)
    wait_state(app, "processing", timeout=15_000)

    assert "procesando" in banner_text(app).lower()
    amber = app.page.evaluate("(()=>{const s=document.createElement('span');s.style.color=getComputedStyle(document.documentElement).getPropertyValue('--signal-amber').trim();document.body.appendChild(s);const c=getComputedStyle(s).color;s.remove();return c;})()")
    assert app.page.evaluate("getComputedStyle(document.getElementById('brain-banner')).color") == amber


def test_in_silence_the_button_is_red_but_the_banner_stays_green_and_never_red(app):
    """Activo y en silencio: el boton se pone rojo; el cartel NO (seria asustar) y NO se oculta (ahi justo hay que saber como apagarlo)."""
    turn_on(app)
    voice(app, False)
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'idle-active'", timeout=10_000)

    assert banner_visible(app) and banner_state(app) == "listening"
    red = app.page.evaluate("(()=>{const s=document.createElement('span');s.style.color=getComputedStyle(document.documentElement).getPropertyValue('--signal-red').trim();document.body.appendChild(s);const c=getComputedStyle(s).color;s.remove();return c;})()")
    assert app.page.evaluate("getComputedStyle(document.getElementById('brain-banner')).color") != red


def test_the_banner_never_takes_any_other_state_than_listening_or_processing(app, provider):
    provider.delay = 1.5
    provider.whisper.script[:] = [("hola visión", 0.0)]
    turn_on(app)
    seen: set[str] = set()
    voice(app, True)
    for _ in range(14):
        app.page.wait_for_timeout(250)
        seen.add(banner_state(app))
        if _ == 4:
            voice(app, False)

    assert seen <= {"listening", "processing"}, seen


# ------------------------------------------------------------------------------------------------------------------ REQ-004: como apagarlo
def test_the_banner_says_how_to_turn_it_off(app):
    turn_on(app)

    text = banner_text(app)
    assert "Esc" in text and "apagarlo" in text.lower()
    assert "icono" not in text.lower(), "tocar el icono con el modo activo NO lo apaga: el cartel no puede decir que si"
    assert "Esc" in [k.strip() for k in app.page.locator("#brain-banner kbd").all_inner_texts()]


# ------------------------------------------------------------------------------------------------------------------ REQ-005: se apaga y desaparece
def test_pressing_escape_turns_it_off_and_hides_the_banner(app):
    turn_on(app)
    assert banner_visible(app)

    app.page.keyboard.press("Escape")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'idle'", timeout=10_000)

    assert not banner_visible(app)


def test_touching_the_brain_icon_while_active_does_not_turn_it_off_and_the_banner_stays(app):
    """Decision anterior del usuario («una vez activo, SOLO Escape lo apaga»): un clic accidental no corta el microfono. El cartel tiene que reflejarlo: sigue visible."""
    turn_on(app)

    app.page.click("#brain-btn")
    app.page.wait_for_timeout(800)

    assert app.page.locator("#brain-btn").get_attribute("data-state") != "idle"
    assert banner_visible(app)


def test_it_comes_back_when_the_mode_is_turned_on_again(app):
    turn_on(app)
    app.page.keyboard.press("Escape")
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'idle'", timeout=10_000)
    assert not banner_visible(app)

    turn_on(app)

    assert banner_visible(app)


# ------------------------------------------------------------------------------------------------------------------ REQ-006: accesible
def test_it_is_a_polite_live_region_and_the_state_is_said_in_words_not_only_in_color(app):
    el = app.page.locator("#brain-banner")

    assert el.get_attribute("role") == "status" and el.get_attribute("aria-live") == "polite"
    turn_on(app)
    voice(app, True)
    wait_state(app, "listening")
    listening = banner_text(app)
    assert "escuchando" in listening.lower()


def test_the_icon_alone_is_not_the_only_indicator(app):
    """El icono es decoracion: si se quita, el texto sigue diciendo el estado y como apagar."""
    turn_on(app)

    assert app.page.locator("#brain-banner .brain-banner-icon").get_attribute("aria-hidden") == "true"
    assert len(banner_text(app).strip()) > 20


# ------------------------------------------------------------------------------------------------------------------ REQ-007/008: no rompe nada, convive con el banner de persistencia
def test_the_brain_button_and_its_states_are_unchanged(app):
    assert app.page.locator("#brain-btn").get_attribute("data-state") == "idle"
    turn_on(app)
    voice(app, True)
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'listening'", timeout=10_000)
    voice(app, False)
    app.page.wait_for_function("document.getElementById('brain-btn').dataset.state === 'idle-active'", timeout=10_000)


def test_it_looks_like_the_persistence_banner_same_strip_same_type_and_border(app):
    turn_on(app)

    def style(selector: str) -> dict:
        return app.page.evaluate(
            "(sel) => { const e = document.querySelector(sel); const s = getComputedStyle(e);"
            " return { display: s.display, fontSize: s.fontSize, paddingTop: s.paddingTop, paddingLeft: s.paddingLeft, gap: s.gap, borderBottom: s.borderBottomWidth, align: s.alignItems }; }",
            selector,
        )

    app.page.evaluate("document.getElementById('persistence-banner').classList.remove('hidden')")
    brain, persistence = style("#brain-banner"), style("#persistence-banner")

    assert brain == persistence, (brain, persistence)


def test_it_stacks_below_the_persistence_banner_without_covering_it(app):
    turn_on(app)
    app.page.evaluate("document.getElementById('persistence-banner').classList.remove('hidden')")

    p = app.page.locator("#persistence-banner").bounding_box()
    b = app.page.locator("#brain-banner").bounding_box()

    assert p and b and b["y"] >= p["y"] + p["height"] - 1, (p, b)
    assert app.page.evaluate("document.getElementById('persistence-banner').compareDocumentPosition(document.getElementById('brain-banner')) & Node.DOCUMENT_POSITION_FOLLOWING")


def test_the_banner_text_is_fixed_and_carries_no_html_from_anywhere(app):
    turn_on(app)

    assert app.page.locator("#brain-banner script, #brain-banner img").count() == 0
