"""
Configuracion de la voz de Vision (spec voz-fish-audio REQ-001/002/003/008).

Se guarda en `<carpeta de datos>/data/voice_config.json` (mismo criterio que
`project_config.json`: un JSON simple, fuera del repo). Es de la instalacion,
no del navegador: el backend la necesita para recorrer la cadena.

- `chain`: motores en orden de prioridad, cada uno con su modelo
  (`[{"engine": "fish", "model": "s2.1-pro"}, ...]`). La voz del navegador no
  figura: es SIEMPRE el ultimo eslabon (REQ-003). Vacia = voz del navegador,
  que es como viene de fabrica (REQ-002).
- `consent`: por motor en la nube, si el usuario confirmo que el texto leido se
  envia a ese servicio. Sin consentimiento no se guarda en la cadena y, por las
  dudas, `service.py` tampoco lo usa (REQ-002).
- `effect`: efecto "IA" (REQ-008), apagado de fabrica. Lo aplica el frontend.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

CONFIG_FILE = BASE_DIR / "data" / "voice_config.json"
ENGINE_NAMES = ("fish", "elevenlabs", "local")
CLOUD_ENGINES = ("fish", "elevenlabs")
MAX_CHAIN_LENGTH = 8
DEFAULT_PAUSE_MINUTES = 30
MIN_PAUSE_MINUTES = 1
MAX_PAUSE_MINUTES = 240

_LOCK = threading.Lock()


class VoiceConfigError(ValueError):
    """Configuracion rechazada; el mensaje ya esta pensado para el usuario."""


@dataclass
class VoiceConfig:
    chain: list[dict[str, str]] = field(default_factory=list)
    fish_voice_id: str = ""
    elevenlabs_voice_id: str = ""
    consent: dict[str, bool] = field(default_factory=dict)
    effect: bool = False
    pause_minutes: int = DEFAULT_PAUSE_MINUTES

    def voice_for(self, engine: str) -> str:
        """El ID elegido en Configuracion o, si no hay, el del `.env`."""
        settings = get_settings()
        if engine == "fish":
            return (self.fish_voice_id or settings.fish_voice_id).strip()
        if engine == "elevenlabs":
            return (self.elevenlabs_voice_id or settings.elevenlabs_voice_id).strip()
        return ""


def load_config() -> VoiceConfig:
    with _LOCK:
        if not CONFIG_FILE.exists():
            return VoiceConfig()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("voice_config.json ilegible: se usa la voz del navegador", exc_info=True)
            return VoiceConfig()
    known = {k: v for k, v in data.items() if k in VoiceConfig.__dataclass_fields__}
    return VoiceConfig(**known)


def save_config(cfg: VoiceConfig) -> None:
    with _LOCK:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_chain(chain: list[Any], models_by_engine: dict[str, tuple[str, ...]]) -> list[dict[str, str]]:
    """Valida la cadena: motores conocidos, modelo del motor (o el primero si
    viene vacio), sin repetidos y con un tope de largo."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in chain:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine", "")).strip()
        if engine not in ENGINE_NAMES:
            raise VoiceConfigError(f"Motor de voz desconocido: «{engine}».")
        models = models_by_engine.get(engine, ())
        model = str(item.get("model", "")).strip() or (models[0] if models else "")
        if models and model not in models:
            raise VoiceConfigError(f"El modelo «{model}» no es de ese motor de voz.")
        if (engine, model) in seen:
            continue
        seen.add((engine, model))
        result.append({"engine": engine, "model": model})
    if len(result) > MAX_CHAIN_LENGTH:
        raise VoiceConfigError(f"La lista admite hasta {MAX_CHAIN_LENGTH} voces.")
    return result


def update_config(changes: dict[str, Any], models_by_engine: dict[str, tuple[str, ...]]) -> VoiceConfig:
    """Aplica los cambios de Configuracion > Audio (solo las claves presentes)."""
    cfg = load_config()
    revoked: set[str] = set()
    if changes.get("consent") is not None:
        for engine, value in dict(changes["consent"]).items():
            if engine in CLOUD_ENGINES:
                cfg.consent[engine] = bool(value)
                if not value:
                    revoked.add(engine)
    if changes.get("chain") is not None:
        cfg.chain = normalize_chain(list(changes["chain"]), models_by_engine)
    elif revoked:
        # Retirar el consentimiento saca ese servicio de la lista.
        cfg.chain = [link for link in cfg.chain if link["engine"] not in revoked]
    for key in ("fish_voice_id", "elevenlabs_voice_id"):
        if changes.get(key) is not None:
            setattr(cfg, key, str(changes[key]).strip())
    if changes.get("effect") is not None:
        cfg.effect = bool(changes["effect"])
    if changes.get("pause_minutes") is not None:
        minutes = int(changes["pause_minutes"])
        if not MIN_PAUSE_MINUTES <= minutes <= MAX_PAUSE_MINUTES:
            raise VoiceConfigError(
                f"Los minutos de pausa tienen que estar entre {MIN_PAUSE_MINUTES} y {MAX_PAUSE_MINUTES}."
            )
        cfg.pause_minutes = minutes
    # REQ-002: un motor en la nube sin consentimiento no entra en la lista.
    missing = sorted({link["engine"] for link in cfg.chain if link["engine"] in CLOUD_ENGINES} - {
        engine for engine, ok in cfg.consent.items() if ok
    })
    if missing:
        raise VoiceConfigError(
            "Antes de usar un servicio de voz en la nube hay que confirmar que el texto que ULTRON lee "
            "se le envía a ese servicio."
        )
    save_config(cfg)
    return cfg
