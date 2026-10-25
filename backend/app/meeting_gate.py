"""
Gate heuristico local para el Modo reunion (spec narracion-agente-efimera,
REQ-004/REQ-005). Decide con regex/keywords baratos (SIN invocar al LLM)
si un fragmento de transcripcion contiene una invocacion real a Jarvis/
Vision -- tecnica adoptada de OpenOats (proyecto open-source de
referencia, ver Decisiones registradas en spec.md): el LLM NUNCA decide
"si" responder, solo genera contenido DESPUES de que el gate ya aprobo.
Esto evita invocar al LLM constantemente durante 30-60 min de escucha
continua (latencia + costo).

REQ-004 (revisado 2026-09-07, por pedido explicito del usuario): el
patron ORIGINAL exigia ademas una señal de pregunta/opinion en la misma
frase (ej. "que opinas", "?") -- el usuario reporto que decir "Vision,
que opinas de esto?" tampoco se reconocia (causa real: Whisper a veces
transcribe sin el signo de interrogacion, sobre todo en fragmentos
cortos/con ruido, y el patron de opinion dependia mucho de "?" como señal
principal). El usuario pidio EXPLICITAMENTE maxima sensibilidad: CUALQUIER
mencion de "Vision"/"Jarvis" (mayusculas/minusculas/con o sin tilde) ya
dispara una respuesta, sin exigir mas condiciones. Se acepta el riesgo de
mas falsos positivos (ej. alguien habla de "la vision del negocio" sin
querer invocar al asistente) a cambio de nunca mas perder una invocacion
real -- decision explicita del usuario, no asuncion del agente.

REQ-004: throttle anti-rafaga -- espaciado minimo configurable entre
intervenciones, para evitar que Jarvis opine repetidamente en poco tiempo
si el gate detecta varias invocaciones seguidas (sigue siendo necesario
CON MAS razon ahora que el gate es mas sensible)."""

from __future__ import annotations

import re
import time

# Patron: "jarvis" O "ultron"/"ultrón" en cualquier posicion, en cualquier capitalizacion -- unica condicion para aprobar. Ya NO se
# exige ninguna señal adicional de pregunta/opinion (ver docstring del modulo para el porque de este cambio).
# ULTRON (spec portar-desde-origen REQ-010): se lo llama «Ultron» (o «Jarvis»); ya no responde a «Vision».
_ASSISTANT_MENTION_RE = re.compile(r"\bjarvis\b|\bultr[oó]n\b", re.IGNORECASE)

# REQ-004: throttle anti-rafaga -- espaciado minimo entre intervenciones
# aprobadas por el gate (independiente por sesion, ver MeetingGateState).
_MIN_SECONDS_BETWEEN_INTERVENTIONS = 8.0


class MeetingGateState:
    """Estado minimo por sesion de modo reunion -- solo el timestamp de la
    ultima intervencion aprobada, para el throttle anti-rafaga. Vive en
    memoria del proceso (no se persiste), igual que el resto del estado de
    sesion (SessionState) -- se descarta al salir del modo reunion."""

    def __init__(self) -> None:
        self.last_intervention_at: float = 0.0


def check_invocation(fragment: str, gate_state: MeetingGateState) -> bool:
    """REQ-004/REQ-005: True si `fragment` menciona a Jarvis/Vision Y no
    viola el throttle anti-rafaga. Puro/barato -- sin llamadas de red, sin
    LLM. Debe correr en <1ms para no introducir latencia perceptible sobre
    cada fragmento transcrito en tiempo real."""
    if not fragment or not fragment.strip():
        return False

    if not _ASSISTANT_MENTION_RE.search(fragment):
        return False

    now = time.monotonic()
    if now - gate_state.last_intervention_at < _MIN_SECONDS_BETWEEN_INTERVENTIONS:
        return False  # throttle: ya opino hace muy poco, se ignora esta invocacion

    gate_state.last_intervention_at = now
    return True
