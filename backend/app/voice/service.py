"""
Recorre la cadena de voces (spec voz-fish-audio REQ-001/003/005/006/007).

Por cada frase: prueba los motores en el orden de Configuracion > Audio,
saltea los que estan en pausa y devuelve el audio del primero que responde.
Un motor que falla queda en pausa (asi las frases siguientes no esperan a un
servicio caido o sin credito) y se arma UN aviso para el chat: que paso, con
que motor sigue Vision y que hacer. Si fallan todos, el frontend habla con la
voz del navegador (REQ-003: nunca mudo).

Las pausas reusan `ModelPauses` del salto automatico de modelos: misma
semantica (en memoria, globales, se limpian al vencer).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.friendly_errors import FriendlyError, describe_voice_engine_error
from app.llm.model_fallback import ModelPauses, hhmm
from app.voice import local_clone
from app.voice.engines import Audio, ElevenLabsEngine, FishAudioEngine, VoiceEngine, VoiceEngineError
from app.voice.settings import CLOUD_ENGINES, VoiceConfig, load_config

logger = logging.getLogger(__name__)

# REQ-001: registro de motores. Sumar uno es agregarlo aca (y a
# `settings.ENGINE_NAMES`), sin tocar a los demas.
ENGINES: dict[str, VoiceEngine] = {
    "fish": FishAudioEngine(),
    "elevenlabs": ElevenLabsEngine(),
    "local": local_clone.LocalCloneEngine(),
}
BROWSER_LABEL = "la voz del navegador"
# El frontend manda frase por frase (REQ-004); un texto mas largo es un error.
MAX_TEXT_CHARS = 1500
# Fallas que se arreglan solas (saturacion, red, error puntual): pausa corta.
# Las que necesitan al usuario (clave, credito, voz, instalacion): la pausa
# configurada, o hasta que se guarde la configuracion de nuevo.
_SHORT_PAUSE_KINDS = frozenset({"limite", "sin_conexion", "fallo"})
SHORT_PAUSE_SECONDS = 120

PAUSES = ModelPauses()


def models_by_engine() -> dict[str, tuple[str, ...]]:
    return {name: engine.models for name, engine in ENGINES.items()}


def link_label(link: dict[str, str]) -> str:
    """Como se nombra un eslabon en los avisos ("Fish Audio (s2.1-pro-free)")."""
    engine = ENGINES[link["engine"]]
    if link["engine"] == "local":
        return "tu voz clonada"
    return f"{engine.label} ({link['model']})"


def _pause_key(link: dict[str, str]) -> str:
    return f"{link['engine']}:{link['model']}"


def active_links(cfg: VoiceConfig) -> list[dict[str, str]]:
    """La cadena sin los servicios en la nube que no tienen consentimiento
    (REQ-002: el texto nunca sale sin que el usuario lo haya confirmado)."""
    return [
        link
        for link in cfg.chain
        if link["engine"] in ENGINES and (link["engine"] not in CLOUD_ENGINES or cfg.consent.get(link["engine"]))
    ]


@dataclass
class SpeakResult:
    audio: Audio
    engine: str
    model: str
    notice: FriendlyError | None


class NoVoiceEngineAvailable(Exception):
    """Ningun motor pudo hablar: el frontend usa la voz del navegador.
    `notice` es None cuando no hubo falla nueva (cadena vacia o todo en pausa)."""

    def __init__(self, notice: FriendlyError | None) -> None:
        super().__init__(notice.code if notice else "sin-motores")
        self.notice = notice


def _notice(failures: list[tuple[dict[str, str], VoiceEngineError]], next_label: str | None) -> FriendlyError | None:
    """Un solo aviso por frase: el de la primera falla (la del motor preferido)."""
    if not failures:
        return None
    link, exc = failures[0]
    return describe_voice_engine_error(exc.kind, ENGINES[link["engine"]].label, next_label, exc.status)


def speak(text: str, only: dict[str, str] | None = None) -> SpeakResult:
    """Audio de UNA frase. `only`: probar un eslabon puntual desde
    Configuracion (sin saltar a otro ni pausar: el usuario quiere oir ese)."""
    cfg = load_config()
    links = [only] if only else active_links(cfg)
    if not links:
        raise NoVoiceEngineAvailable(None)
    failures: list[tuple[dict[str, str], VoiceEngineError]] = []
    for link in links:
        if not only and PAUSES.paused_until(_pause_key(link)) is not None:
            continue
        engine = ENGINES[link["engine"]]
        try:
            audio = engine.synthesize(text, model=link["model"], voice_id=cfg.voice_for(link["engine"]))
        except VoiceEngineError as exc:
            logger.warning("Motor de voz %s fallo [%s]: %s", _pause_key(link), exc.kind, exc.detail[:300])
            if not only:
                seconds = SHORT_PAUSE_SECONDS if exc.kind in _SHORT_PAUSE_KINDS else cfg.pause_minutes * 60
                PAUSES.pause(_pause_key(link), seconds)
            failures.append((link, exc))
            continue
        return SpeakResult(audio, link["engine"], link["model"], _notice(failures, link_label(link)))
    raise NoVoiceEngineAvailable(_notice(failures, None if only else BROWSER_LABEL))


def _link_ready(link: dict[str, str], cfg: VoiceConfig) -> str | None:
    """Que le falta a un eslabon para poder hablar (None si nada)."""
    engine = link["engine"]
    if engine == "local":
        if not local_clone.is_installed():
            return "falta instalar la clonación local"
        if local_clone.sample_path() is None:
            return "falta tu muestra de voz"
        return None
    settings = get_settings()
    key = settings.fish_api_key if engine == "fish" else settings.elevenlabs_api_key
    if not key.strip():
        from app import credentials  # import diferido: credentials importa config y conectores

        return f"falta la clave en {credentials.where_to_load('Voz')}"  # gestor-conexiones: Credenciales o Conexiones
    if not cfg.voice_for(engine):
        return "falta elegir la voz"
    if not cfg.consent.get(engine):
        return "falta confirmar el envío de texto"
    return None


def status_payload(cfg: VoiceConfig | None = None) -> dict[str, Any]:
    """Todo lo que muestra Configuracion > Audio."""
    cfg = cfg or load_config()
    items = []
    for link in cfg.chain:
        until = PAUSES.paused_until(_pause_key(link))
        items.append(
            {
                **link,
                "label": link_label(link),
                "missing": _link_ready(link, cfg),
                "paused_until": hhmm(until) if until else None,
            }
        )
    return {
        "chain": cfg.chain,
        "items": items,
        "fish_voice_id": cfg.voice_for("fish"),
        "elevenlabs_voice_id": cfg.voice_for("elevenlabs"),
        "consent": {engine: bool(cfg.consent.get(engine)) for engine in CLOUD_ENGINES},
        "effect": cfg.effect,
        "pause_minutes": cfg.pause_minutes,
        "engines": [
            {"name": name, "label": engine.label, "cloud": engine.cloud, "models": list(engine.models)}
            for name, engine in ENGINES.items()
        ],
        "local": {**local_clone.install_status(), "sample": local_clone.sample_info()},
    }
