"""E2E del control del microfono de Windows en un Chromium REAL contra el backend real (spec control-microfono-windows, REQ-002..006, 008, 009, 010).

El audio de Windows es el FAKE de `e2e_support.WINDOWS_AUDIO` (nunca el real de quien corre la suite): arranca con microfonos ACTIVOS y cada test silencia el que necesita. Los microfonos de Chromium
son los falsos de siempre; sus nombres coinciden con los del Windows falso, que es como Vision cruza uno con otro (REQ-006). Lo que se prueba es lo que la persona ve y toca:
el modal de aviso antes de escuchar, el boton Activar, el deslizador (0 silencia), la barra verde y que el plug and play de siempre siga igual."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from e2e_support import MIC_KEY, MIC_LABEL_KEY, WINDOWS_AUDIO, FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

OVERLAY = "#mic-warn-overlay"
VISIBLE = "!document.getElementById('mic-warn-overlay').classList.contains('hidden')"


@pytest.fixture(scope="module")
def whisper():
    return FakeWhisper()


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
    WINDOWS_AUDIO.reset()  # cada test arranca con todos los microfonos ACTIVOS
    with open_app(browser, base_url) as a:
        yield a


def modal_visible(app) -> bool:
    return app.page.evaluate(VISIBLE)


def wait_modal(app) -> None:
    app.page.wait_for_function(VISIBLE)


def modal_options(app) -> list[str]:
    return app.page.eval_on_selector_all("#mic-warn-select option", "els => els.map(e => e.textContent)")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005: aviso ANTES de escuchar, en los tres modos
# ---------------------------------------------------------------------------------------------------------------------
class TestWarningBeforeListening:
    @pytest.mark.parametrize(
        ("button", "label"),
        [
            ("#voice-mic-btn", "El micrófono del chat"),
            ("#brain-btn", "El Modo Cerebro"),
            ("#voice-record-btn", "El Modo reunión"),
        ],
    )
    def test_a_muted_microphone_shows_the_warning_with_its_name_before_any_audio_is_opened(self, app, button, label):
        WINDOWS_AUDIO.mute_all()
        app.open()
        before = len(app.gum())

        app.page.click(button)
        wait_modal(app)

        assert "silenciado en Windows" in app.page.inner_text("#mic-warn-title")
        message = app.page.inner_text("#mic-warn-message")
        assert label in message and "Fake Default Audio Input" in message  # dice CUAL es y que modo no va a escuchar
        # El unico microfono que se abre es el de la barra verde del propio aviso: el modo todavia NO arranco.
        assert app.page.evaluate("document.getElementById('voice-mic-btn').dataset.listening") != "true"
        assert app.page.get_attribute("#brain-btn", "data-state") == "idle"
        assert len(app.gum()) <= before + 1

    def test_the_warning_lists_every_connected_microphone(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        app.page.click("#voice-mic-btn")
        wait_modal(app)

        names = modal_options(app)
        assert len(names) == 3
        assert any("Fake Audio Input 1" in n for n in names) and any("Fake Audio Input 2" in n for n in names)
        assert any("predeterminado de Windows" in n for n in names)

    def test_an_active_microphone_shows_no_warning_and_listens_as_always(self, app):
        app.open()

        app.start_listening("#voice-mic-btn")

        assert not modal_visible(app)
        assert app.page.get_attribute("#voice-mic-btn", "data-listening") == "true"
        app.page.click("#voice-mic-btn")

    def test_closing_the_warning_cancels_the_mode_and_changes_no_audio(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#brain-btn")
        wait_modal(app)

        app.page.click("#mic-warn-close")

        assert not modal_visible(app)
        assert app.page.get_attribute("#brain-btn", "data-state") == "idle"
        assert WINDOWS_AUDIO.writes == []  # REQ-008: cerrar no modifica nada

    def test_continuing_without_activating_starts_the_mode_and_still_changes_no_audio(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#voice-mic-btn")
        wait_modal(app)

        app.page.click("#mic-warn-continue")
        app.page.wait_for_function("document.getElementById('voice-mic-btn').dataset.listening === 'true'")

        assert WINDOWS_AUDIO.writes == []  # la persona eligio seguir: Vision no activa nada por su cuenta
        app.page.click("#voice-mic-btn")

    def test_a_double_click_never_opens_two_warnings_or_two_modes(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        app.page.evaluate("const b = document.getElementById('brain-btn'); b.click(); b.click();")
        wait_modal(app)
        app.page.wait_for_timeout(500)

        assert (
            app.page.eval_on_selector_all(OVERLAY, "els => els.filter(e => !e.classList.contains('hidden')).length")
            == 1
        )
        assert len(modal_options(app)) == 3  # una sola lista, no duplicada
        # Un 2do chequeo habria creado un 2do medidor de nivel y el primero quedaria con el microfono abierto para siempre (indicador rojo del navegador).
        app.page.click("#mic-warn-close")
        live = "window.__streams.filter(s => s.getAudioTracks().some(t => t.readyState === 'live')).length"
        app.page.wait_for_function(f"{live} === 0")

    def test_clicking_again_with_the_warning_open_does_not_stack_a_second_check(self, app):
        """Con el aviso abierto el 2do clic del mismo boton se ignora: sin esa guarda cada clic leia Windows y pisaba la lista y el medidor del aviso."""
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#brain-btn")
        wait_modal(app)
        reads = {"n": 0}
        original = WINDOWS_AUDIO.list_microphones

        def counting():
            reads["n"] += 1
            return original()

        WINDOWS_AUDIO.list_microphones = counting
        try:
            app.page.evaluate("document.getElementById('brain-btn').click()")  # el aviso es un modal, pero el boton sigue siendo alcanzable por script
            app.page.wait_for_timeout(700)
        finally:
            WINDOWS_AUDIO.list_microphones = original

        assert reads["n"] == 0  # no se volvio a leer Windows
        assert app.page.eval_on_selector_all(OVERLAY, "els => els.filter(e => !e.classList.contains('hidden')).length") == 1


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002 / 003 / 004 en el aviso: activar, volumen y 0 = silenciar
# ---------------------------------------------------------------------------------------------------------------------
class TestActivateAndVolumeInTheWarning:
    def test_activate_unmutes_in_windows_keeps_the_volume_and_the_modal_shows_it(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#voice-mic-btn")
        wait_modal(app)

        app.page.click("#mic-warn-activate")
        app.page.wait_for_function("document.getElementById('mic-warn-status').dataset.state === 'on'")

        assert ("mute", "win-0", False) in WINDOWS_AUDIO.writes
        assert (
            WINDOWS_AUDIO.mics["win-0"].muted is False and WINDOWS_AUDIO.mics["win-0"].volume == 70
        )  # el volumen no se toco
        assert not any(w[0] == "volume" for w in WINDOWS_AUDIO.writes)
        assert app.page.inner_text("#mic-warn-activate") == "Silenciar micrófono"

    def test_the_slider_sets_the_windows_volume_and_unmutes(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#voice-mic-btn")
        wait_modal(app)

        app.page.fill("#mic-warn-slider", "35")
        app.page.wait_for_function("document.getElementById('mic-warn-status').dataset.state === 'on'")

        assert WINDOWS_AUDIO.mics["win-0"].volume == 35 and WINDOWS_AUDIO.mics["win-0"].muted is False
        assert app.page.inner_text("#mic-warn-value") == "35 %"

    def test_the_slider_at_the_minimum_mutes_instead_of_leaving_it_active_but_deaf(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#voice-mic-btn")
        wait_modal(app)
        app.page.click("#mic-warn-activate")
        app.page.wait_for_function("document.getElementById('mic-warn-status').dataset.state === 'on'")

        app.page.fill("#mic-warn-slider", "0")
        app.page.wait_for_function("document.getElementById('mic-warn-status').dataset.state === 'muted'")

        assert WINDOWS_AUDIO.mics["win-0"].muted is True
        assert WINDOWS_AUDIO.mics["win-0"].volume == 70  # conserva el nivel: activar despues no lo deja en 0

    def test_a_windows_that_refuses_the_change_is_explained_in_plain_words(self, app, monkeypatch):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.page.click("#voice-mic-btn")
        wait_modal(app)
        monkeypatch.setattr(WINDOWS_AUDIO, "set_mute", lambda *a: None)  # el driver dice OK y no aplica nada

        app.page.click("#mic-warn-activate")
        app.page.wait_for_function("document.getElementById('mic-warn-error').textContent.length > 0")

        text = app.page.inner_text("#mic-warn-error")
        assert "Panel de control" in text and "HRESULT" not in text and "0x" not in text
        assert app.page.get_attribute("#mic-warn-status", "data-state") == "muted"  # sigue diciendo la verdad


# ---------------------------------------------------------------------------------------------------------------------
# Configuracion > Audio
# ---------------------------------------------------------------------------------------------------------------------
class TestAudioSettings:
    def test_the_panel_shows_the_windows_microphones_and_their_state(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'ready'")

        assert app.page.eval_on_selector_all("#cfg-winmic-select option", "e => e.length") == 3
        assert app.page.get_attribute("#cfg-winmic-state", "data-state") == "muted"
        assert app.page.inner_text("#cfg-winmic-toggle") == "Activar micrófono"
        assert app.page.input_value("#cfg-winmic-slider") == "70"

    def test_activate_and_the_slider_work_from_settings_too(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()
        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'ready'")

        app.page.click("#cfg-winmic-toggle")
        app.page.wait_for_function("document.getElementById('cfg-winmic-state').dataset.state === 'on'")
        assert not any(w[0] == "volume" for w in WINDOWS_AUDIO.writes)  # activar solo quita el silencio: el volumen no se toca
        assert WINDOWS_AUDIO.mics["win-0"].volume == 70
        app.page.fill("#cfg-winmic-slider", "0")
        app.page.wait_for_function("document.getElementById('cfg-winmic-state').dataset.state === 'muted'")

        assert WINDOWS_AUDIO.mics["win-0"].muted is True

    def test_just_opening_settings_never_writes_anything(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'ready'")
        app.page.wait_for_timeout(500)

        assert WINDOWS_AUDIO.writes == []  # REQ-008

    def test_with_no_microphone_connected_it_says_so_in_plain_words(self, app):
        WINDOWS_AUDIO.mics.clear()
        app.open()

        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'empty'")

        assert "ningún micrófono" in app.page.inner_text("#cfg-winmic-note")

    def test_the_green_meter_and_the_new_block_live_together_without_leaking_streams(self, app):
        app.open()
        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'ready'")
        app.page.wait_for_function("window.__gum.some(c => c.done && c.deviceId)")
        app.page.click("#settings-close")

        live = "window.__streams.filter(s => s.getAudioTracks().some(t => t.readyState === 'live')).length"
        app.page.wait_for_function(f"{live} === 0")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006: no actuar sobre un homonimo
# ---------------------------------------------------------------------------------------------------------------------
class TestSameNameMicrophones:
    def test_with_two_microphones_of_the_same_name_the_panel_asks_instead_of_guessing(self, app):
        from app.audio_control import Microphone

        WINDOWS_AUDIO.mics["win-dup"] = Microphone(
            "win-dup", "Fake Audio Input 1", False, True, 20
        )  # mismo nombre que otro
        app.open()
        app.page.evaluate(
            "([k, l]) => { localStorage.setItem(k, 'id-guardado'); localStorage.setItem(l, 'Fake Audio Input 1'); }",
            [MIC_KEY, MIC_LABEL_KEY],
        )

        app.open_audio_settings()
        app.page.wait_for_function("document.getElementById('cfg-winmic-block').dataset.state === 'ready'")

        assert "mismo nombre" in app.page.inner_text("#cfg-winmic-note")
        assert WINDOWS_AUDIO.writes == []

    def test_a_muted_homonym_never_triggers_the_warning_for_the_wrong_microphone(self, app):
        from app.audio_control import Microphone

        WINDOWS_AUDIO.mics["win-dup"] = Microphone("win-dup", "Fake Audio Input 1", False, True, 20)
        app.open()
        app.page.evaluate(
            "([k, l]) => { localStorage.setItem(k, 'x'); localStorage.setItem(l, 'Fake Audio Input 1'); }",
            [MIC_KEY, MIC_LABEL_KEY],
        )

        app.page.click("#voice-mic-btn")
        app.page.wait_for_timeout(1500)

        assert not modal_visible(app)  # ambiguo: no se le dice a la persona que active «uno» que quiza no es el suyo
        assert WINDOWS_AUDIO.writes == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009: sin senal a los 5 s cuando no se puede leer el estado
# ---------------------------------------------------------------------------------------------------------------------
class TestNoSignal:
    @pytest.fixture
    def silent_browser_mic(self, app):
        """El navegador entrega un microfono que solo trae silencio (como uno silenciado por el sistema)."""
        app.page.add_init_script(
            """
            (() => {
              const md = navigator.mediaDevices;
              const prev = md.getUserMedia.bind(md);
              md.getUserMedia = async (c) => {
                const ctx = new AudioContext();
                const dest = ctx.createMediaStreamDestination(); // sin fuente: ceros exactos
                window.__silentCtx = ctx;
                return dest.stream;
              };
            })();
            """
        )

    def test_when_windows_cannot_be_read_and_nothing_arrives_it_warns_after_five_seconds_once(
        self, app, silent_browser_mic, monkeypatch
    ):
        def broken():
            raise OSError("sin acceso al audio")

        monkeypatch.setattr(WINDOWS_AUDIO, "list_microphones", broken)  # no se puede confirmar el silencio
        app.open()
        # el modal solo puede abrirse si hay microfonos que listar: se restablece para la lista pero NO para el chequeo previo
        app.page.click("#voice-mic-btn")
        app.page.wait_for_timeout(1200)
        assert not modal_visible(app)  # todavia no pasaron los 5 s

        monkeypatch.undo()
        app.page.wait_for_function(VISIBLE, timeout=12_000)

        assert "No llega señal de audio" in app.page.inner_text("#mic-warn-title")

    def test_with_a_real_signal_the_no_signal_warning_never_appears(self, app, monkeypatch):
        """Windows es ilegible SOLO en el chequeo previo (la 1ra lectura) y legible despues: si el aviso de «sin senal» saltara pese a haber senal, tendria microfonos
        que listar y se veria. Con Windows ilegible todo el tiempo este test no podria fallar nunca (no habria nada que mostrar)."""
        original = WINDOWS_AUDIO.list_microphones
        reads = {"n": 0}

        def unreadable_only_the_first_time():
            reads["n"] += 1
            if reads["n"] == 1:
                raise OSError("sin acceso al audio")
            return original()

        monkeypatch.setattr(WINDOWS_AUDIO, "list_microphones", unreadable_only_the_first_time)
        app.open()

        app.page.click("#voice-mic-btn")  # el microfono falso de Chromium trae un tono de 440 Hz: hay senal
        app.page.wait_for_function("document.getElementById('voice-mic-btn').dataset.listening === 'true'")
        app.page.wait_for_timeout(6500)
        monkeypatch.undo()  # ahora Windows vuelve a responder: si el aviso de «sin senal» se disparara, tendria microfonos que listar

        assert not modal_visible(app)
        app.page.wait_for_timeout(1500)  # y tampoco aparece tarde
        assert not modal_visible(app)
        app.page.click("#voice-mic-btn")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-010: el plug and play y el resto del audio siguen igual
# ---------------------------------------------------------------------------------------------------------------------
class TestPlugAndPlayUntouched:
    def test_an_automatic_reconnection_of_brain_mode_never_shows_the_warning(self, app):
        app.open()
        app.start_listening("#brain-btn")
        WINDOWS_AUDIO.mute_all()  # Windows lo silencia DESPUES de arrancar
        # El microfono que se reabre solo trae SILENCIO (como uno silenciado por el sistema): si la reconexion fuera un arranque manual, el aviso de «sin senal»
        # saltaria a los 5 s. Con el tono del microfono falso habria senal y este test no podria fallar nunca.
        app.page.evaluate(
            """() => {
              navigator.mediaDevices.getUserMedia = async () => {
                const ctx = new AudioContext();
                return ctx.createMediaStreamDestination().stream; // sin fuente: ceros exactos
              };
            }"""
        )

        app.unplug_current_mic()  # el plug and play lo reabre solo
        app.page.wait_for_timeout(6500)  # mas que los 5 s del aviso de «sin senal»

        assert app.page.get_attribute("#brain-btn", "data-state") != "idle"  # Cerebro sigue encendido tras la reconexion
        assert not modal_visible(app)  # la reconexion automatica no es un arranque manual
        assert WINDOWS_AUDIO.writes == []
        app.page.keyboard.press("Escape")

    def test_a_device_change_does_not_open_anything_on_its_own(self, app):
        WINDOWS_AUDIO.mute_all()
        app.open()

        app.device_change()
        app.page.wait_for_timeout(800)

        assert not modal_visible(app) and WINDOWS_AUDIO.writes == []

    def test_a_failing_audio_service_never_blocks_the_modes(self, app, monkeypatch):
        def broken():
            raise OSError("servicio de audio caido")

        monkeypatch.setattr(WINDOWS_AUDIO, "list_microphones", broken)
        app.open()

        app.start_listening("#voice-mic-btn")  # arranca igual: el aviso nunca bloquea por una falla de lectura

        assert app.page.get_attribute("#voice-mic-btn", "data-listening") == "true"
        app.page.click("#voice-mic-btn")
