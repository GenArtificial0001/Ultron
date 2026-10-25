"""E2E de la fuente de audio del Modo reunion en un Chromium REAL contra el backend real (spec audio-sistema-modo-reunion, REQ-001..014).

Lo que se prueba es lo que la persona ve y toca: la ventana «¿Que queres que escuche?» al tocar Record, las tres fuentes («solo lo que hablo», «solo lo que escucho», «lo que escucho y lo que hablo»), que el audio de la PC se pida
DENTRO del clic y se descarte el video, que «ambos» mezcle de verdad las dos fuentes, que Vision no se escuche a si misma, que se corte una fuente a mitad sin cerrar la reunion y que no quede ninguna pista abierta.

`getDisplayMedia` es un FAKE controlado por el test (`window.__display`): entrega un audio sintetico de frecuencia conocida (2000 Hz para «la PC»; el microfono falso de Chromium trae 440 Hz), asi se MIDE que la mezcla contiene las dos
fuentes y la senal no es solo «hay una pista». Lo que estos tests NO pueden probar es que Windows entregue de verdad el audio de Teams/Zoom: eso lo confirma el usuario con una reunion real (ver progress.md).
El audio de Windows es el FAKE de `e2e_support.WINDOWS_AUDIO`; el backend, el de siempre con Whisper falso."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from e2e_support import MIC_KEY, MIC_LABEL_KEY, WINDOWS_AUDIO, FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

OVERLAY = "meeting-source-overlay"
WINDOW_VISIBLE = f"!document.getElementById('{OVERLAY}').classList.contains('hidden')"
LIVE = "window.__streams.filter(s => s.getAudioTracks().some(t => t.readyState === 'live')).length"
DISPLAY_LIVE = "window.__display.streams.filter(s => s.getTracks().some(t => t.readyState === 'live')).length"
SYSTEM_HZ = 2000

# getDisplayMedia falso: pide permiso como el real (se puede cancelar o no traer audio), entrega un tono de SYSTEM_HZ y una pista de video, y registra cada flujo y si el pedido ocurrio dentro de un clic.
FAKE_DISPLAY = f"""
(() => {{
  const md = navigator.mediaDevices;
  window.__display = {{ calls: 0, streams: [], mode: 'ok', inGesture: [], constraints: [] }};
  md.getDisplayMedia = async (constraints) => {{
    const d = window.__display;
    d.calls += 1;
    d.constraints.push(JSON.parse(JSON.stringify(constraints || {{}})));
    d.inGesture.push(!!(navigator.userActivation && navigator.userActivation.isActive));
    if (d.mode === 'denied') throw new DOMException('cancelado', 'NotAllowedError');
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    if (d.mode !== 'no-audio') {{
      const osc = ctx.createOscillator(); osc.frequency.value = {SYSTEM_HZ};
      const gain = ctx.createGain(); gain.gain.value = 0.5;
      osc.connect(gain).connect(dest); osc.start();
    }}
    const canvas = document.createElement('canvas'); canvas.width = 16; canvas.height = 16;
    const video = canvas.captureStream(1).getVideoTracks()[0];
    const stream = new MediaStream([...(d.mode === 'no-audio' ? [] : dest.stream.getAudioTracks()), video]);
    window.__display.streams.push(stream);
    stream.__ctx = ctx;
    return stream;
  }};
}})();
"""

# Mide cuanta energia hay a una frecuencia en lo que REALMENTE graba el MediaRecorder: se engancha a MediaRecorder y analiza cada fragmento.
SPY_RECORDER = """
(() => {
  window.__rec = { starts: 0, mixedStreams: [], fragments: 0 };
  const Orig = window.MediaRecorder;
  window.MediaRecorder = class extends Orig {
    constructor(stream, opts) { super(stream, opts); window.__rec.starts += 1; window.__rec.mixedStreams.push(stream); this.addEventListener('stop', () => { window.__rec.fragments += 1; }); }
  };
  window.__rec.energy = async (stream, hz, ms = 700) => {
    const ctx = new AudioContext(); await ctx.resume();
    const an = ctx.createAnalyser(); an.fftSize = 4096; ctx.createMediaStreamSource(stream).connect(an);
    await new Promise(r => setTimeout(r, ms));
    const buf = new Float32Array(an.frequencyBinCount); an.getFloatFrequencyData(buf);
    for (let i = 0; i < buf.length; i++) if (!(buf[i] > -140)) buf[i] = -140; // piso: -Infinity/NaN (cero digital) no deben leerse como «sobresale infinito»
    const bin = Math.round(hz / (ctx.sampleRate / an.fftSize));
    let peak = -200; for (let i = Math.max(0, bin - 2); i <= bin + 2; i++) peak = Math.max(peak, buf[i]);
    let floor = 0, n = 0; for (let i = 0; i < buf.length; i++) { if (Math.abs(i - bin) > 40) { floor += buf[i]; n++; } }
    await ctx.close();
    return { peak, floor: floor / n, above: peak - floor / n };
  };
})();
"""


def audio_channels(webm: bytes) -> int | None:
    """Canales de la pista de audio de un WebM, leyendo el elemento EBML `Channels` (0x9F) dentro de Segment > Tracks > TrackEntry > Audio. Sin ffmpeg: la suite no lo tiene."""

    def vint(buf: bytes, i: int) -> tuple[int, int]:
        first = buf[i]
        n = 1
        while n <= 8 and not first & (0x80 >> (n - 1)):
            n += 1
        value = first & (0xFF >> n)
        for k in range(1, n):
            value = (value << 8) | buf[i + k]
        return value, n

    def ident(buf: bytes, i: int) -> tuple[int, int]:
        first = buf[i]
        n = 1
        while n <= 4 and not first & (0x80 >> (n - 1)):
            n += 1
        return int.from_bytes(buf[i : i + n], "big"), n

    containers = {0x18538067, 0x1654AE6B, 0xAE, 0xE1}  # Segment, Tracks, TrackEntry, Audio

    def walk(buf: bytes, i: int, end: int) -> int | None:
        while i < end:
            try:
                eid, id_len = ident(buf, i)
                size, size_len = vint(buf, i + id_len)
            except IndexError:
                return None
            body = i + id_len + size_len
            unknown = size == (1 << (7 * size_len)) - 1
            stop = end if unknown else min(end, body + size)
            if eid == 0x9F:
                return int.from_bytes(buf[body : body + size], "big")
            if eid in containers:
                found = walk(buf, body, stop)
                if found is not None:
                    return found
            i = stop
        return None

    return walk(webm, 0, len(webm))


class RecordingWhisper(FakeWhisper):
    """Como `FakeWhisper`, pero guarda el audio entero de cada pedido para poder mirar con que formato llego al servicio."""

    def __init__(self):
        super().__init__()
        self.blobs: list[bytes] = []

    def create(self, **kwargs):
        data = kwargs["file"].read()
        self.blobs.append(data)
        self.audio_sizes.append(len(data))
        if self.exc is not None:
            raise self.exc
        return self.text


@pytest.fixture(scope="module")
def whisper():
    return RecordingWhisper()


@pytest.fixture(scope="module")
def base_url(whisper):
    with serve_backend(SimpleNamespace(audio=SimpleNamespace(transcriptions=whisper))) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    WINDOWS_AUDIO.reset()
    with open_app(browser, base_url) as a:
        a.page.add_init_script(FAKE_DISPLAY)
        a.page.add_init_script(SPY_RECORDER)
        # ESTOS tests prueban la ventana: se la pide siempre (los e2e anteriores arrancan con «no preguntar», ver e2e_support).
        a.page.add_init_script(
            "try { localStorage.setItem('vision.meeting.ask', '1'); localStorage.removeItem('vision.meeting.source'); } catch (e) {}"
        )
        yield a


def energy_of(app, hz: float, *, index: str = "0", need: float = 25, tries: int = 8) -> dict:
    """Energia a `hz` en lo que REALMENTE graba el MediaRecorder, reintentando hasta que sobresalga `need` dB (hasta `tries` ventanas de 700 ms).
    Por que reintenta: la primera ventana puede caer sobre el silencio digital del arranque del contexto de audio (medido: -74 dB la 1.a vez y -34 dB despues) y el test falla
    por un tema de TIEMPOS, no del audio. Si el tono NO llega nunca, igual falla despues de las `tries` ventanas: no se afloja lo que verifica."""
    last: dict = {}
    for _ in range(tries):
        last = app.page.evaluate(f"window.__rec.energy(window.__rec.mixedStreams.at({index}), {hz})")
        if last["above"] > need:
            break
    return last


def window_visible(app) -> bool:
    return app.page.evaluate(WINDOW_VISIBLE)


def click_record(app) -> None:
    app.page.click("#voice-record-btn")
    app.page.wait_for_function(WINDOW_VISIBLE)


def choose(app, source: str) -> None:
    app.page.check(f'input[name="meeting-source"][value="{source}"]')


def start(app, source: str) -> None:
    click_record(app)
    choose(app, source)
    app.page.click("#meeting-source-start")


def end_meeting(app, *, discard: bool = True) -> None:
    app.page.keyboard.press("Escape")
    app.page.wait_for_function("!document.getElementById('voice-eye-exit-confirm').classList.contains('hidden')")
    app.page.click("#voice-eye-exit-discard" if discard else "#voice-eye-exit-yes")
    app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording !== 'true'")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: la ventana al tocar Record
# ---------------------------------------------------------------------------------------------------------------------
class TestSourceWindow:
    def test_record_opens_a_window_with_three_options_and_only_i_speak_is_checked(self, app):
        app.open()

        click_record(app)

        values = app.page.eval_on_selector_all(
            'input[name="meeting-source"]', "els => els.map(e => [e.value, e.checked])"
        )
        assert values == [["mic", True], ["system", False], ["both", False]]
        text = app.page.inner_text(f"#{OVERLAY}")
        assert "Solo lo que hablo" in text and "Solo lo que escucho" in text and "Lo que escucho y lo que hablo" in text
        assert not app.page.is_checked("#meeting-source-noask")

    def test_opening_the_window_opens_no_audio_at_all(self, app):
        app.open()

        click_record(app)
        app.page.wait_for_timeout(500)

        assert len(app.gum()) == 0  # ni microfono
        assert app.page.evaluate("window.__display.calls") == 0  # ni audio de la PC
        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"

    @pytest.mark.parametrize("how", ["#meeting-source-cancel", "#meeting-source-close", "Escape"])
    def test_cancelling_starts_nothing_and_leaves_no_track_open(self, app, how):
        app.open()
        click_record(app)

        if how == "Escape":
            app.page.keyboard.press("Escape")
        else:
            app.page.click(how)
        app.page.wait_for_timeout(300)

        assert not window_visible(app)
        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"
        assert len(app.gum()) == 0 and app.page.evaluate("window.__display.calls") == 0
        assert app.page.eval_on_selector("#voice-eye-widget", "e => e.classList.contains('hidden')")

    def test_a_second_click_on_record_never_stacks_a_second_window(self, app):
        app.open()

        app.page.evaluate("const b = document.getElementById('voice-record-btn'); b.click(); b.click();")
        app.page.wait_for_function(WINDOW_VISIBLE)
        app.page.wait_for_timeout(400)

        assert (
            app.page.eval_on_selector_all(
                f"#{OVERLAY}", "els => els.filter(e => !e.classList.contains('hidden')).length"
            )
            == 1
        )

    def test_the_hint_about_sharing_the_whole_screen_only_shows_for_the_system_options(self, app):
        app.open()
        click_record(app)
        assert app.page.inner_text("#meeting-source-hint").strip() == ""

        choose(app, "system")
        hint = app.page.inner_text("#meeting-source-hint")

        assert "Pantalla completa" in hint and "Compartir también el audio del sistema" in hint
        assert "no graba ni guarda" in hint


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: «solo lo que hablo» es lo de siempre
# ---------------------------------------------------------------------------------------------------------------------
class TestMicOnlyIsUnchanged:
    def test_i_speak_never_asks_the_browser_for_the_pc_audio_and_uses_the_microphone(self, app):
        app.open()

        start(app, "mic")
        app.page.wait_for_function(
            "window.__gum.length > 0 && window.__gum.at(-1).done && window.__gum.at(-1).deviceId"
        )

        assert app.page.evaluate("window.__display.calls") == 0
        assert app.page.get_attribute("#voice-record-btn", "data-recording") == "true"
        assert app.page.inner_text("#voice-eye-source") == "Micrófono"
        end_meeting(app)

    def test_i_speak_records_fragments_and_transcribes_them_like_before(self, app, whisper):
        before = len(whisper.audio_sizes)
        app.open()

        start(app, "mic")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(1500)
        end_meeting(app)

        assert len(whisper.audio_sizes) > before  # el fragmento llego a /api/transcribe


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003: «solo lo que escucho»
# ---------------------------------------------------------------------------------------------------------------------
class TestOnlyWhatIHear:
    def test_it_asks_for_the_pc_audio_inside_the_click_drops_the_video_and_never_opens_the_microphone(self, app):
        app.open()

        start(app, "system")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        assert app.page.evaluate("window.__display.calls") == 1
        assert app.page.evaluate("window.__display.inGesture") == [
            True
        ]  # el navegador solo abre su selector con un gesto reciente
        assert app.page.evaluate("window.__display.constraints[0].video") is True  # requisito de la API
        assert app.page.evaluate(
            "window.__display.streams[0].getVideoTracks().every(t => t.readyState === 'ended')"
        )  # privacidad: la imagen se descarta
        assert len(app.gum()) == 0  # el microfono NO se abre
        assert app.page.inner_text("#voice-eye-source") == "Audio de la PC"
        end_meeting(app)

    def test_what_is_recorded_is_the_pc_audio(self, app):
        app.open()
        start(app, "system")
        app.page.wait_for_function("window.__rec.starts >= 1")

        energy = energy_of(app, SYSTEM_HZ, need=25)

        assert energy["above"] > 25  # el tono de la PC sobresale claramente del piso: de verdad se esta grabando eso
        end_meeting(app)

    def test_it_does_not_check_the_windows_microphone_because_it_does_not_use_it(self, app):
        WINDOWS_AUDIO.mute_all()  # el micro de Windows esta silenciado: no importa, esta fuente no lo usa
        app.open()

        start(app, "system")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        assert not app.page.evaluate("!document.getElementById('mic-warn-overlay').classList.contains('hidden')")
        assert WINDOWS_AUDIO.writes == []
        end_meeting(app)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: «lo que escucho y lo que hablo» -> UN solo flujo con las dos fuentes
# ---------------------------------------------------------------------------------------------------------------------
class TestTheAudioSentToWhisperIsMono:
    """Bug real (2026-10-21): con «lo que escucho y lo que hablo» la transcripcion fallaba una y otra vez (`500 internal_server_error: 'channel_transcript'` del proveedor de IA).
    Medido con el servicio REAL: el audio del microfono (mono) da 200; el audio de la PC y la mezcla de los dos (estereo, 2 canales) dan 500, 6 de 6. El servicio no sabe transcribir 2 canales.
    Por eso TODO lo que Vision manda a transcribir en el Modo reunion tiene que ser MONO, venga de donde venga.
    LIMITE: se prueba el formato del audio que sale hacia el servicio; que el servicio real lo acepte se verifico a mano con ese audio (ver progress.md de la spec)."""

    @pytest.mark.parametrize("source", ["mic", "system", "both"])
    def test_what_reaches_whisper_has_exactly_one_channel(self, app, whisper, source):
        whisper.blobs.clear()
        app.open()

        start(app, source)
        deadline = time.monotonic() + 25
        while not whisper.blobs and time.monotonic() < deadline:
            app.page.wait_for_timeout(250)

        assert whisper.blobs, "llego al menos un fragmento a Whisper"
        channels = {audio_channels(blob) for blob in whisper.blobs}
        assert channels == {1}, f"el servicio de transcripcion falla con audio de 2 canales: {channels}"
        end_meeting(app)

    def test_the_parser_really_tells_one_channel_from_two(self):
        """Control del propio test: un WebM sintetico con `Channels` = 2 se lee como 2 (si el parser devolviera siempre 1, los tests de arriba pasarian sin verificar nada)."""

        def element(ident: list[int], body: bytes) -> bytes:
            return bytes(ident) + bytes([0x80 | len(body)]) + body

        def webm(channels: int) -> bytes:
            audio = element([0x9F], bytes([channels]))  # Channels
            track = element([0xAE], element([0xE1], audio))  # TrackEntry > Audio > Channels
            tracks = element([0x16, 0x54, 0xAE, 0x6B], track)  # Tracks
            return element([0x18, 0x53, 0x80, 0x67], tracks)  # Segment

        assert audio_channels(webm(2)) == 2
        assert audio_channels(webm(1)) == 1
        assert audio_channels(b"no es un webm") is None


class TestThePersonKeepsHearingTheMeeting:
    """Bug real (2026-10-20): al grabar «lo que escucho» (o «lo que escucho y lo que hablo») la persona DEJABA de oir la reunion y volvia a oirla al detener.
    Causa: Vision pedia el audio de la PC con `suppressLocalAudioPlayback: true`, que le dice al navegador que silencie ese audio en los parlantes mientras lo captura.
    En una reunion hay que poder oir. El eco de la propia voz de Vision ya lo cubre REQ-006 (un fragmento con Vision hablando no se transcribe), no hace falta silenciar nada.
    LIMITE: el getDisplayMedia de estos tests es un FAKE; prueba lo que Vision LE PIDE al navegador, no que Windows/Chrome reales dejen de silenciar (eso solo se ve con una reunion real)."""

    @pytest.mark.parametrize("source", ["system", "both"])
    def test_it_never_asks_the_browser_to_mute_the_pc_audio_on_the_speakers(self, app, source):
        app.open()

        start(app, source)
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        audio = app.page.evaluate("window.__display.constraints[0].audio")
        suppress = audio.get("suppressLocalAudioPlayback") if isinstance(audio, dict) else None
        assert suppress is not True, "pedirlo en true silencia la reunion para quien la esta oyendo"

    @pytest.mark.parametrize("source", ["system", "both"])
    def test_it_asks_explicitly_to_keep_the_local_playback(self, app, source):
        """No alcanza con omitirlo: algunos navegadores lo tienen en true por defecto. Se pide expresamente `false`."""
        app.open()

        start(app, source)
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        audio = app.page.evaluate("window.__display.constraints[0].audio")
        assert isinstance(audio, dict) and audio.get("suppressLocalAudioPlayback") is False

    @pytest.mark.parametrize("source", ["system", "both"])
    def test_the_mixer_never_connects_the_pc_audio_to_the_speakers(self, app, source):
        """El mezclador solo graba: ni el audio de la PC ni el microfono se conectan al `destination` del contexto (los parlantes)."""
        app.page.add_init_script(
            """
            (() => {
              window.__toSpeakers = 0;
              const Orig = AudioNode.prototype.connect;
              AudioNode.prototype.connect = function (target, ...rest) {
                if (target instanceof AudioDestinationNode) window.__toSpeakers++;
                return Orig.call(this, target, ...rest);
              };
            })();
            """
        )
        app.open()

        start(app, source)
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")
        app.page.wait_for_timeout(800)

        assert app.page.evaluate("window.__toSpeakers") == 0, "algo del Modo reunion se conecto a los parlantes"


class TestBothMixed:
    def test_both_sources_are_mixed_into_one_recorded_stream_with_energy_from_each(self, app):
        app.open()

        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")
        mic = energy_of(app, 440, need=20)
        pc = energy_of(app, SYSTEM_HZ, need=20)

        assert app.page.evaluate("window.__rec.mixedStreams.length") >= 1
        assert mic["above"] > 20, f"falta el microfono en la mezcla: {mic}"
        assert pc["above"] > 20, f"falta el audio de la PC en la mezcla: {pc}"
        assert app.page.inner_text("#voice-eye-source") == "Audio de la PC + micrófono"
        end_meeting(app)

    def test_the_mixed_stream_is_the_only_thing_recorded_not_one_recorder_per_source(self, app):
        app.open()
        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")

        distinct = app.page.evaluate("new Set(window.__rec.mixedStreams.map(s => s.id)).size")

        assert distinct == 1  # un unico flujo grabado: una sola transcripcion mezclada (sin diarizacion)
        end_meeting(app)

    def test_both_asks_the_pc_audio_once_and_opens_the_microphone_once(self, app):
        app.open()

        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")

        assert app.page.evaluate("window.__display.calls") == 1 and len(app.gum()) == 1
        end_meeting(app)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005: no se pudo capturar el audio de la PC
# ---------------------------------------------------------------------------------------------------------------------
class TestWhenThePcAudioCannotBeCaptured:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("denied", "No se compartió nada"), ("no-audio", "No se compartió el audio")],
    )
    def test_the_window_stays_open_explains_in_plain_words_and_starts_nothing(self, app, mode, expected):
        app.open()
        app.page.evaluate(f"window.__display.mode = '{mode}'")

        start(app, "system")
        app.page.wait_for_function("document.getElementById('meeting-source-error').textContent.length > 0")

        assert window_visible(app)  # queda abierta para reintentar o cancelar
        message = app.page.inner_text("#meeting-source-error")
        assert expected in message and "NotAllowed" not in message and "DOMException" not in message
        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"
        assert len(app.gum()) == 0
        assert app.page.evaluate(DISPLAY_LIVE) == 0  # no quedo ninguna pista abierta

    def test_after_a_failed_try_it_can_be_retried_and_then_starts(self, app):
        app.open()
        app.page.evaluate("window.__display.mode = 'denied'")
        start(app, "system")
        app.page.wait_for_function("document.getElementById('meeting-source-error').textContent.length > 0")

        app.page.evaluate("window.__display.mode = 'ok'")
        app.page.click("#meeting-source-start")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        assert not window_visible(app)
        end_meeting(app)

    def test_a_browser_without_the_capability_blocks_the_system_options_with_the_reason(self, app):
        app.page.add_init_script(
            "delete navigator.mediaDevices.getDisplayMedia; Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', { value: undefined, configurable: true });"
        )
        app.open()

        click_record(app)

        disabled = app.page.eval_on_selector_all(
            'input[name="meeting-source"]', "els => els.map(e => [e.value, e.disabled])"
        )
        assert disabled == [["mic", False], ["system", True], ["both", True]]
        assert "Chrome o Edge" in app.page.inner_text("#meeting-source-error")
        assert app.page.eval_on_selector('input[name="meeting-source"][value="mic"]', "e => e.checked")

    def test_closing_the_window_while_the_browser_selector_is_open_leaves_nothing_open(self, app):
        app.open()
        # Forma de FUNCION: si el texto terminara en una funcion, Playwright la ejecutaria el mismo (llamaria a getDisplayMedia de mas y dejaria un flujo vivo).
        app.page.evaluate(
            """() => {
              const orig = navigator.mediaDevices.getDisplayMedia;
              navigator.mediaDevices.getDisplayMedia = async (c) => { await new Promise(r => setTimeout(r, 800)); return orig(c); };
            }"""
        )
        start(app, "system")

        app.page.keyboard.press("Escape")  # cierra la ventana mientras el navegador todavia 'estaba eligiendo'
        app.page.wait_for_function("window.__display.streams.length >= 1", timeout=10_000)  # el audio LLEGO tarde (si no, '0 vivas' pasaria sin probar nada)
        app.page.wait_for_function(f"{DISPLAY_LIVE} === 0", timeout=10_000)  # y se cerro solo: sin esto quedaria el indicador de «compartiendo pantalla» del navegador

        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006: Vision no se escucha a si misma
# ---------------------------------------------------------------------------------------------------------------------
class TestVisionDoesNotHearItself:
    def _fake_speaking(self, app, value: bool) -> None:
        app.page.evaluate(
            f"Object.defineProperty(window.speechSynthesis, 'speaking', {{ get: () => {str(value).lower()}, configurable: true }});"
        )

    def test_with_the_pc_audio_a_fragment_that_overlaps_vision_speaking_is_not_transcribed(self, app, whisper):
        app.open()
        self._fake_speaking(app, True)  # Vision esta hablando durante todo el fragmento
        before = len(whisper.audio_sizes)

        start(app, "system")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(800)

        assert len(whisper.audio_sizes) == before  # nada se mando a transcribir
        end_meeting(app)

    def test_with_the_pc_audio_a_fragment_where_vision_starts_talking_midway_is_not_transcribed(self, app, whisper):
        """Vision callada al arrancar el fragmento y hablando a mitad: el chequeo de arranque no la ve, la ve el temporizador de 250 ms."""
        app.open()
        self._fake_speaking(app, False)
        before = len(whisper.audio_sizes)

        start(app, "system")
        app.page.wait_for_function("window.__rec.starts >= 1")
        app.page.wait_for_timeout(1000)  # el fragmento ya esta grabando
        self._fake_speaking(app, True)
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(2500)  # margen para que una transcripcion tardia (si el guardia fallara) se vea

        assert len(whisper.audio_sizes) == before
        end_meeting(app)

    def test_with_the_pc_audio_and_vision_silent_the_fragments_are_transcribed(self, app, whisper):
        app.open()
        self._fake_speaking(app, False)
        before = len(whisper.audio_sizes)

        start(app, "system")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(1500)
        end_meeting(app)

        assert len(whisper.audio_sizes) > before

    def test_with_the_microphone_only_nothing_is_ever_discarded(self, app, whisper):
        """«Solo lo que hablo» tiene cancelacion de eco del navegador: comportamiento de siempre, sin descartar fragmentos."""
        app.open()
        self._fake_speaking(app, True)
        before = len(whisper.audio_sizes)

        start(app, "mic")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(1500)
        end_meeting(app)

        assert len(whisper.audio_sizes) > before


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007 / REQ-008: el resumen al «Guardar y salir» y la salida limpia
# ---------------------------------------------------------------------------------------------------------------------
class TestExit:
    @pytest.mark.parametrize("source", ["mic", "system", "both"])
    def test_leaving_stops_every_track_and_closes_the_audio_context(self, app, source):
        app.open()
        start(app, source)
        app.page.wait_for_function("window.__rec.starts >= 1")

        end_meeting(app)
        app.page.wait_for_timeout(500)

        assert app.page.evaluate(LIVE) == 0  # microfono
        assert app.page.evaluate(DISPLAY_LIVE) == 0  # audio de la PC
        assert app.page.eval_on_selector("#voice-eye-widget", "e => e.classList.contains('hidden')")
        assert app.page.inner_text("#voice-eye-source") == ""  # el indicador se limpia

    def test_save_and_exit_asks_for_the_summary_with_what_was_heard(self, app, whisper, base_url):
        whisper.text = "Juan dijo que el contenedor llega el martes"
        calls: list[dict] = []
        app.page.route(
            "**/api/meeting/summarize",
            lambda route: (
                calls.append(route.request.post_data_json),
                route.fulfill(json={"summary": "Resumen de la reunion"}),
            ),
        )
        app.open()

        start(app, "both")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)
        app.page.wait_for_timeout(2000)
        end_meeting(app, discard=False)
        app.page.wait_for_function("document.body.innerText.includes('Resumen de la reunion')", timeout=15_000)

        assert len(calls) == 1 and "el contenedor llega el martes" in calls[0]["transcript"]

    def test_discarding_never_asks_for_a_summary_and_keeps_no_audio(self, app):
        asked: list[int] = []
        app.page.route(
            "**/api/meeting/summarize", lambda route: (asked.append(1), route.fulfill(json={"summary": "x"}))
        )
        app.open()
        start(app, "system")
        app.page.wait_for_function("window.__rec.fragments >= 1", timeout=20_000)

        end_meeting(app, discard=True)
        app.page.wait_for_timeout(500)

        assert asked == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009: se corta una fuente a mitad
# ---------------------------------------------------------------------------------------------------------------------
class TestAFeedCutsOut:
    def _stop_pc_audio(self, app) -> None:
        app.page.evaluate(
            "window.__display.streams[0].getAudioTracks().forEach(t => { t.stop(); t.dispatchEvent(new Event('ended')); })"
        )

    def test_stopping_the_share_keeps_the_meeting_open_and_warns_once_with_both(self, app):
        app.open()
        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")

        self._stop_pc_audio(app)
        app.page.wait_for_function("document.body.innerText.includes('se dejó de compartir el audio de la PC')")

        notes = app.page.evaluate(
            "[...document.querySelectorAll('.message.system-note, .system-note')].filter(n => n.innerText.includes('se dejó de compartir')).length"
        )
        assert notes == 1
        assert app.page.get_attribute("#voice-record-btn", "data-recording") == "true"  # la reunion sigue abierta
        assert "solo tu micrófono" in app.page.inner_text("body")
        end_meeting(app)

    def test_stopping_the_share_with_only_the_pc_audio_keeps_the_meeting_open_and_says_how_to_finish(self, app):
        app.open()
        start(app, "system")
        app.page.wait_for_function("window.__rec.starts >= 1")

        self._stop_pc_audio(app)
        app.page.wait_for_function("document.body.innerText.includes('no hay otra fuente')")

        assert app.page.get_attribute("#voice-record-btn", "data-recording") == "true"
        assert "Guardar y salir" in app.page.inner_text("body")
        end_meeting(app)

    def test_unplugging_the_microphone_in_both_keeps_the_pc_audio_and_the_same_recorded_stream(self, app):
        """El plug and play del microfono sigue andando dentro de la mezcla: se cambia SOLO el microfono, el flujo grabado no se reemplaza."""
        app.open()
        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")
        stream_before = app.page.evaluate("window.__rec.mixedStreams[0].id")

        app.unplug_current_mic()
        app.page.wait_for_function("document.body.innerText.includes('y el audio de la PC')", timeout=15_000)

        assert app.page.evaluate("window.__rec.mixedStreams.at(-1).id") == stream_before  # mismo flujo mezclado
        assert app.page.evaluate(DISPLAY_LIVE) == 1  # el audio de la PC no se toco
        pc = energy_of(app, SYSTEM_HZ, index="-1", need=20)
        assert pc["above"] > 20  # y sigue llegando
        end_meeting(app)

    def test_a_device_change_with_a_saved_microphone_never_opens_one_when_only_the_pc_audio_is_used(self, app):
        """Con «solo lo que escucho» no hay microfono: el plug and play (que reabre el guardado si no coincide con el flujo activo) no tiene nada que reabrir."""
        app.open()
        mic = app.page.evaluate(
            "navigator.mediaDevices.enumerateDevices().then(d => d.filter(x => x.kind === 'audioinput' && x.deviceId !== 'default' && x.deviceId !== 'communications').map(x => [x.deviceId, x.label])[0])"
        )
        assert mic, "el Chromium de los tests tiene microfonos falsos"
        app.page.evaluate("([k, l, id, label]) => { localStorage.setItem(k, id); localStorage.setItem(l, label); }", [MIC_KEY, MIC_LABEL_KEY, mic[0], mic[1]])
        start(app, "system")
        app.page.wait_for_function("window.__rec.starts >= 1")

        app.device_change()
        app.page.wait_for_timeout(1500)

        assert app.page.get_attribute("#voice-record-btn", "data-recording") == "true"
        assert len(app.gum()) == 0  # no se intento abrir ningun microfono
        end_meeting(app)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011: Configuracion > Audio y «No volver a preguntar»
# ---------------------------------------------------------------------------------------------------------------------
class TestSettingsAndPreference:
    def test_settings_shows_the_default_source_and_the_ask_checkbox(self, app):
        app.open()

        app.open_audio_settings()

        assert app.page.input_value("#cfg-meeting-source") == "mic"
        assert app.page.is_checked("#cfg-meeting-ask")

    def test_what_was_saved_with_dont_ask_is_what_settings_shows_afterwards(self, app):
        """«No volver a preguntar» en la ventana guarda la fuente y apaga la pregunta: Configuracion tiene que mostrar exactamente eso (si no, quedaria desfasada)."""
        app.open()
        click_record(app)
        choose(app, "both")
        app.page.check("#meeting-source-noask")
        app.page.click("#meeting-source-start")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")
        end_meeting(app)

        app.open_audio_settings()

        assert app.page.input_value("#cfg-meeting-source") == "both"
        assert not app.page.is_checked("#cfg-meeting-ask")

    def test_the_source_chosen_in_settings_is_the_one_preselected_in_the_window(self, app):
        app.open()
        app.open_audio_settings()
        app.page.select_option("#cfg-meeting-source", "both")
        app.page.click("#settings-close")

        click_record(app)

        assert app.page.eval_on_selector('input[name="meeting-source"][value="both"]', "e => e.checked")

    def test_dont_ask_again_makes_record_start_directly_with_the_saved_source(self, app):
        app.open()
        click_record(app)
        choose(app, "mic")
        app.page.check("#meeting-source-noask")
        app.page.click("#meeting-source-start")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")
        end_meeting(app)

        app.page.click("#voice-record-btn")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        assert not window_visible(app)  # arranco directo, sin ventana
        end_meeting(app)

    def test_dont_ask_with_a_pc_source_still_opens_the_browser_selector_inside_the_click(self, app):
        app.open()
        click_record(app)
        choose(app, "system")
        app.page.check("#meeting-source-noask")
        app.page.click("#meeting-source-start")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")
        end_meeting(app)
        app.page.evaluate("window.__display.inGesture.length = 0")

        app.page.click("#voice-record-btn")
        app.page.wait_for_function("document.getElementById('voice-record-btn').dataset.recording === 'true'")

        assert app.page.evaluate("window.__display.inGesture") == [
            True
        ]  # el navegador igual pregunta, y dentro del clic
        end_meeting(app)

    def test_unchecking_ask_in_settings_and_checking_it_again_brings_the_window_back(self, app):
        app.open()
        app.open_audio_settings()
        app.page.uncheck("#cfg-meeting-ask")
        app.page.check("#cfg-meeting-ask")
        app.page.click("#settings-close")

        click_record(app)

        assert window_visible(app)

    def test_a_choice_without_dont_ask_applies_only_to_that_meeting(self, app):
        app.open()
        start(app, "both")
        app.page.wait_for_function("window.__rec.starts >= 1")
        end_meeting(app)

        click_record(app)

        assert app.page.eval_on_selector(
            'input[name="meeting-source"][value="mic"]', "e => e.checked"
        )  # el default sigue siendo «solo lo que hablo»


# ---------------------------------------------------------------------------------------------------------------------
# REQ-012 / REQ-013 / REQ-014: los otros modos no cambian y las exclusiones se mantienen
# ---------------------------------------------------------------------------------------------------------------------
class TestOtherModesAndExclusions:
    def test_brain_mode_and_the_chat_microphone_never_ask_for_a_source(self, app):
        app.open()

        app.start_listening("#brain-btn")
        assert not window_visible(app) and app.page.evaluate("window.__display.calls") == 0
        app.page.keyboard.press("Escape")
        app.page.wait_for_function(f"{LIVE} === 0")

        app.start_listening("#voice-mic-btn")
        assert not window_visible(app) and app.page.evaluate("window.__display.calls") == 0
        app.page.click("#voice-mic-btn")

    def test_with_the_window_open_brain_mode_cannot_start_on_top(self, app):
        app.open()
        click_record(app)

        app.page.evaluate("document.getElementById('brain-btn').click()")
        app.page.wait_for_timeout(500)

        assert app.page.get_attribute("#brain-btn", "data-state") == "idle"
        assert len(app.gum()) == 0
        assert "preparando" in app.page.inner_text("body")

    def test_with_the_microphone_silenced_in_windows_the_0_30_warning_still_comes_after_choosing_i_speak(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        start(app, "mic")
        app.page.wait_for_function("!document.getElementById('mic-warn-overlay').classList.contains('hidden')")

        assert "silenciado en Windows" in app.page.inner_text("#mic-warn-title")
        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"
        app.page.click("#mic-warn-close")

    def test_cancelling_the_microphone_warning_after_choosing_both_closes_the_pc_audio_too(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        start(app, "both")
        app.page.wait_for_function("!document.getElementById('mic-warn-overlay').classList.contains('hidden')")

        app.page.click("#mic-warn-close")
        app.page.wait_for_timeout(500)

        assert app.page.evaluate(DISPLAY_LIVE) == 0  # no queda el audio de la PC abierto
        assert app.page.get_attribute("#voice-record-btn", "data-recording") != "true"
