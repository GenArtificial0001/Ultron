"""
Clonacion local de voz con Chatterbox Multilingual (spec voz-fish-audio REQ-007).

Tres piezas:
- **Muestra de voz**: el usuario la sube desde Configuracion > Audio (WAV o MP3,
  10 s como minimo). Se guarda en `<carpeta de datos>/data/voz/`, que nunca se
  versiona: la voz de una persona es un dato personal.
- **Instalacion** (boton "Instalar clonacion local"): `chatterbox-tts` pesa
  varios GB (PyTorch y el modelo) y se instala EN EL ENTORNO DE VISION (decision
  del usuario). Riesgo real: `chatterbox-tts` 0.1.7 pide `numpy<2` en Python
  < 3.13 y Vision pide `numpy>=2.5`. Por eso primero se SIMULA la instalacion
  (`pip install --dry-run --report`) y, si fuera a cambiar la version de algun
  paquete ya instalado, no se instala nada y se explica por que.
- **Motor**: carga el modelo una sola vez (tarda) y genera cada frase con la
  muestra como referencia. En una PC sin placa NVIDIA tarda varios segundos por
  frase: queda exento del limite de 2 s (REQ-004) y Configuracion lo avisa.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import BASE_DIR, IS_FROZEN
from app.voice.engines import Audio, VoiceEngineError

logger = logging.getLogger(__name__)

PACKAGE = "chatterbox-tts"
LANGUAGE = "es"
SAMPLE_DIR = BASE_DIR / "data" / "voz"
SAMPLE_STEM = "muestra"
MIN_SAMPLE_SECONDS = 10.0
MAX_SAMPLE_BYTES = 25 * 1024 * 1024
SAMPLE_FORMATS = ("wav", "mp3")


class SampleError(ValueError):
    """Muestra rechazada; el mensaje ya esta pensado para el usuario."""


# ---------------------------------------------------------------------------
# REQ-007: duracion de la muestra, sin dependencias (el entorno de Vision no
# trae librerias de audio). WAV: se leen los bloques RIFF (sirve para PCM y
# float, que el modulo `wave` no abre). MP3: cantidad de frames del encabezado
# Xing/Info si existe (VBR), o tamaño / bitrate (CBR).
# ---------------------------------------------------------------------------
def wav_duration(data: bytes) -> float:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SampleError("El archivo no es un WAV válido. Exportalo de nuevo como WAV o MP3.")
    pos = 12
    byte_rate = 0
    data_size = 0
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = pos + 8
        if chunk_id == b"fmt " and size >= 16:
            (byte_rate,) = struct.unpack("<I", data[body + 8 : body + 12])
        elif chunk_id == b"data":
            # Algunas grabadoras dejan el tamaño en 0 o en 0xFFFFFFFF al grabar en vivo.
            data_size = min(size, len(data) - body)
            break
        pos = body + size + (size % 2)
    if not byte_rate or not data_size:
        raise SampleError("No pude leer el audio del WAV. Exportalo de nuevo como WAV o MP3.")
    return data_size / byte_rate


_MP3_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}


def mp3_duration(data: bytes) -> float:
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:  # etiqueta ID3v2 al principio
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size
    while pos + 4 <= len(data):
        if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
            header = struct.unpack(">I", data[pos : pos + 4])[0]
            version_bits = (header >> 19) & 0b11
            layer_bits = (header >> 17) & 0b11
            bitrate_idx = (header >> 12) & 0xF
            rate_idx = (header >> 10) & 0b11
            if version_bits != 0b01 and layer_bits == 0b01 and 0 < bitrate_idx < 15 and rate_idx < 3:
                break  # frame de MPEG Layer III valido
        pos += 1
    else:
        raise SampleError("El archivo no es un MP3 válido. Exportalo de nuevo como WAV o MP3.")
    version = {0b11: 1, 0b10: 2, 0b00: 25}[version_bits]
    bitrate = _MP3_BITRATES[1 if version == 1 else 2][bitrate_idx] * 1000
    sample_rate = _MP3_RATES[version][rate_idx]
    samples_per_frame = 1152 if version == 1 else 576
    mono = ((header >> 6) & 0b11) == 0b11
    side_info = (17 if mono else 32) if version == 1 else (9 if mono else 17)
    xing = pos + 4 + side_info
    if data[xing : xing + 4] in (b"Xing", b"Info"):
        (flags,) = struct.unpack(">I", data[xing + 4 : xing + 8])
        if flags & 0x1:
            (frames,) = struct.unpack(">I", data[xing + 8 : xing + 12])
            return frames * samples_per_frame / sample_rate
    return (len(data) - pos) * 8 / bitrate


def audio_duration(data: bytes, fmt: str) -> float:
    return wav_duration(data) if fmt == "wav" else mp3_duration(data)


def sample_format(filename: str) -> str:
    fmt = Path(filename or "").suffix.lower().lstrip(".")
    if fmt not in SAMPLE_FORMATS:
        raise SampleError("La muestra tiene que ser un archivo WAV o MP3.")
    return fmt


def save_sample(data: bytes, filename: str) -> dict[str, Any]:
    """REQ-007: valida y guarda la muestra (reemplaza la anterior)."""
    fmt = sample_format(filename)
    if len(data) > MAX_SAMPLE_BYTES:
        raise SampleError("La muestra pesa más de 25 MB. Con 1 a 2 minutos de voz alcanza.")
    seconds = audio_duration(data, fmt)
    if seconds < MIN_SAMPLE_SECONDS:
        raise SampleError(
            f"La muestra dura {seconds:.0f} s y hacen falta {MIN_SAMPLE_SECONDS:.0f} s como mínimo "
            "(se recomienda 1 a 2 minutos de voz clara, sin ruido de fondo)."
        )
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for old in SAMPLE_DIR.glob(f"{SAMPLE_STEM}.*"):
        old.unlink()
    (SAMPLE_DIR / f"{SAMPLE_STEM}.{fmt}").write_bytes(data)
    return sample_info()


def sample_path() -> Path | None:
    for fmt in SAMPLE_FORMATS:
        path = SAMPLE_DIR / f"{SAMPLE_STEM}.{fmt}"
        if path.exists():
            return path
    return None


def sample_info() -> dict[str, Any]:
    path = sample_path()
    if path is None:
        return {"present": False, "seconds": None, "format": None}
    fmt = path.suffix.lstrip(".")
    try:
        seconds: float | None = round(audio_duration(path.read_bytes(), fmt), 1)
    except SampleError:
        seconds = None
    return {"present": True, "seconds": seconds, "format": fmt}


def delete_sample() -> None:
    path = sample_path()
    if path is not None:
        path.unlink()


# ---------------------------------------------------------------------------
# REQ-007: instalacion con simulacion previa, en un hilo aparte para que la
# pantalla muestre el progreso sin trabar el servidor.
# ---------------------------------------------------------------------------
def is_installed() -> bool:
    return importlib.util.find_spec("chatterbox") is not None


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_versions() -> dict[str, str]:
    return {
        _normalize(dist.metadata["Name"]): dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    }


def plan_conflicts(report: dict[str, Any], installed: dict[str, str]) -> list[tuple[str, str, str]]:
    """Paquetes ya instalados cuya version cambiaria: (nombre, actual, nueva).
    Cualquier cambio cuenta (subir o bajar): el usuario pidio no tocar lo que
    Vision ya usa."""
    conflicts = []
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        name = _normalize(str(meta.get("name", "")))
        new = str(meta.get("version", ""))
        current = installed.get(name)
        if current and new and current != new:
            conflicts.append((name, current, new))
    return sorted(conflicts)


def describe_conflicts(conflicts: list[tuple[str, str, str]], python_version: tuple[int, int]) -> str:
    listed = ", ".join(f"{name} {old} → {new}" for name, old, new in conflicts[:5])
    more = f" y {len(conflicts) - 5} más" if len(conflicts) > 5 else ""
    message = (
        f"No instalé nada: la clonación local cambiaría paquetes que Vision ya usa ({listed}{more}) "
        "y podría dejar de funcionar."
    )
    if python_version < (3, 13) and any(name == "numpy" for name, _, _ in conflicts):
        return message + " Con Python 3.13 o más no pasa: instalalo y volvé a crear el entorno de Vision."
    return message + " Avisá a quien administra Vision."


def _run_pip(args: list[str], on_line: Callable[[str], None]) -> int:
    """`python -m pip <args>` con el mismo Python que corre Vision (su entorno)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip())
    return proc.wait()


def _run_warmup(on_line: Callable[[str], None]) -> int:
    """Descarga el modelo en un proceso aparte (no carga PyTorch en el servidor
    solo para bajar archivos): la primera frase no espera la descarga."""
    code = (
        "from chatterbox.mtl_tts import ChatterboxMultilingualTTS;"
        "ChatterboxMultilingualTTS.from_pretrained(device='cpu', t3_model='v3')"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_line(line.rstrip())
    return proc.wait()


class _InstallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "idle"  # idle | checking | installing | downloading | done | error
        self.message = ""
        self.log: deque[str] = deque(maxlen=30)
        self.thread: threading.Thread | None = None

    def set(self, status: str, message: str) -> None:
        with self.lock:
            self.status = status
            self.message = message

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "installed": is_installed(),
                "status": self.status,
                "message": self.message,
                "log": list(self.log),
            }


INSTALL = _InstallState()


def install_status() -> dict[str, Any]:
    return INSTALL.snapshot()


def start_install() -> dict[str, Any]:
    """Arranca la instalacion en segundo plano (si no hay una en curso)."""
    if IS_FROZEN:
        INSTALL.set("error", "La clonación local no se puede instalar en esta forma de ejecutar Vision.")
        return install_status()
    with INSTALL.lock:
        running = INSTALL.thread is not None and INSTALL.thread.is_alive()
        if not running:
            INSTALL.log.clear()
            INSTALL.thread = threading.Thread(target=_install_worker, name="voz-local-install", daemon=True)
            INSTALL.thread.start()
    return install_status()


def _install_worker() -> None:
    def log(line: str) -> None:
        if line:
            with INSTALL.lock:
                INSTALL.log.append(line)

    try:
        INSTALL.set("checking", "Revisando que la instalación no cambie nada de lo que Vision ya usa…")
        with tempfile.TemporaryDirectory(prefix="vision-voz-") as tmp:
            report_path = Path(tmp) / "report.json"
            code = _run_pip(
                ["install", "--dry-run", "--quiet", "--report", str(report_path), PACKAGE], log
            )
            if code != 0 or not report_path.exists():
                INSTALL.set(
                    "error",
                    "No pude revisar la instalación (sin conexión o el índice de paquetes no respondió). "
                    "Revisá tu conexión y el proxy de tu empresa y volvé a intentar.",
                )
                return
            report = json.loads(report_path.read_text(encoding="utf-8"))
        conflicts = plan_conflicts(report, installed_versions())
        if conflicts:
            logger.warning("Instalacion de %s cancelada por conflictos: %s", PACKAGE, conflicts)
            INSTALL.set("error", describe_conflicts(conflicts, sys.version_info[:2]))
            return

        INSTALL.set("installing", "Instalando la clonación local (varios GB, puede tardar bastante)…")
        if _run_pip(["install", PACKAGE], log) != 0:
            INSTALL.set("error", "La instalación falló. El detalle quedó en el log de Vision; volvé a intentar.")
            return
        importlib.invalidate_caches()

        INSTALL.set("downloading", "Descargando el modelo de voz (la primera vez tarda varios minutos)…")
        if _run_warmup(log) != 0:
            INSTALL.set(
                "error",
                "Se instaló, pero no pude descargar el modelo de voz. Revisá tu conexión y volvé a intentar.",
            )
            return
        INSTALL.set("done", "Clonación local lista. Subí tu muestra de voz y agregala a la lista.")
    except Exception:  # noqa: BLE001 -- el hilo nunca debe morir en silencio
        logger.exception("Error instalando la clonacion local de voz")
        INSTALL.set("error", "La instalación falló. El detalle quedó en el log de Vision; volvé a intentar.")
    finally:
        for line in list(INSTALL.log)[-5:]:
            logger.info("voz local (pip): %s", line)


# ---------------------------------------------------------------------------
# REQ-007: el motor. El modelo se carga una sola vez y se comparte; `generate`
# no es seguro con varias frases a la vez, por eso va con candado.
# ---------------------------------------------------------------------------
_MODEL: Any = None
_MODEL_LOCK = threading.Lock()


def _load_model() -> Any:
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            import torch
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            device = "cuda" if torch.cuda.is_available() else "cpu"
            started = time.monotonic()
            _MODEL = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
            logger.info("Modelo de voz local cargado en %.1f s (%s)", time.monotonic() - started, device)
        return _MODEL


def _to_wav(samples: Any, sample_rate: int) -> bytes:
    """Tensor float [-1, 1] -> WAV PCM 16 bits con la stdlib (evita depender del
    backend de audio de torchaudio)."""
    pcm = (samples.squeeze().clamp(-1, 1) * 32767).to(dtype=_int16()).cpu().numpy().tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(sample_rate))
        out.writeframes(pcm)
    return buf.getvalue()


def _int16() -> Any:
    import torch

    return torch.int16


class LocalCloneEngine:
    name = "local"
    label = "Clonación local"
    models = ("multilingual-v3",)
    cloud = False

    def synthesize(self, text: str, *, model: str, voice_id: str) -> Audio:
        if not is_installed():
            raise VoiceEngineError("no_instalado")
        sample = sample_path()
        if sample is None:
            raise VoiceEngineError("sin_muestra")
        try:
            tts = _load_model()
            with _MODEL_LOCK:
                samples = tts.generate(text, language_id=LANGUAGE, audio_prompt_path=str(sample))
            return Audio(_to_wav(samples, tts.sr), "audio/wav")
        except VoiceEngineError:
            raise
        except Exception as exc:  # noqa: BLE001 -- cualquier falla del modelo salta al siguiente motor
            logger.exception("Error generando voz local")
            raise VoiceEngineError("fallo", str(exc)) from exc

    def list_voices(self) -> list[dict[str, str]]:
        return []  # la voz es la muestra subida
