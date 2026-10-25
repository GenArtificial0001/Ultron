"""Infraestructura compartida de los tests e2e (marcador `e2e`): backend REAL en
un puerto libre de loopback + un Chromium de Playwright + una pestaña con la
app lista.

No es un modulo de tests (pytest solo junta `test_*.py`): cada `test_e2e_*.py`
arma sus fixtures de modulo con estos context managers. Asi hay UN solo
servidor y UN solo Chromium por modulo, sin duplicar el arranque ni importar
fixtures de otro archivo de tests.

Como se simula el hardware, sin microfono fisico:
  * Chromium corre con `--use-fake-device-for-media-stream`: expone 3 microfonos
    falsos ("default", "Fake Audio Input 1", "Fake Audio Input 2") y graba un
    tono de 440 Hz desde un WAV generado aca (energia de voz real para el filtro
    de silencio del frontend).
  * `INIT_SCRIPT` se inyecta ANTES de la app: envuelve `getUserMedia` (registra
    cada pedido con sus constraints y el dispositivo que abrio de verdad) y
    `enumerateDevices` (permite "enchufar"/"desenchufar" dispositivos).
  * El cliente del proveedor de IA (`main.get_client`) es un fake: nunca hay red.

Si Playwright o su Chromium no estan instalados, los e2e se SALTEAN.
"""

from __future__ import annotations

import json
import math
import socket
import struct
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
]
MIC_KEY = "vision.mic.deviceId"
MIC_LABEL_KEY = "vision.mic.label"

# Instrumentación inyectada antes de que cargue app.js. `window.__gum`: cada
# pedido de micrófono (constraints, dispositivo abierto -- id y nombre -- o
# nombre del error; `done` cuando se resolvió). `__hiddenLabels`: micrófonos
# "desenchufados", por NOMBRE (los ids de Chromium cambian en cada carga):
# enumerateDevices no los lista y abrirlos con `exact` falla como en Chrome
# real. `__failExact`: el dispositivo desaparece justo entre listarlo y
# abrirlo. `__gumFail`: no hay NINGÚN micrófono (o permiso denegado).
# `__deviceChangeListeners`: la app terminó de evaluar y registró su
# listener de plug and play.
INIT_SCRIPT = """
(() => {
  const md = navigator.mediaDevices;
  window.__gum = [];
  window.__streams = [];
  window.__hiddenLabels = [];
  window.__extraDevices = [];
  window.__gumFail = null;
  window.__failExact = null;
  window.__deviceChangeListeners = 0;
  // spec audio-sistema-modo-reunion: Record ahora PREGUNTA de donde escucha (ventana de fuente). Los e2e anteriores a esa spec prueban el microfono tocando Record y esperan
  // que se abra directo: arrancan con «no volver a preguntar» (= el comportamiento de siempre). Solo si NADIE lo fijo antes, asi los tests de la ventana pueden activarlo
  // con localStorage y sobrevive a una recarga. Con 'mic' como fuente el camino es el mismo de siempre.
  try { if (localStorage.getItem('vision.meeting.ask') === null) localStorage.setItem('vision.meeting.ask', '0'); } catch (e) { /* sin localStorage */ }
  const origGum = md.getUserMedia.bind(md);
  const origEnum = md.enumerateDevices.bind(md);
  md.getUserMedia = async (constraints) => {
    const rec = { constraints: JSON.parse(JSON.stringify(constraints)) };
    window.__gum.push(rec);
    try {
      const exact = constraints?.audio?.deviceId?.exact;
      if (window.__gumFail) throw new DOMException("simulado", window.__gumFail);
      if (exact && window.__failExact) throw new DOMException("desaparecio", window.__failExact);
      if (exact) {
        const dev = (await origEnum()).find((d) => d.deviceId === exact);
        if (dev && window.__hiddenLabels.includes(dev.label)) {
          throw new DOMException("desenchufado", "OverconstrainedError");
        }
      }
      const stream = await origGum(constraints);
      const track = stream.getAudioTracks()[0];
      rec.deviceId = track.getSettings().deviceId;
      rec.label = track.label;
      window.__streams.push(stream);
      return stream;
    } catch (err) {
      rec.error = err.name;
      throw err;
    } finally {
      rec.done = true;
    }
  };
  md.enumerateDevices = async () => {
    const real = (await origEnum()).filter((d) => !window.__hiddenLabels.includes(d.label));
    return [...real, ...window.__extraDevices];
  };
  const origAdd = md.addEventListener.bind(md);
  md.addEventListener = (type, fn, opts) => {
    if (type === "devicechange") window.__deviceChangeListeners += 1;
    return origAdd(type, fn, opts);
  };
})();
"""


class FakeWindowsAudio:
    """Core Audio de mentira para los e2e: los mismos nombres que los microfonos falsos de Chromium, todos ACTIVOS por defecto. Registra cada escritura."""

    NAMES = ("Fake Default Audio Input", "Fake Audio Input 1", "Fake Audio Input 2")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        from app.audio_control import Microphone

        self.mics = {f"win-{i}": Microphone(f"win-{i}", name, i == 0, False, 70) for i, name in enumerate(self.NAMES)}
        self.writes: list[tuple] = []

    def mute_all(self, *, only_default: bool = True) -> None:
        """Deja silenciado el microfono predeterminado (el caso real del usuario) o todos."""
        from app.audio_control import Microphone

        for mic_id, m in list(self.mics.items()):
            if m.is_default or not only_default:
                self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, True, m.volume)

    def list_microphones(self):
        return list(self.mics.values())

    def set_mute(self, mic_id: str, muted: bool) -> None:
        from app.audio_control import Microphone

        self.writes.append(("mute", mic_id, muted))
        m = self.mics[mic_id]
        self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, muted, m.volume)

    def set_volume(self, mic_id: str, scalar: float) -> None:
        from app.audio_control import Microphone

        self.writes.append(("volume", mic_id, round(scalar, 2)))
        m = self.mics[mic_id]
        self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, m.muted, round(scalar * 100))


WINDOWS_AUDIO = FakeWindowsAudio()  # lo reinicia y instala `serve_backend` en cada arranque


class FakeWhisper:
    """`audio.transcriptions` falso: devuelve `text`, o lanza `exc` si esta seteado."""

    def __init__(self):
        self.text = "hola vision desde el microfono"
        self.exc: BaseException | None = None
        self.audio_sizes: list[int] = []

    def create(self, **kwargs):
        self.audio_sizes.append(len(kwargs["file"].read()))
        if self.exc is not None:
            raise self.exc
        return self.text


@contextmanager
def serve_backend(client: Any) -> Iterator[str]:
    """Levanta el backend real en un puerto libre de loopback, con
    `main.get_client` devolviendo `client`, y entrega su URL base."""
    import uvicorn

    from app import main

    from app import audio_control

    WINDOWS_AUDIO.reset()
    audio_control.set_backend(WINDOWS_AUDIO)  # spec control-microfono-windows: nunca el audio real de quien corre la suite
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "get_client", lambda *a, **k: client)
        # El dictado usa su propio proveedor (spec vision-generico REQ-003): el
        # mismo fake responde /audio/transcriptions.
        mp.setattr(main, "get_stt_client", lambda: (client, "whisper-1"))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        # lifespan="off": sin purga/agentes/alarmas/notificador de fondo -- los
        # e2e prueban el frontend, no las tareas de arranque.
        server = uvicorn.Server(
            uvicorn.Config(
                main.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="off",
                # Las pestañas dejan conexiones abiertas (keep-alive/SSE): sin
                # esto el apagado espera ~13 s a que se cierren solas.
                timeout_graceful_shutdown=1,
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline:
                pytest.fail("el backend no arrancó en 20 s")
            time.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            audio_control.set_backend(None)


@contextmanager
def launch_chromium(audio_dir: Path, base_url: str) -> Iterator[Any]:
    """Chromium con micrófonos falsos que graban un tono de 440 Hz."""
    wav = audio_dir / "voz.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / 16_000)))
                for i in range(16_000 * 4)
            )
        )
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(
                args=[*CHROMIUM_ARGS, f"--use-file-for-fake-audio-capture={wav}"]
            )
        except Exception as exc:  # noqa: BLE001 -- sin Chromium de Playwright no hay e2e posible
            pytest.skip(
                f"Chromium de Playwright no disponible: {str(exc).splitlines()[0]}"
            )
        # Calentamiento: la PRIMERA navegacion de un Chromium y un backend recien
        # levantados puede tardar >30 s en esta maquina (medido: setup de 17 s y
        # un goto que agoto 30 s). Se paga una sola vez aca, con margen, en vez de
        # que el primer test del modulo falle por tiempo y no por un bug.
        warm = b.new_context(permissions=["microphone"])
        warm.new_page().goto(
            base_url + "/", wait_until="domcontentloaded", timeout=120_000
        )
        warm.close()
        try:
            yield b
        finally:
            b.close()


class App:
    """Una pestaña con storage limpio, ya cargada y con la app lista."""

    def __init__(self, page, base_url):
        self.page, self.base_url = page, base_url
        self.page_errors: list[str] = []
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.set_default_timeout(15_000)
        page.set_default_navigation_timeout(30_000)

    def open(self, saved: tuple[str, str] | None = None, hidden: tuple[str, ...] = ()):
        """`saved` = (deviceId, nombre) guardados de una sesion ANTERIOR: el id
        es a proposito uno viejo, porque Chromium cambia los ids en cada carga
        (ver matchSavedMic en app.js). `hidden` = nombres "desenchufados".
        Ambos se fijan con un init script, ANTES de que corra app.js."""
        setup = []
        if saved is not None:
            setup.append(
                f"localStorage.setItem({json.dumps(MIC_KEY)}, {json.dumps(saved[0])});"
            )
            setup.append(
                f"localStorage.setItem({json.dumps(MIC_LABEL_KEY)}, {json.dumps(saved[1])});"
            )
        if hidden:
            setup.append(f"window.__hiddenLabels = {json.dumps(list(hidden))};")
        if setup:
            self.page.add_init_script("\n".join(setup))
        self.page.goto(self.base_url + "/", wait_until="domcontentloaded")
        self.wait_ready()
        return self

    def wait_ready(self) -> None:
        # Señal de "app lista": app.js terminó de evaluarse hasta registrar su
        # listener de plug and play (si no parsea, esto falla).
        self.page.wait_for_function("window.__deviceChangeListeners > 0")

    def gum(self) -> list[dict]:
        return self.page.evaluate("window.__gum")

    def wait_gum_count(self, n: int) -> list[dict]:
        """Espera n pedidos de micrófono YA resueltos (abiertos o fallidos):
        mirar un pedido pendiente da resultados a medias."""
        self.page.wait_for_function(
            f"window.__gum.length >= {n} && window.__gum.slice(0, {n}).every(c => c.done)"
        )
        return self.gum()

    def notes(self) -> list[str]:
        return self.page.eval_on_selector_all(
            ".msg.system-note", "els => els.map(e => e.textContent)"
        )

    def wait_note(self, fragment: str) -> None:
        self.page.wait_for_function(
            "frag => [...document.querySelectorAll('.msg.system-note')].some(e => e.textContent.includes(frag))",
            arg=fragment,
        )

    def unplug_current_mic(self) -> None:
        # Lo que hace el navegador al desaparecer la fuente: `ended` en la pista.
        self.page.evaluate(
            "window.__streams.at(-1).getAudioTracks()[0].dispatchEvent(new Event('ended'))"
        )

    def device_change(self) -> None:
        self.page.evaluate(
            "navigator.mediaDevices.dispatchEvent(new Event('devicechange'))"
        )

    def current_id(self, label: str) -> str:
        """deviceId que tiene HOY (en esta carga) el micrófono con ese nombre."""
        return self.page.evaluate(
            "label => navigator.mediaDevices.enumerateDevices().then(d => d.find(x => x.label === label).deviceId)",
            label,
        )

    def saved(self) -> tuple[str, str]:
        return tuple(
            self.page.evaluate(
                f"[localStorage.getItem({json.dumps(MIC_KEY)}), localStorage.getItem({json.dumps(MIC_LABEL_KEY)})]"
            )
        )

    def open_audio_settings(self) -> None:
        self.page.click("#settings-btn")
        self.page.click('.settings-tab[data-tab="audio"]')
        self.page.wait_for_selector('.settings-pane[data-pane="audio"]:not(.hidden)')

    def start_listening(self, button: str) -> dict:
        """Toca un botón de modo de voz y espera a que el micrófono quede abierto."""
        before = len(self.gum())
        self.page.click(button)
        self.page.wait_for_function(
            f"window.__gum.length > {before} && window.__gum.at(-1).done && window.__gum.at(-1).deviceId"
        )
        return self.gum()[-1]


@contextmanager
def open_app(browser: Any, base_url: str) -> Iterator[App]:
    """Contexto de navegador nuevo (storage limpio) con la instrumentación; al
    cerrar, falla si la página tiró algún error de JS."""
    # clipboard-read/write: permiso adicional para los tests de
    # copiar-formulas-unicode/copiar-mensajes que leen navigator.clipboard
    # tras un click -- Chromium headless lo deniega sin este grant explicito.
    context = browser.new_context(
        permissions=["microphone", "clipboard-read", "clipboard-write"]
    )
    context.add_init_script(INIT_SCRIPT)
    page = context.new_page()
    a = App(page, base_url)
    try:
        yield a
    finally:
        context.close()
    assert a.page_errors == [], f"errores de JS en la página: {a.page_errors}"
