"""
Contrato de los motores de voz y los motores en la nube (spec voz-fish-audio
REQ-001/005/006).

Cada motor sabe una sola cosa: convertir UNA frase en audio. La cadena de
prioridad, las pausas y los avisos viven en `service.py`, asi sumar un motor
nuevo es escribir una clase con `synthesize` (y opcionalmente `list_voices`) y
registrarla en `ENGINES`, sin tocar a los demas (decision del usuario: cambios
"seguido", motores intercambiables).

Las llamadas usan `requests` (ya es dependencia de Vision) en vez de los SDK de
cada servicio: cero dependencias nuevas, y `requests` respeta el HTTP_PROXY /
HTTPS_PROXY del `.env` que `config._apply_proxy` deja en el entorno (proxy de
cualquier empresa, spec vision-generico REQ-004). Una `Session` reusa la
conexion entre frases: ahorra el saludo TLS en cada una (REQ-004, menos de 2 s).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

# (conexion, lectura) en segundos: una frase corta tarda ~1 s en generarse; si
# el servicio no contesta en ese margen, conviene saltar al siguiente motor.
_TIMEOUT = (5, 30)
_SESSION = requests.Session()


@dataclass(frozen=True)
class Audio:
    data: bytes
    mime: str


class VoiceEngineError(Exception):
    """Falla de un motor, clasificada para decidir la pausa y el aviso.

    `kind`: sin_clave | clave_invalida | sin_voz | sin_credito | limite |
    sin_conexion | no_instalado | sin_muestra | fallo (ver
    `friendly_errors.describe_voice_engine_error`). `detail` va solo al log."""

    def __init__(self, kind: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.status = status


class VoiceEngine(Protocol):
    name: str
    label: str
    models: tuple[str, ...]
    cloud: bool

    def synthesize(self, text: str, *, model: str, voice_id: str) -> Audio: ...

    def list_voices(self) -> list[dict[str, str]]: ...


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Pedido HTTP con los errores de red traducidos a `sin_conexion`."""
    try:
        return _SESSION.request(method, url, timeout=_TIMEOUT, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise VoiceEngineError("sin_conexion", str(exc)) from exc


def _body_text(resp: requests.Response, limit: int = 300) -> str:
    try:
        return resp.text[:limit]
    except Exception:  # noqa: BLE001 -- solo es detalle para el log
        return ""


# ---------------------------------------------------------------------------
# REQ-005: Fish Audio (https://docs.fish.audio). POST /v1/tts con la voz por
# `reference_id` y el modelo en el header `model`; `s2.1-pro-free` es el modelo
# gratuito para el salto automatico cuando se acaba el credito.
# ---------------------------------------------------------------------------
class FishAudioEngine:
    name = "fish"
    label = "Fish Audio"
    models = ("s2.1-pro", "s2.1-pro-free", "s2-pro", "s1")
    cloud = True
    base_url = "https://api.fish.audio"

    def _key(self) -> str:
        key = get_settings().fish_api_key.strip()
        if not key:
            raise VoiceEngineError("sin_clave")
        return key

    def synthesize(self, text: str, *, model: str, voice_id: str) -> Audio:
        key = self._key()
        if not voice_id:
            raise VoiceEngineError("sin_voz")
        resp = _request(
            "POST",
            f"{self.base_url}/v1/tts",
            headers={"Authorization": f"Bearer {key}", "model": model or self.models[0]},
            # "balanced": menos demora que "normal" con casi la misma calidad.
            json={"text": text, "reference_id": voice_id, "format": "mp3", "latency": "balanced"},
        )
        if resp.status_code == 200 and resp.content:
            return Audio(resp.content, "audio/mpeg")
        raise _classify(resp, voice_status=(404,))

    def list_voices(self) -> list[dict[str, str]]:
        """Las voces propias de la cuenta (clonadas o guardadas). Las de la
        biblioteca publica se usan pegando su ID a mano."""
        resp = _request(
            "GET",
            f"{self.base_url}/model",
            headers={"Authorization": f"Bearer {self._key()}"},
            params={"self": "true", "page_size": 100},
        )
        if resp.status_code != 200:
            raise _classify(resp)
        items = resp.json().get("items", [])
        return [{"id": str(i.get("_id", "")), "name": str(i.get("title", ""))} for i in items if i.get("_id")]


# ---------------------------------------------------------------------------
# REQ-006: ElevenLabs (https://elevenlabs.io/docs). POST
# /v1/text-to-speech/{voice_id} con `model_id`; `eleven_flash_v2_5` es el de
# menor demora y habla español.
# ---------------------------------------------------------------------------
class ElevenLabsEngine:
    name = "elevenlabs"
    label = "ElevenLabs"
    models = ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_v3")
    cloud = True
    base_url = "https://api.elevenlabs.io"

    def _key(self) -> str:
        key = get_settings().elevenlabs_api_key.strip()
        if not key:
            raise VoiceEngineError("sin_clave")
        return key

    def synthesize(self, text: str, *, model: str, voice_id: str) -> Audio:
        key = self._key()
        if not voice_id:
            raise VoiceEngineError("sin_voz")
        resp = _request(
            "POST",
            f"{self.base_url}/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key},
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": model or self.models[0]},
        )
        if resp.status_code == 200 and resp.content:
            return Audio(resp.content, "audio/mpeg")
        raise _classify(resp, voice_status=(404,))

    def list_voices(self) -> list[dict[str, str]]:
        resp = _request("GET", f"{self.base_url}/v1/voices", headers={"xi-api-key": self._key()})
        if resp.status_code != 200:
            raise _classify(resp)
        voices = resp.json().get("voices", [])
        return [{"id": str(v.get("voice_id", "")), "name": str(v.get("name", ""))} for v in voices if v.get("voice_id")]


# Palabras con las que ElevenLabs (y otros) avisan en el cuerpo de un 401 que lo
# que falta es credito, no una clave valida.
_NO_CREDIT_HINTS = ("quota_exceeded", "payment_required", "insufficient", "credit", "quota")


def _classify(resp: requests.Response, voice_status: tuple[int, ...] = ()) -> VoiceEngineError:
    """Traduce la respuesta de error de un servicio a un `kind` (REQ-003)."""
    status = resp.status_code
    body = _body_text(resp)
    lowered = body.lower()
    if status == 402 or (status in (401, 403) and any(h in lowered for h in _NO_CREDIT_HINTS)):
        kind = "sin_credito"
    elif status in (401, 403):
        kind = "clave_invalida"
    elif status in voice_status:
        kind = "sin_voz"
    elif status == 429:
        kind = "limite"
    else:
        kind = "fallo"
    return VoiceEngineError(kind, f"HTTP {status}: {body}", status)
