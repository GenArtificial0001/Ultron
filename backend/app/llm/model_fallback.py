"""
Salto automatico de modelos (spec `salto-automatico-modelos`, APPROVED 2026-09-25).

Por que existe: el proveedor de IA limita el GASTO por usuario y POR MODELO (429 "Too much
spend: model X is limited to $N per 600 seconds"). Antes, cuando un modelo
llegaba a su tope el chat se cortaba y habia que cambiar de modelo a mano (y
acordarse de volver). Ahora, si el usuario lo activa, Vision sigue con el
siguiente modelo de SU lista de prioridad (Configuracion > Modelo e
inteligencia) y, pasada la pausa, vuelve a probar el anterior.

Piezas:
- `ModelPauses` (REQ-003): pausas por modelo, globales a todas las sesiones (la
  cuota es del usuario, no de la conversacion) y en memoria (al reiniciar
  Vision se pierden: el primer intento vuelve a medir, a costo de una llamada).
- `TurnModelPicker` (REQ-002/003/004/009): elige el modelo de CADA paso de un
  turno y arma los avisos del chat. Logica pura: el loop (`llm/loop.py`) solo
  le pregunta que modelo usar y le cuenta cuando uno fallo.
- El historial compatible con el modelo destino (REQ-005) vive en
  `llm/history_compat.py`.
- `check_now` (REQ-010): la vuelta se ve cuando vence la pausa, no recien con
  el proximo mensaje -- el frontend pregunta a la hora que informa `resume_at`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.friendly_errors import (
    FriendlyError,
    describe_all_models_paused,
    describe_model_error,
    describe_task_needs_new_message,
    model_recovered_notice,
    model_switch_notice,
)

# spec varios-proveedores REQ-005: la lista arranca VACIA; el usuario la arma
# con modelos de cualquier proveedor ("proveedor/modelo").
DEFAULT_ORDER: tuple[str, ...] = ()
DEFAULT_PAUSE_MINUTES = 10
MAX_ORDER_LENGTH = 12
MIN_PAUSE_MINUTES = 1
MAX_PAUSE_MINUTES = 120

# REQ-002/REQ-008: disparan el salto los limites (cualquier 429: tope de gasto
# por modelo o del usuario, o demasiados pedidos) y la caida del servicio (5xx)
# -- decision del usuario "limite, caida, solo errores"; la prueba real del
# 2026-09-25 mostro que un 429 que no se reconocia como "tope" no saltaba. Un
# rechazo 400 o una demora sin codigo NO: suelen ser problemas del pedido que el
# otro modelo tambien tendria, y saltar los esconderia.
# spec varios-proveedores REQ-005: sin credito tambien salta (a otro proveedor:
# ver register_failure).
TRIGGER_CODES = frozenset(
    {"MODELO-LIMITE", "MODELO-LIMITE-USUARIO", "MODELO-OCUPADO", "MODELO-CAIDO", "MODELO-SIN-CREDITO"}
)


def _is_local(model_id: str) -> bool:
    from app.llm.providers import is_local  # import diferido: evita ciclo

    return is_local(model_id)


def is_gemini(model_id: str) -> bool:
    # spec varios-proveedores: el id puede venir con prefijo de proveedor
    # ("gemini/gemini-2.5-flash", "openrouter/google/gemini-..."): se mira el nombre.
    return model_id.rsplit("/", 1)[-1].lower().startswith("gemini")


def resolve(model: str, catalog: dict[str, dict[str, Any]] | None) -> tuple[str, dict[str, Any] | None]:
    """(id real, entrada del catalogo). Los alias ("claude-opus-latest") se
    resuelven al modelo al que apuntan HOY. Sin catalogo (el proveedor de IA no respondio
    /models_metadata) se usa el nombre tal cual."""
    entry = (catalog or {}).get(model)
    if entry is None:
        return model, None
    return str(entry.get("id") or model), entry


def _shown(model_id: str) -> str:
    """spec varios-proveedores: nombre legible para los avisos ("gemini-3.8-flash (Gemini)")."""
    from app.llm.providers import display_name  # import diferido: evita ciclo

    return display_name(model_id)


def hhmm(epoch: float) -> str:
    """Hora local "15:10" (la que ve el usuario en el reloj de Windows)."""
    return time.strftime("%H:%M", time.localtime(epoch))


@dataclass(frozen=True)
class FallbackSettings:
    enabled: bool = False
    order: tuple[str, ...] = DEFAULT_ORDER
    pause_minutes: int = DEFAULT_PAUSE_MINUTES


def load_settings() -> FallbackSettings:
    """Lee la configuracion guardada desde Configuracion (REQ-001)."""
    from app.project_context import get_project_config  # import diferido: evita ciclo

    from app.llm.providers import canonical  # import diferido: evita ciclo

    cfg = get_project_config()
    return FallbackSettings(
        enabled=bool(cfg.model_fallback_enabled),
        # spec varios-proveedores REQ-007: una lista vieja, sin prefijo, se lee
        # como del proveedor principal.
        order=tuple(canonical(m) for m in (cfg.model_fallback_order or DEFAULT_ORDER)),
        pause_minutes=int(cfg.model_fallback_pause_minutes or DEFAULT_PAUSE_MINUTES),
    )


class ModelPauses:
    """REQ-003: `modelo real -> pausado hasta` (epoch). Thread-safe: la leen el
    loop de cada sesion y el endpoint de estado de Configuracion."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}
        self._recovered: set[str] = set()
        self._lock = threading.Lock()

    def _now(self) -> float:
        return (self._clock or time.time)()

    def pause(self, model: str, seconds: float) -> float:
        with self._lock:
            until = self._now() + seconds
            self._until[model] = until
            self._recovered.discard(model)
            return until

    def paused_until(self, model: str) -> float | None:
        """Hasta cuando esta en pausa, o None si esta disponible. Una pausa
        vencida se limpia aca y el modelo queda marcado para avisar UNA vez
        que se vuelve a usar (`pop_recovered`)."""
        with self._lock:
            until = self._until.get(model)
            if until is None:
                return None
            if until <= self._now():
                del self._until[model]
                self._recovered.add(model)
                return None
            return until

    def pop_recovered(self, model: str) -> bool:
        with self._lock:
            if model in self._recovered:
                self._recovered.discard(model)
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._until.clear()
            self._recovered.clear()


PAUSES = ModelPauses()


@dataclass(frozen=True)
class Choice:
    """Modelo para el proximo paso; o un error si no queda ninguno usable."""

    model: str | None
    notice: str | None = None
    error: FriendlyError | None = None


class TurnModelPicker:
    """Elige el modelo de cada paso de UN turno.

    Candidatos, en orden: el modelo "de casa" (el elegido a mano en el selector,
    o el primero de la lista en modo Automatico) y despues la lista del usuario,
    sin repetir el mismo modelo real. Se usa el primero que no este en pausa ni
    haya desaparecido del catalogo (REQ-001/002/003)."""

    def __init__(
        self,
        settings: FallbackSettings,
        selected: str | None,
        default_model: str,
        catalog: dict[str, dict[str, Any]] | None,
        pauses: ModelPauses | None = None,
    ) -> None:
        self._catalog = catalog or {}
        self._pauses = pauses or PAUSES
        self._pause_seconds = max(MIN_PAUSE_MINUTES, settings.pause_minutes) * 60
        home = selected or (settings.order[0] if settings.order else default_model)
        self._candidates: list[tuple[str, dict[str, Any] | None]] = []
        seen: set[str] = set()
        for name in (home, *settings.order):
            real, entry = resolve(name, self._catalog)
            if real not in seen:
                seen.add(real)
                self._candidates.append((real, entry))
        self._last_model: str | None = None  # modelo que produjo el ultimo paso OK del turno
        self._pending_failure: tuple[str, str, float] | None = None

    @property
    def home(self) -> str:
        return self._candidates[0][0]

    def _is_missing(self, entry: dict[str, Any] | None) -> bool:
        # Con catalogo cargado, un nombre que no esta ya no existe en el proveedor de IA
        # (REQ-001: se saltea). Sin catalogo no se sabe: se intenta igual.
        return bool(self._catalog) and entry is None

    def _secret_ok(self, model: str) -> bool | None:
        _, entry = resolve(model, self._catalog)
        if entry is None or "secret_data_compliant" not in entry:
            return None
        return bool(entry["secret_data_compliant"])

    def choose(self, *, task_started: bool) -> Choice:
        """`task_started`: el turno ya ejecuto herramientas (REQ-009)."""
        skipped_gemini: str | None = None
        earliest: tuple[float, str] | None = None
        for model, entry in self._candidates:
            if self._is_missing(entry):
                continue
            until = self._pauses.paused_until(model)
            if until is not None:
                if earliest is None or until < earliest[0]:
                    earliest = (until, model)
                continue
            # REQ-009: Gemini exige su firma de pensamiento en las tool-calls del
            # turno en curso; las que genero otro modelo no la tienen.
            if task_started and is_gemini(model) and self._last_model and not is_gemini(self._last_model):
                skipped_gemini = skipped_gemini or model
                continue
            return Choice(model=model, notice=self._notice_for(model))
        if skipped_gemini is not None:
            return Choice(model=None, error=describe_task_needs_new_message(_shown(skipped_gemini)))
        if earliest is not None:
            return Choice(model=None, error=describe_all_models_paused(_shown(earliest[1]), hhmm(earliest[0])))
        # Nada usable (lista vacia o todos los nombres desaparecieron del
        # catalogo): se intenta con el de casa, como sin salto.
        return Choice(model=self.home)

    def _notice_for(self, model: str) -> str | None:
        if self._pending_failure is not None:
            failed, code, until = self._pending_failure
            self._pending_failure = None
            self._pauses.pop_recovered(model)
            # REQ-007: se salta igual, pero se avisa si el destino no es apto
            # para datos secretos y el que fallo si lo era.
            not_secret = self._secret_ok(failed) is not False and self._secret_ok(model) is False
            return model_switch_notice(_shown(failed), code, _shown(model), hhmm(until), next_not_secret=not_secret)
        if self._pauses.pop_recovered(model):
            return model_recovered_notice(_shown(model))
        return None

    def mark_used(self, model: str) -> None:
        """El modelo completo un paso: queda como el que viene trabajando."""
        self._last_model = model

    def register_failure(self, model: str, exc: BaseException) -> bool:
        """REQ-002/008: True si la falla dispara el salto; en ese caso el modelo
        queda en pausa y el proximo `choose` elige otro y arma el aviso."""
        friendly = describe_model_error(exc, model)
        # spec ia-local REQ-004: un motor LOCAL apagado (sin conexion) es una caida de ese modelo: se pausa y el turno sigue con el
        # siguiente de la lista. Para los de la nube, «sin conexion» sigue sin saltar (suele ser la red de la PC, no el modelo).
        local_down = friendly.code == "MODELO-SIN-CONEXION" and _is_local(model)
        if friendly.code not in TRIGGER_CODES and not local_down:
            return False
        until = self._pauses.pause(model, self._pause_seconds)
        if friendly.code == "MODELO-LIMITE-USUARIO" and self._catalog:
            # El tope es del USUARIO y vale para todos los modelos pagos: probar
            # otro pago solo sumaria esperas y fallas. Se pausan todos los pagos
            # de la lista y el salto va directo a uno gratuito (on-prem), como
            # sugiere el propio mensaje del proveedor de IA.
            for candidate, entry in self._candidates:
                if entry is not None and not entry.get("hosted_on_prem"):
                    self._pauses.pause(candidate, self._pause_seconds)
        if friendly.code == "MODELO-SIN-CREDITO":
            # spec varios-proveedores REQ-005: sin credito, ningun modelo de ESE
            # proveedor va a responder: se pausan todos y el salto va a otro.
            from app.llm.providers import provider_of  # import diferido: evita ciclo

            owner = provider_of(model)
            for candidate, _entry in self._candidates:
                if provider_of(candidate) == owner:
                    self._pauses.pause(candidate, self._pause_seconds)
        self._pending_failure = (model, friendly.code, until)
        return True

    def first_available(self) -> str:
        """Modelo para tareas auxiliares del turno (ej. compactar el historial):
        el primero disponible de la lista, sin avisos ni efectos sobre el turno."""
        for model, entry in self._candidates:
            if not self._is_missing(entry) and self._pauses.paused_until(model) is None:
                return model
        return self.home

    def resume_at(self) -> float | None:
        """REQ-010: cuando vence la proxima pausa de la lista (epoch), o None si
        no hay ninguna. A esa hora el frontend pregunta de nuevo (`check_now`)."""
        pending = [
            until
            for model, entry in self._candidates
            if not self._is_missing(entry) and (until := self._pauses.paused_until(model)) is not None
        ]
        return min(pending, default=None)


def check_now(
    settings: FallbackSettings,
    selected: str | None,
    default_model: str,
    catalog: dict[str, dict[str, Any]] | None,
    pauses: ModelPauses | None = None,
) -> dict[str, Any]:
    """REQ-010: que pasaria si el usuario mandara un mensaje AHORA, para el chip
    del chat: el modelo que lo responderia (None si todos estan en pausa), el
    aviso de vuelta si su pausa acaba de vencer y cuando vence la proxima pausa.

    Bug real 2026-09-26 (prueba del usuario): la pausa vencia a las 10:58 pero
    el chip y el aviso "Ya pasó la pausa de..." recien aparecian al escribir de
    nuevo (11:02), porque las pausas solo se miraban al arrancar un turno.

    Es la misma eleccion que el primer paso de un turno (`choose`), asi que el
    chip nunca contradice lo que va a pasar. El aviso de vuelta se da UNA sola
    vez: si lo muestra este chequeo, el proximo turno ya no lo repite."""
    picker = TurnModelPicker(settings, selected, default_model, catalog, pauses)
    choice = picker.choose(task_started=False)
    return {
        "model": choice.model,
        "home": picker.home,
        "text": choice.notice,
        "resume_at": picker.resume_at(),
    }


def status_items(
    settings: FallbackSettings, catalog: dict[str, dict[str, Any]] | None, pauses: ModelPauses | None = None
) -> list[dict[str, Any]]:
    """Estado de cada modelo de la lista, para Configuracion (REQ-001/003)."""
    pauses = pauses or PAUSES
    items = []
    for name in settings.order:
        real, entry = resolve(name, catalog)
        missing = bool(catalog) and entry is None
        until = None if missing else pauses.paused_until(real)
        items.append(
            {
                "model": name,
                "label": _shown(name),
                "resolved": None if missing else real,
                "missing": missing,
                "paused_until": hhmm(until) if until else None,
                "secret_data_compliant": None if entry is None else entry.get("secret_data_compliant"),
            }
        )
    return items


def catalog_choices(catalog: dict[str, dict[str, Any]] | None) -> list[dict[str, str]]:
    """Opciones para agregar a la lista: primero los alias que siguen al modelo
    nuevo de cada familia (`*-latest`, `*flagship*`), despues cada modelo."""
    entries = {str(e.get("id")): e for e in (catalog or {}).values() if e.get("id")}
    aliases: list[dict[str, str]] = []
    models: list[dict[str, str]] = []
    for model_id in sorted(entries):
        entry = entries[model_id]
        # spec varios-proveedores: "Gemini · gemini-2.5-flash (gratis)".
        label = model_id
        if entry.get("provider_label") and entry.get("name"):
            label = f"{entry['provider_label']} · {entry['name']}" + (" (gratis)" if entry.get("free") else "")
        models.append({"value": model_id, "label": label, "resolved": model_id})
        for alias in entry.get("aliases") or []:
            if "latest" in alias or "flagship" in alias:
                aliases.append({"value": alias, "label": f"{alias} → hoy {model_id}", "resolved": model_id})
    return sorted(aliases, key=lambda a: a["value"]) + models


def normalize_order(order: list[Any]) -> list[str]:
    """Limpia la lista que manda la UI: sin vacios ni repetidos, con tope."""
    clean: list[str] = []
    for item in order:
        name = str(item or "").strip()
        if name and name not in clean:
            clean.append(name)
    return clean[:MAX_ORDER_LENGTH]
