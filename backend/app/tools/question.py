"""
Tool `question`: permite al agente repreguntar al usuario con opciones
cliqueables en vez de solo texto libre, replicando la tool `question` de
OpenCode. Como esta v1 no tiene un mecanismo de "pausar el turno del LLM
y esperar respuesta humana" (eso requeriria un loop de espera bloqueante
tipo `Deferred.await` de OpenCode), la respuesta a esta version simplificada
es: se emite el evento SSE `type: "question"` para que el frontend lo
muestre, y la tool devuelve inmediatamente un texto indicando que la
pregunta fue mostrada -- el usuario respondera en su PROXIMO mensaje de
chat normal, no de forma sincronica dentro del mismo turno.

Nota de diseno (deuda tecnica conocida, aceptable para v1 local /
prototipo personal): un salto de calidad futuro seria bloquear el turno
del LLM hasta que el usuario responda (como hace OpenCode con
`permission.assert`), pero eso requiere reestructurar el loop de
tool-calling para soportar pausas largas -- fuera de alcance de esta
primera version. Marcado EXPLICITAMENTE como reversible en la spec
modo-plan-loop: el usuario puede pedir portarlo mas adelante si el
criterio actual (fire-and-forget) no convence en uso real.

spec modo-plan-loop REQ-008/009: la "opcion recomendada" NO es un campo
de schema nuevo -- es la MISMA convencion textual real de OpenCode
(packages/core/src/tool/question.ts): el LLM pone la mejor opcion PRIMERA
en la lista y le agrega el sufijo "(Recomendada)" al label. Esta tool
detecta ese sufijo (`_find_recommended_index`) para que, SI el loop
autonomo esta corriendo, pueda auto-seleccionarla sin pausar (REQ-009);
si el loop esta activo y NINGUNA opcion trae el sufijo, se pausa con
`session.loop_paused_for_question = True` (dispara la alarma del lado
del frontend)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.tools.base import Tool

logger = logging.getLogger(__name__)

RECOMMENDED_SUFFIX = "(Recomendada)"

# Bug real reportado por el usuario: a veces el modelo (se observo con
# Claude via el proveedor de IA) no manda 'options' como array JSON sino como UN SOLO
# string con una sintaxis pseudo-XML: `<option label="..." description="...">`.
# Si ese string se itera directo como si fuera una lista (`for o in raw_options`),
# Python itera CARACTER POR CARACTER (los strings son iterables) -- cada letra
# se vuelve una "opcion" individual, generando cientos de botones de 1 caracter
# que tapan toda la pantalla (el "teclado gigante" reportado). Este regex
# intenta reparar ese string mal formado extrayendo los pares label/description.
_OPTION_TAG_RE = re.compile(
    r'<option\s+label="(?P<label>[^"]*)"(?:\s+description="(?P<description>[^"]*)")?\s*/?>',
    re.IGNORECASE,
)


def _parse_options_from_malformed_string(raw: str) -> list[dict]:
    """Recuperacion best-effort del caso descrito arriba: si el regex no
    matchea nada (el string no sigue ese patron), se devuelve lista vacia
    -- el fallback seguro es tratarlo como 'sin opciones' (respuesta libre),
    nunca iterar el string caracter por caracter."""
    matches = [
        {"label": m.group("label"), "description": m.group("description") or ""}
        for m in _OPTION_TAG_RE.finditer(raw)
    ]
    if not matches:
        logger.warning(
            "Tool 'question': 'options' llego como string sin formato "
            "reconocible, se descarta (se ofrece respuesta libre): %r",
            raw[:200],
        )
    return matches


def _normalize_option(opt: Any) -> dict:
    """Algunos modelos (se observo con Claude via el proveedor de IA) devuelven items de
    'options' como string plano en vez del objeto {label, description} que
    pide el schema -- se normaliza aca en vez de asumir ciegamente la forma
    exacta del JSON Schema (los modelos no siempre la respetan al 100%)."""
    if isinstance(opt, str):
        return {"label": opt, "description": ""}
    if isinstance(opt, dict):
        return {
            "label": opt.get("label", str(opt)),
            "description": opt.get("description", ""),
        }
    return {"label": str(opt), "description": ""}


def _find_recommended_index(options: list[dict]) -> int | None:
    """REQ-008: busca la opcion con el sufijo "(Recomendada)" en su label
    -- case-insensitive y tolerante a espacios extra, porque el LLM no
    siempre respeta el formato exacto al pie de la letra."""
    for i, opt in enumerate(options):
        if opt["label"].strip().lower().endswith(RECOMMENDED_SUFFIX.lower()):
            return i
    return None


def _question_execute(args: dict, session: Any = None) -> str:
    question_text = args.get("question", "")
    raw_options = args.get("options", [])
    if not question_text:
        return "Error: falta el argumento 'question'."

    # Ver comentario de `_parse_options_from_malformed_string`: NUNCA iterar
    # un string directo ("for o in raw_options") porque itera caracter por
    # caracter -- primero se repara el caso malformado conocido.
    if isinstance(raw_options, str):
        raw_options = _parse_options_from_malformed_string(raw_options)

    options = [_normalize_option(o) for o in (raw_options or [])]
    recommended_idx = _find_recommended_index(options)

    # spec modo-plan-loop REQ-009/011: si el loop autonomo esta corriendo,
    # decide auto-seleccionar (sin pausar ni alarmar) o pausar+alarmar,
    # ANTES de emitir el evento visual normal -- el frontend igual
    # muestra la pregunta (transparencia de que paso), pero el backend ya
    # resolvio si hace falta intervencion humana real.
    #
    # REQ-011 (elegido por el usuario = "Ambas", mas defensivo): el loop
    # auto-selecciona SOLO cuando hay una opcion "(Recomendada)" y ninguna
    # indica riesgo. PAUSA + alarma en cualquiera de: alguna opcion trae el
    # marcador de riesgo "(Riesgo)" (posible dano a archivos de la compania /
    # consecuencia importante), o NO hay ninguna "(Recomendada)" (decision
    # ambigua). Aplica igual en modo normal y en modo auto (`loop_auto`);
    # el modo auto no baja estas barreras de seguridad.
    loop_auto_answer: str | None = None
    has_risk = any("(riesgo)" in o["label"].lower() for o in options)
    if session is not None and getattr(session, "loop_active", False):
        if has_risk or recommended_idx is None:
            # Red de seguridad: riesgo explicito, o sin opcion claramente
            # recomendada -> NO se auto-ejecuta, se pausa con alarma.
            session.loop_paused_for_question = True
            session.push_event(
                "loop-paused", {"question": question_text, "options": options}
            )
        else:
            loop_auto_answer = options[recommended_idx]["label"]

    if session is not None:
        session.push_event(
            "question",
            {
                "question": question_text,
                "options": options,
                "recommended_index": recommended_idx,
                "auto_answered": loop_auto_answer,
            },
        )

    opciones_txt = (
        ", ".join(o["label"] for o in options) if options else "(respuesta libre)"
    )

    if loop_auto_answer:
        return (
            f"Pregunta '{question_text}' (opciones: {opciones_txt}). El loop autonomo "
            f"auto-selecciono la opcion recomendada: '{loop_auto_answer}'. Continua con esa "
            "eleccion sin esperar confirmacion humana."
        )

    return (
        f"Pregunta mostrada al usuario: '{question_text}' (opciones: {opciones_txt}). "
        "Esperando que el usuario responda en su proximo mensaje."
    )


question_tool = Tool(
    name="question",
    description=(
        "Use this tool when you need to ask the user questions during execution. This allows you to:\n"
        "1. Gather user preferences or requirements\n"
        "2. Clarify ambiguous instructions\n"
        "3. Get decisions on implementation choices as you work\n"
        "4. Offer choices to the user about what direction to take.\n\n"
        "Usage notes:\n"
        "- Answers are returned as the label of the option chosen (or free text if the user types "
        "their own answer)\n"
        "- If you recommend a specific option, make that the first option in the list and add "
        '"(Recomendada)" at the end of the label -- this lets an autonomous /loop pick it '
        "automatically without waiting for a human, instead of pausing and sounding an alarm.\n"
        "- If a decision could DAMAGE COMPANY FILES, break something important, or involves "
        'significant risk, add "(Riesgo)" to the end of the relevant option label(s) -- this makes '
        "the loop PAUSE and sound an alarm for a human, even in auto mode, instead of proceeding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["question"],
    },
    execute=_question_execute,
    # Toca session.push_event() (asyncio.Queue) -- ver comentario de
    # Tool.thread_safe en app/tools/base.py.
    thread_safe=False,
)
