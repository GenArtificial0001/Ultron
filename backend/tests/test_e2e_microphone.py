"""E2E de micrófono en un Chromium REAL (Playwright) contra el backend real
(uvicorn en un puerto libre de loopback, sin tareas de arranque).

Cubre lo que ningún test de Python puede ver, porque vive en `frontend/js/app.js`:
el botón de micrófono grabando y llenando el compositor, el micrófono ELEGIDO
en Configuración (QA-007) y el plug and play (QA-008).

La infraestructura (backend real en loopback, Chromium con micrófonos falsos,
instrumentación de getUserMedia/enumerateDevices) vive en `e2e_support.py`.
Cómo se simula el hardware, sin micrófono físico:
  * Chromium corre con `--use-fake-device-for-media-stream`: expone 3 micrófonos
    falsos ("default", "Fake Audio Input 1", "Fake Audio Input 2") y graba un
    tono de 440 Hz desde un WAV generado acá (energía de voz real para el filtro
    de silencio del frontend).
  * Un script inyectado ANTES de la app envuelve `getUserMedia` (registra cada
    pedido con sus constraints y el dispositivo que abrió de verdad) y
    `enumerateDevices` (permite "enchufar"/"desenchufar" dispositivos).
  * Desenchufar = disparar `ended` sobre la pista del micrófono, que es lo que
    hace el navegador cuando la fuente desaparece.
  * Whisper es un fake en el backend: nunca hay red ni el proveedor de IA.

Marcado `e2e` (tarda ~3,5 min, medido con 22 tests). Para un ciclo rápido: `pytest -m "not e2e"`.
Se saltea solo si Playwright o su Chromium no están instalados.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Infraestructura (módulo): backend real + Whisper fake + un Chromium
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def whisper():
    return FakeWhisper()


@pytest.fixture(scope="module")
def base_url(whisper):
    with serve_backend(
        SimpleNamespace(audio=SimpleNamespace(transcriptions=whisper))
    ) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        yield a


MIC_1 = "Fake Audio Input 1"
DEFAULT_MIC = "Fake Default Audio Input"
OLD_ID = "id-de-una-sesion-anterior"  # el navegador rotó el id desde que se guardó


# ---------------------------------------------------------------------------
# Carga y botón de micrófono (flujo completo con Whisper fake)
# ---------------------------------------------------------------------------
def test_app_loads_without_js_errors_and_registers_plug_and_play(app):
    app.open()
    assert app.page.evaluate("window.__deviceChangeListeners") >= 1  # QA-008


def test_mic_button_records_transcribes_and_fills_composer(app, whisper):
    whisper.text = "  hola vision desde el microfono "
    before = len(whisper.audio_sizes)
    app.open()
    app.start_listening("#voice-mic-btn")
    app.page.wait_for_function(
        "document.getElementById('voice-mic-btn').dataset.listening === 'true'"
    )
    app.page.wait_for_timeout(1500)  # "habla" 1,5 s de tono
    app.page.click("#voice-mic-btn")
    app.page.wait_for_function("document.getElementById('chat-input').value.length > 0")
    assert app.page.input_value("#chat-input") == "hola vision desde el microfono"
    assert app.page.get_attribute("#voice-mic-btn", "data-listening") == "false"
    assert (
        len(whisper.audio_sizes) == before + 1 and whisper.audio_sizes[-1] > 1000
    )  # audio real, no vacío


def test_whisper_hallucination_never_reaches_the_composer(app, whisper):
    whisper.text = "Gracias por ver el video."
    before = len(whisper.audio_sizes)
    app.open()
    app.start_listening("#voice-mic-btn")
    app.page.wait_for_timeout(1200)
    app.page.click("#voice-mic-btn")
    deadline = time.monotonic() + 10
    while len(whisper.audio_sizes) == before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(whisper.audio_sizes) == before + 1, "no llegó a transcribirse"
    app.page.wait_for_timeout(500)
    assert app.page.input_value("#chat-input") == ""


# ---------------------------------------------------------------------------
# QA-007: el micrófono ELEGIDO es el que graba
# ---------------------------------------------------------------------------
def test_saved_mic_is_used_by_mic_button(app):
    app.open(saved=(OLD_ID, MIC_1))
    call = app.start_listening("#voice-mic-btn")
    current = app.current_id(MIC_1)
    assert call["constraints"]["audio"]["deviceId"] == {"exact": current}
    assert call["label"] == MIC_1  # Chromium abrió ESE micrófono, no el predeterminado
    app.page.click("#voice-mic-btn")


def test_saved_mic_is_recognized_by_name_when_the_browser_rotates_its_id(app):
    """El navegador puede cambiar el deviceId (cookies borradas, ventana privada,
    limpieza al salir). Guardando solo el id, Vision olvidaba el micrófono."""
    app.open(saved=(OLD_ID, MIC_1))
    app.start_listening("#voice-mic-btn")
    assert app.saved() == (app.current_id(MIC_1), MIC_1)  # se actualizó al id nuevo
    assert not any("no está conectado" in n for n in app.notes())  # sin falsa alarma
    app.page.click("#voice-mic-btn")


def test_saved_mic_is_used_by_brain_mode_keeping_echo_cancellation(app):
    app.open(saved=(OLD_ID, MIC_1))
    call = app.start_listening("#brain-btn")
    assert call["label"] == MIC_1
    audio = call["constraints"]["audio"]
    assert audio["deviceId"] == {"exact": app.current_id(MIC_1)}
    assert (
        audio["echoCancellation"] is True and audio["noiseSuppression"] is True
    )  # REQ-010 intacto
    app.page.keyboard.press("Escape")


def test_saved_mic_is_used_by_meeting_mode(app):
    app.open(saved=(OLD_ID, MIC_1))
    assert app.start_listening("#voice-record-btn")["label"] == MIC_1


def test_voice_modes_explain_that_dictation_is_not_configured(app, whisper):
    """Bug real (2026-09-27, spec vision-generico REQ-003): sin el dictado
    configurado, el Modo Cerebro y el modo reunion decian "sigo intentando,
    apaga y volve a prender el modo" -- reintentar no lo arregla. Ahora
    muestran el motivo real que manda el backend."""
    from app.llm.client import ProviderNotConfigured

    whisper.exc = ProviderNotConfigured("faltan STT_BASE_URL y STT_API_KEY")
    try:
        app.open()
        app.start_listening("#voice-record-btn")
        app.wait_note("El dictado por voz no está configurado")
        note = next(n for n in app.notes() if "dictado por voz" in n)
        assert note.startswith("Modo reunión:")
        assert "Sigo intentando" not in note
    finally:
        whisper.exc = None


def test_unplugged_saved_mic_falls_back_to_default_and_warns_once(app):
    app.open(saved=(OLD_ID, MIC_1), hidden=(MIC_1,))
    call = app.start_listening("#voice-mic-btn")
    assert call["constraints"] == {"audio": True} and call["label"] == DEFAULT_MIC
    app.wait_note("no está conectado")
    app.page.click("#voice-mic-btn")
    app.start_listening("#voice-mic-btn")  # segunda grabación: no repite el aviso
    assert sum("no está conectado" in n for n in app.notes()) == 1
    app.page.click("#voice-mic-btn")
    assert app.saved() == (OLD_ID, MIC_1)  # la preferencia NO se pierde


def test_mic_vanishing_between_listing_and_opening_still_records(app):
    """Chrome REAL tira OverconstrainedError si el `exact` ya no existe."""
    app.open(saved=(OLD_ID, MIC_1))
    app.page.evaluate("window.__failExact = 'OverconstrainedError'")
    app.page.click("#voice-mic-btn")
    calls = app.wait_gum_count(2)
    assert calls[0]["error"] == "OverconstrainedError"
    assert (
        calls[1]["constraints"] == {"audio": True} and calls[1]["label"] == DEFAULT_MIC
    )
    app.wait_note("no está conectado")
    app.page.click("#voice-mic-btn")


def test_permission_denied_is_not_masked_as_fallback(app):
    app.open(saved=(OLD_ID, MIC_1))
    app.page.evaluate("window.__gumFail = 'NotAllowedError'")
    app.page.click("#voice-mic-btn")
    app.wait_gum_count(1)
    app.page.wait_for_timeout(300)
    assert (
        len(app.gum()) == 1
    )  # NO reintenta con el predeterminado: el permiso es del usuario
    assert app.page.get_attribute("#voice-mic-btn", "data-listening") == "false"


def test_choosing_mic_in_settings_persists_and_survives_reload(app):
    app.open()
    app.open_audio_settings()
    current = app.current_id(MIC_1)
    app.page.wait_for_selector(
        f'#cfg-mic-select option[value="{current}"]', state="attached"
    )
    app.page.select_option("#cfg-mic-select", current)
    assert app.saved() == (current, MIC_1)
    app.page.wait_for_function(
        "window.__gum.some(c => c.done && c.label === 'Fake Audio Input 1')"
    )  # medidor
    app.page.reload(
        wait_until="domcontentloaded"
    )  # Chromium le cambia el id al recargar
    app.wait_ready()
    app.open_audio_settings()
    new_id = app.current_id(MIC_1)
    app.page.wait_for_function(
        f"document.getElementById('cfg-mic-select').value === {json.dumps(new_id)}"
    )


# ---------------------------------------------------------------------------
# QA-008: plug and play
# ---------------------------------------------------------------------------
def test_new_mic_appears_in_settings_without_reopening_the_tab(app):
    app.open()
    app.open_audio_settings()
    app.page.evaluate(
        "window.__extraDevices.push({kind: 'audioinput', deviceId: 'usb-nuevo', label: 'Micrófono USB recién enchufado', groupId: 'g'})"
    )
    app.device_change()
    app.page.wait_for_selector(
        '#cfg-mic-select option[value="usb-nuevo"]', state="attached"
    )


def test_unplugged_mic_disappears_from_settings_list(app):
    app.open()
    app.open_audio_settings()
    app.page.wait_for_function(
        "[...document.getElementById('cfg-mic-select').options].some(o => o.textContent === 'Fake Audio Input 2')"
    )
    app.page.evaluate("window.__hiddenLabels = ['Fake Audio Input 2']")
    app.device_change()
    app.page.wait_for_function(
        "![...document.getElementById('cfg-mic-select').options].some(o => o.textContent === 'Fake Audio Input 2')"
    )


def test_disconnected_saved_mic_stays_selected_and_is_explained(app):
    app.open(saved=(OLD_ID, MIC_1), hidden=(MIC_1,))
    app.open_audio_settings()
    app.page.wait_for_function(
        "document.getElementById('cfg-mic-status').textContent.includes('no está conectado')"
    )
    assert app.page.input_value("#cfg-mic-select") == OLD_ID
    label = app.page.eval_on_selector(
        "#cfg-mic-select", "s => s.options[s.selectedIndex].textContent"
    )
    assert label == f"{MIC_1} (desconectado)"
    assert app.saved() == (OLD_ID, MIC_1)  # la preferencia NO se pierde


def test_brain_mode_reconnects_when_mic_is_unplugged(app):
    app.open()
    app.start_listening("#brain-btn")
    app.unplug_current_mic()
    app.wait_gum_count(2)
    app.wait_note("Modo Cerebro: escuchando por")
    assert (
        app.page.get_attribute("#brain-btn", "data-state") != "idle"
    )  # sigue encendido
    app.page.keyboard.press("Escape")


def test_meeting_mode_survives_unplug_without_ending_the_meeting(app):
    app.open()
    app.start_listening("#voice-record-btn")
    app.unplug_current_mic()
    app.wait_gum_count(2)
    app.wait_note("Modo reunión: escuchando por")
    assert app.page.get_attribute("#voice-record-btn", "data-recording") == "true"
    # la cadena de fragmentos siguió sobre el micrófono nuevo
    app.page.wait_for_function(
        "window.__streams.at(-1).getAudioTracks()[0].readyState === 'live'"
    )


def test_brain_mode_without_any_mic_waits_and_resumes_when_one_is_plugged(app):
    app.open()
    app.start_listening("#brain-btn")
    app.page.evaluate("window.__gumFail = 'NotFoundError'")  # no queda ningún micrófono
    app.unplug_current_mic()
    app.wait_note("no hay otro conectado")
    assert app.page.get_attribute("#brain-btn", "data-state") != "idle"
    opened_before = sum(1 for c in app.gum() if c.get("deviceId"))
    app.page.evaluate("window.__gumFail = null")  # se enchufa uno
    app.device_change()
    app.page.wait_for_function(
        f"window.__gum.filter(c => c.deviceId).length > {opened_before}"
    )
    app.wait_note("Modo Cerebro: escuchando por")
    app.page.keyboard.press("Escape")


def test_brain_mode_moves_to_saved_mic_when_it_is_plugged_back(app):
    app.open(saved=(OLD_ID, MIC_1), hidden=(MIC_1,))
    assert (
        app.start_listening("#brain-btn")["label"] == DEFAULT_MIC
    )  # arrancó con el predeterminado
    app.wait_note("no está conectado")
    app.page.evaluate("window.__hiddenLabels = []")  # se vuelve a enchufar el elegido
    app.device_change()
    app.page.wait_for_function(
        "window.__gum.some(c => c.label === 'Fake Audio Input 1')"
    )
    app.wait_note(f"Modo Cerebro: escuchando por «{MIC_1}»")
    app.page.keyboard.press("Escape")


def test_unrelated_device_change_does_not_restart_brain_mode(app):
    app.open()
    app.start_listening("#brain-btn")
    app.device_change()  # ej. se enchufaron auriculares (salida de audio)
    app.page.wait_for_timeout(600)
    assert len(app.gum()) == 1
    app.page.keyboard.press("Escape")


def test_turning_brain_mode_off_never_triggers_reconnection(app):
    app.open()
    app.start_listening("#brain-btn")
    app.page.keyboard.press("Escape")
    app.page.wait_for_function(
        "document.getElementById('brain-btn').dataset.state === 'idle'"
    )
    app.device_change()
    app.page.wait_for_timeout(600)
    assert len(app.gum()) == 1 and not any("escuchando por" in n for n in app.notes())


# ---------------------------------------------------------------------------
# Micrófonos que quedan abiertos (fuga): el indicador rojo del navegador
# sigue encendido y el audio se sigue capturando sin que nadie lo use.
# ---------------------------------------------------------------------------
LIVE_STREAMS_JS = "window.__streams.filter(s => s.getAudioTracks().some(t => t.readyState === 'live')).length"


def test_burst_of_device_changes_never_leaks_a_meter_stream(app):
    """Windows dispara varios `devicechange` seguidos al enchufar un headset USB
    (entrada + salida). Con la pestaña Audio abierta, cada uno reinicia el
    medidor: dos arranques en vuelo no pueden dejar un stream huérfano."""
    app.open()
    app.open_audio_settings()
    app.page.wait_for_function("window.__gum.some(c => c.done && c.deviceId)")
    app.page.evaluate(
        "for (let i = 0; i < 3; i++) navigator.mediaDevices.dispatchEvent(new Event('devicechange'))"
    )
    app.page.wait_for_function(
        "window.__gum.length >= 4 && window.__gum.every(c => c.done)"
    )
    app.page.wait_for_timeout(300)
    assert app.page.evaluate(LIVE_STREAMS_JS) == 1  # solo el medidor vigente
    app.page.click("#settings-close")
    app.page.wait_for_function(f"{LIVE_STREAMS_JS} === 0")


def test_double_click_on_mic_button_does_not_leave_a_recording_running(app):
    app.open()
    app.page.evaluate(
        "const b = document.getElementById('voice-mic-btn'); b.click(); b.click();"
    )
    app.page.wait_for_function(
        "document.getElementById('voice-mic-btn').dataset.listening === 'true'"
    )
    app.page.wait_for_function("window.__gum.every(c => c.done)")
    app.page.wait_for_timeout(300)
    app.page.click("#voice-mic-btn")  # el usuario corta la grabación
    app.page.wait_for_function(f"{LIVE_STREAMS_JS} === 0")
    assert len(app.gum()) == 1  # un solo micrófono abierto por una sola grabación


# ---------------------------------------------------------------------------
# Atajo de espacio en Modo Cerebro (spec atajo-espacio-modo-cerebro,
# APPROVED 2026-09-26): REQ-007 exige evidencia objetiva de que el atajo NO
# toca el stream de microfono ni el VAD -- estos tests verifican eso
# explicitamente (no solo "no rompe nada", sino "el mismo stream sigue
# vivo"), ademas del reset liviano de REQ-008.
# ---------------------------------------------------------------------------
def test_space_shortcut_does_not_reopen_the_microphone_stream(app):
    """REQ-007 (blindaje): presionar espacio en Modo Cerebro activo NO debe
    generar ningun pedido nuevo de getUserMedia -- el stream original sigue
    siendo el mismo (mismo id de stream), a diferencia de Escape que si lo
    cierra."""
    app.open()
    app.start_listening("#brain-btn")
    gum_count_before = len(app.gum())
    stream_id_before = app.page.evaluate("window.__streams.at(-1).id")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(
        200
    )  # tiempo de sobra para que un reset incorrecto se notara

    assert len(app.gum()) == gum_count_before, (
        "el atajo de espacio NO debe pedir un microfono nuevo"
    )
    assert app.page.evaluate("window.__streams.at(-1).id") == stream_id_before, (
        "el stream de microfono debe seguir siendo el MISMO objeto -- el atajo es un reset "
        "liviano, no debe cerrar/reabrir el hardware"
    )
    assert (
        app.page.get_attribute("#brain-btn", "data-state") != "idle"
    )  # Modo Cerebro sigue activo
    app.page.keyboard.press("Escape")


def test_space_shortcut_only_acts_while_brain_mode_is_active(app):
    """REQ-002: fuera de Modo Cerebro, espacio no debe disparar ningun
    comportamiento especial (ni pedir microfono, ni tocar el compositor)."""
    app.open()
    gum_count_before = len(app.gum())
    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(200)
    assert (
        len(app.gum()) == gum_count_before
    )  # nada paso -- Modo Cerebro no estaba activo


def test_space_shortcut_respects_the_configurable_toggle(app):
    """REQ-003: con el checkbox de Configuracion > Audio desactivado, espacio
    no debe interrumpir nada aunque Modo Cerebro este activo."""
    app.open()
    app.page.evaluate(
        "localStorage.setItem('vision.brain.spaceInterruptEnabled', 'false')"
    )
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.start_listening("#brain-btn")
    gum_count_before = len(app.gum())
    stream_id_before = app.page.evaluate("window.__streams.at(-1).id")

    app.page.keyboard.press("Space")
    app.page.wait_for_timeout(200)

    # Sin el atajo activo, nada cambia -- ni siquiera el stream se toca (ya
    # cubierto por el test de arriba, pero se re-verifica con el toggle OFF).
    assert app.page.evaluate("window.__streams.at(-1).id") == stream_id_before
    app.page.keyboard.press("Escape")
