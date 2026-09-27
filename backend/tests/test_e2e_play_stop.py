"""E2E del boton de reproducir voz (🔊/⏹) en un Chromium REAL contra el backend
real (spec `fix-boton-play-stop`, APPROVED 2026-09-26).

Bug real reportado por el usuario: al presionar ⏹ el icono pasaba a 🔊
(detenido) ANTES de que el audio realmente se cortara -- mas notorio en
respuestas largas. Causa raiz: `speechSynthesis.cancel()` de la Web Speech API
no garantiza corte instantaneo en todos los navegadores/utterances, y
`stopActivePlayback()` (app.js) soltaba el icono de forma optimista sin
verificar el estado real.

Estos tests mockean `window.speechSynthesis` con un DELAY simulado antes de
que `cancel()` marque `speaking = false` -- reproduce el bug real de forma
determinista (sin depender del motor de voz real de cada SO/navegador).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"

# Mock de Web Speech API inyectado ANTES de que cargue app.js. `speak()` deja
# `speaking = true` hasta que `cancel()` es llamado; `cancel()` simula el
# delay real del navegador antes de soltar `speaking` (configurable via
# `window.__cancelDelayMs`, default 0 -- los tests que necesitan el delay lo
# fijan explicitamente). `window.__cancelCalls`/`__speakCalls` cuentan
# llamadas para verificar que efectivamente se invoco cada una.
SPEECH_MOCK_SCRIPT = """
(() => {
  window.__cancelDelayMs = 0;
  window.__cancelCalls = 0;
  window.__speakCalls = 0;
  const fake = {
    speaking: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: (utter) => {
      window.__speakCalls += 1;
      fake.speaking = true;
      // Nunca dispara onend por su cuenta -- el test controla el fin
      // llamando a cancel() explicitamente, igual que el boton ⏹.
    },
    cancel: () => {
      window.__cancelCalls += 1;
      const delay = window.__cancelDelayMs;
      if (delay <= 0) {
        fake.speaking = false;
        return;
      }
      // Simula el delay real de corte del navegador: `speaking` sigue en
      // true un rato despues de llamar a cancel(), como el bug real.
      setTimeout(() => { fake.speaking = false; }, delay);
    },
    onvoiceschanged: null,
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""


class FakeProvider:
    """Cliente del proveedor de IA falso: responde una respuesta larga fija (para tener
    algo que reproducir con el boton 🔊), mismo patron minimo que los otros
    modulos e2e."""

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
        delta = SimpleNamespace(
            content="[[TIPO: conversacional]]Respuesta larga de prueba para reproducir.",
            tool_calls=[],
        )
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
    return FakeProvider()


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
        # El mock de speechSynthesis debe existir ANTES de que app.js evalue
        # sus imports (tts.js lee `"speechSynthesis" in window` al cargar).
        a.page.add_init_script(SPEECH_MOCK_SCRIPT)
        a.open()
        # Activa "Leer en voz alta" -- sin esto el boton 🔊 ni siquiera
        # reproduce (ver isTtsEnabled() en handlePlayMessageClick).
        a.page.evaluate("localStorage.setItem('vision.tts.enabled', 'true')")
        a.page.reload(wait_until="domcontentloaded")
        a.page.add_init_script(
            SPEECH_MOCK_SCRIPT
        )  # el reload pierde el mock -- reinyectar
        a.wait_ready()
        yield a


def send_and_get_play_btn(app):
    """Envia un mensaje, espera la respuesta completa, devuelve el selector
    del boton 🔊 de esa respuesta."""
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".msg.assistant:not(.streaming) .msg-play-btn")
    sel = ".msg.assistant:not(.streaming) .msg-play-btn"
    # Con "Leer en voz alta" activo la respuesta se lee sola al terminar y su
    # boton queda en ⏹ (REQ-001): se detiene esa lectura para arrancar desde
    # "callado" el ciclo manual 🔊/⏹ que prueban estos tests.
    app.page.wait_for_function("sel => document.querySelector(sel).dataset.playing === 'true'", arg=sel)
    app.page.click(sel)
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'false'", arg=sel, timeout=3000
    )
    return sel


def test_stop_button_waits_for_real_speech_end_before_releasing_icon(app):
    """REQ-002: con un delay de cancelacion simulado (bug real), el icono NO
    debe volver a 🔊 hasta que `speechSynthesis.speaking` sea realmente
    false -- nunca "miente" mostrando detenido mientras el audio sigue."""
    app.page.evaluate("window.__cancelDelayMs = 400")  # simula el bug real
    btn_selector = send_and_get_play_btn(app)

    app.page.click(btn_selector)  # 🔊 -> arranca a "hablar" (mock)
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'true'",
        arg=btn_selector,
    )

    app.page.click(btn_selector)  # ⏹ -> pide detener
    # Inmediatamente despues del click, mientras el delay simulado corre
    # (400ms), el audio del mock SIGUE "hablando" -- el icono NO debe haber
    # soltado todavia (si soltara ya, seria el bug real reproducido).
    still_speaking = app.page.evaluate("window.speechSynthesis.speaking")
    assert still_speaking is True, (
        "el mock deberia seguir 'hablando' durante el delay simulado"
    )
    icon_right_after_click = app.page.evaluate(
        f"document.querySelector({btn_selector!r}).dataset.playing"
    )
    assert icon_right_after_click == "true", (
        "REQ-002: el icono NO debe pasar a detenido mientras speechSynthesis.speaking sigue en true "
        "-- ese es exactamente el bug real reportado por el usuario"
    )

    # Una vez que el corte real se confirma (pasado el delay), el icono debe
    # soltarse solo, sin necesidad de otra accion del usuario.
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'false'",
        arg=btn_selector,
        timeout=3000,
    )


def test_stop_button_releases_fast_when_cancel_is_instant(app):
    """REQ-003: sin delay de cancelacion (caso comun), el corte debe seguir
    siendo percibido como instantaneo -- no se agrega una espera artificial
    notoria para el caso feliz."""
    app.page.evaluate("window.__cancelDelayMs = 0")
    btn_selector = send_and_get_play_btn(app)

    app.page.click(btn_selector)  # 🔊
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'true'",
        arg=btn_selector,
    )
    app.page.click(btn_selector)  # ⏹
    # Con cancel() instantaneo, el icono debe soltarse en un lapso corto
    # (tope generoso para no ser fragil en CI, pero muy por debajo del
    # STOP_POLL_MAX_MS de 1500ms del codigo).
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'false'",
        arg=btn_selector,
        timeout=500,
    )


def test_play_again_after_stop_starts_immediately(app):
    """REQ-004: no romper el flujo de re-reproducir -- tocar 🔊 de nuevo tras
    un stop debe arrancar sin ninguna demora (usa el camino `immediate`)."""
    app.page.evaluate("window.__cancelDelayMs = 400")
    btn_selector = send_and_get_play_btn(app)

    app.page.click(btn_selector)  # 🔊
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'true'",
        arg=btn_selector,
    )
    app.page.click(btn_selector)  # ⏹ (con delay simulado corriendo en background)
    # Sin esperar a que el delay del stop anterior termine, se vuelve a
    # tocar 🔊 -- debe arrancar de inmediato (immediate: true en
    # _speakFromPlayBtn), no quedar bloqueado esperando el stop previo.
    app.page.click(btn_selector)
    app.page.wait_for_function(
        "sel => document.querySelector(sel).dataset.playing === 'true'",
        arg=btn_selector,
        timeout=500,
    )


# ---------------------------------------------------------------------------
# Bug real reportado por el usuario (2026-09-27): "cuando Vision está hablando y
# presiono la bocina para detener", en vez de cortar volvía a leer desde el
# principio. La lectura AUTOMÁTICA de la respuesta (Leer en voz alta) no quedaba
# atada a su botón: seguía en 🔊, y tocarlo arrancaba otra lectura. Además, el
# aviso tardío de cancel() (Chrome lo manda después, "interrupted") soltaba el
# botón de la lectura NUEVA. Este mock imita eso: cancel() avisa el corte de la
# voz vieja 80 ms después, y cada voz suena hasta que se corta o termina sola.
# ---------------------------------------------------------------------------
REAL_VOICE_MOCK = """
(() => {
  window.__spoken = [];
  window.__cancelCalls = 0;
  let current = null;
  const queue = [];
  const fake = {
    speaking: false,
    pending: false,
    paused: false,
    getVoices: () => [{ name: "Fake ES", lang: "es-419", localService: true, voiceURI: "fake-es" }],
    speak: (utter) => {
      queue.push(utter);
      if (current) return;
      current = queue.shift();
      fake.speaking = true;
      window.__spoken.push(current.text);
    },
    cancel: () => {
      window.__cancelCalls += 1;
      const cut = current;
      queue.length = 0;
      current = null;
      fake.speaking = false;
      if (cut) setTimeout(() => cut.onerror && cut.onerror({ error: "interrupted" }), 80);
    },
    onvoiceschanged: null,
  };
  window.__finishSpeech = () => {
    const done = current;
    current = null;
    fake.speaking = false;
    if (done && done.onend) done.onend({});
  };
  Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  window.SpeechSynthesisUtterance = function (text) { this.text = text; };
})();
"""

BTN = ".msg.assistant:not(.streaming) .msg-play-btn"
TEXT = ".msg.assistant:not(.streaming) .msg-text"


@pytest.fixture
def voice_app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.page.add_init_script(REAL_VOICE_MOCK)
        a.page.add_init_script("localStorage.setItem('vision.tts.enabled', 'true')")
        a.open()
        yield a


def answer_and_wait_auto_read(app):
    app.page.fill("#chat-input", "hola")
    app.page.click("#send-btn")
    app.page.wait_for_selector(BTN)
    app.page.wait_for_function("() => window.__spoken.length === 1")


def playing(app) -> str:
    return app.page.eval_on_selector(BTN, "b => b.dataset.playing")


def wait_playing(app, value: str) -> None:
    app.page.wait_for_function(
        "([sel, v]) => document.querySelector(sel).dataset.playing === v", arg=[BTN, value], timeout=3000
    )


def mark_at(app, offset: int) -> None:
    """Un click sobre el texto en el caracter `offset` (bandera de inicio/fin)."""
    app.page.evaluate(
        """([sel, off]) => {
          const el = document.querySelector(sel);
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let node, acc = 0;
          while ((node = walker.nextNode())) {
            if (acc + node.textContent.length >= off) break;
            acc += node.textContent.length;
          }
          const range = document.createRange();
          range.setStart(node, off - acc);
          range.collapse(true);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        }""",
        [TEXT, offset],
    )


def test_while_vision_reads_the_answer_by_itself_its_button_shows_stop(voice_app):
    answer_and_wait_auto_read(voice_app)
    assert voice_app.page.evaluate("window.__spoken[0]").startswith("Respuesta larga de prueba")
    wait_playing(voice_app, "true")  # REQ-001 estado 1: hablando -> ⏹
    assert voice_app.page.text_content(BTN) == "⏹"


def test_pressing_it_while_vision_reads_cuts_the_voice_for_good(voice_app):
    answer_and_wait_auto_read(voice_app)
    wait_playing(voice_app, "true")
    voice_app.page.click(BTN)
    wait_playing(voice_app, "false")
    voice_app.page.wait_for_timeout(400)  # pasa el aviso tardio de cancel()
    assert voice_app.page.evaluate("window.speechSynthesis.speaking") is False
    assert voice_app.page.evaluate("window.__spoken.length") == 1  # no volvio a leer desde el principio
    assert playing(voice_app) == "false" and voice_app.page.text_content(BTN) == "🔊"


def test_after_stopping_pressing_again_reads_the_whole_answer_from_the_start(voice_app):
    answer_and_wait_auto_read(voice_app)
    wait_playing(voice_app, "true")
    voice_app.page.click(BTN)
    wait_playing(voice_app, "false")
    voice_app.page.click(BTN)  # REQ-001 estado 2, sin rango: desde el inicio
    wait_playing(voice_app, "true")
    full = voice_app.page.text_content(TEXT)
    assert voice_app.page.evaluate("window.__spoken.length") == 2
    assert voice_app.page.evaluate("window.__spoken[1]").strip() == full.strip()


def test_after_stopping_with_a_range_marked_it_reads_only_the_range(voice_app):
    answer_and_wait_auto_read(voice_app)
    wait_playing(voice_app, "true")
    voice_app.page.click(BTN)
    wait_playing(voice_app, "false")
    mark_at(voice_app, 10)
    mark_at(voice_app, 25)
    voice_app.page.click(BTN)  # REQ-001 estado 2, con rango: solo ese fragmento
    wait_playing(voice_app, "true")
    full = voice_app.page.text_content(TEXT)
    assert voice_app.page.evaluate("window.__spoken.at(-1)") == full[10:25]


def test_a_late_cancel_notice_does_not_release_the_new_reading(voice_app):
    answer_and_wait_auto_read(voice_app)
    wait_playing(voice_app, "true")
    voice_app.page.click(BTN)  # corta
    wait_playing(voice_app, "false")
    voice_app.page.click(BTN)  # vuelve a leer ya mismo
    wait_playing(voice_app, "true")
    voice_app.page.wait_for_timeout(400)  # llega el "interrupted" de la voz cortada
    assert playing(voice_app) == "true"  # la lectura nueva sigue marcada: no miente
    voice_app.page.click(BTN)  # y el boton la detiene, no la reinicia
    wait_playing(voice_app, "false")
    voice_app.page.wait_for_timeout(400)
    assert voice_app.page.evaluate("window.__spoken.length") == 2


def test_a_reading_that_ends_by_itself_releases_the_button(voice_app):
    answer_and_wait_auto_read(voice_app)
    wait_playing(voice_app, "true")
    voice_app.page.evaluate("window.__finishSpeech()")
    wait_playing(voice_app, "false")
