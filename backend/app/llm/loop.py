"""
Loop de tool-calling del agente Vision, inspirado en el diseno de
`runTurnAttempt`/`run()` de OpenCode
(packages/core/src/session/runner/llm.ts) pero reescrito en Python puro
con `asyncio` en vez de Effect fibers:

    1. Arma el system prompt (incluye el bloque <available_skills>).
    2. Llama al LLM en modo streaming, pasando las tool `definitions`.
    3. Por cada chunk del stream:
        - Si trae contenido de texto -> emite evento SSE 'text-delta'.
        - Si trae fragmentos de tool_calls -> los acumula (llegan
          particionados: primero el id+name, despues fragmentos del
          string de argumentos JSON).
    4. Al terminar el stream: si hubo tool_calls, las ejecuta TODAS EN
       PARALELO (asyncio.gather, equivalente a los fibers concurrentes de
       OpenCode), emite un evento 'tool-call' y 'tool-result' por cada
       una, agrega los mensajes de rol 'assistant' (con tool_calls) y
       'tool' (con el resultado) al historial, y vuelve a llamar al LLM
       (continuacion) -- repite mientras el modelo siga pidiendo tools.
    5. Si no hubo tool_calls (finish_reason == 'stop'), termina el turno.

Limite de seguridad: `MAX_STEPS` evita loops infinitos si el modelo no
deja de llamar tools (equivalente a `runner/max-steps.ts` de OpenCode).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Any

import openai

from app import attachments as attachments_module
from app.browser_workspace import (
    find_index_file,
    get_browser_dir,
    get_or_create_browser_dir,
)
from app.config import get_settings
from app.friendly_errors import FriendlyError, describe_model_error, describe_step_limit
from app.llm import model_fallback
from app.llm import providers as llm_providers
from app.llm.client import get_client
from app.llm.diagnostics import describe_history_shape
from app.llm.history_compat import history_for_model
from app.perf_log import TurnPerfRecorder
from app.persistence import service as persistence
from app.project_context import build_project_context_prompt
from app.session.events import SessionState
from app.tech_report import LEVEL_DETAILED, LiveNarrator, describe_call, normalize_level
from app.token_estimate import estimate_tokens
from app.tool_timings import describe_operation, get_budget_ms, record_duration
from app.tools import get_registry
from app.tools.base import CURRENT_TOOL_CALL_ID
from app.tools.skill import list_available_skills_xml

logger = logging.getLogger(__name__)

MAX_STEPS = 100
WARN_STEPS = 20

# Detener TAJANTE (pedido del usuario 2026-10-05, «el boton Detener se queda pegado sin detener»): tras un Detener real las tools en curso tienen
# este plazo (segundos) para cerrar limpio; pasado el plazo el bucle DEJA DE ESPERARLAS. Solo bash/glob/grep cortan su trabajo al ver `stop_requested`;
# una consulta a Oracle/SAP/SharePoint/BigQuery o un webfetch es una llamada bloqueante en un hilo que nadie puede interrumpir. El hilo termina solo
# (no se lo mata: no hay forma segura en Python) y su resultado se descarta. Una EDICION del mensaje NO usa esto: esa espera a las tools (REQ-004).
STOP_GRACE_S = 2.0
_STOP_POLL_S = 0.1
_STOPPED_TOOL_RESULT = (
    "[detenido por el usuario] Se dejo de esperar esta herramienta al presionar Detener. "
    "Si ya habia empezado en el sistema externo puede terminar sola, pero su resultado no se usa."
)

# Bug real reportado por el usuario (2026-09-21, sesion "no ejecute la
# busqueda" con NGAVS): el modelo puede terminar un paso emitiendo texto que
# ANUNCIA una accion ("Ahora si cargo el plan...", "lanzo la busqueda en
# Microsoft Graph...") sin llamar a NINGUNA tool real -- antes esto se
# aceptaba como turno terminado normal (`if not tool_calls_acc: ... yield
# done`), asi que el usuario veia una cadena de 10+ mensajes disculpandose
# y prometiendo ejecutar, sin que nada forzara la ejecucion real. Limite de
# reintentos automaticos (ver bloque "promesa vacia" mas abajo) antes de
# rendirse y avisarle al usuario en vez de reintentar para siempre.
_MAX_EMPTY_PROMISE_RETRIES = 2


# REGLA N°1 HARD (pedido del usuario 2026-09): tools de CONECTOR/ACCION que
# NO deben ejecutarse si todavia no hay un plan de tareas cargado (se fuerza a
# planificar primero con todowrite -- ver _dispatch_or_intercept).
# REQ-002 (spec clasificador-tipo-respuesta): detecta/remueve el marcador
# `[[TIPO: xxx]]` de un chunk de streaming.
_TYPE_MARKER_FIND = re.compile(r"\[\[TIPO:\s*([a-z_]+)\s*\]\]")

# Umbral generoso: "[[TIPO: analitica_simple]]" completo son 27 caracteres --
# 40 deja margen sin arriesgarse a retener texto real de mas indefinidamente
# si el modelo nunca cierra un "[[" suelto.
_MARKER_HOLDBACK_THRESHOLD = 40
_MARKER_PREFIX = "[[TIPO:"


def _filter_marker_chunk(carry: str, chunk_text: str) -> tuple[str, str, str | None]:
    """Filtra el marcador `[[TIPO: xxx]]` de un chunk de streaming.

    BUG real corregido (deteccion QA 2026-09-14, 2 rondas): el esquema viejo
    bufferizaba el marcador SOLO una vez al arrancar el turno; si el modelo
    lo re-emitia mas adelante (pasa seguido en turnos con 2+ vueltas de
    tool-calling, o directamente en medio de la respuesta) el marcador se
    colaba crudo en el chat.

    Primera vuelta de este fix: bufferizar en TODOS los chunks, no solo el
    primero -- insuficiente por si sola. Se detecto con logging real contra
    el proveedor de IA que el streaming a veces manda los DOS corchetes de apertura en
    chunks separados (un chunk con solo `"["`, el siguiente con otro `"["`) --
    la deteccion vieja de "hay un `[[` en el buffer" nunca lo encontraba
    porque en ningun momento un chunk individual acumulaba las dos `[`
    juntas antes de chequear. Ahora se retiene el buffer si termina con
    CUALQUIER prefijo parcial de `"[[TIPO:"` (aunque sea un solo `"["`),
    ademas del caso original (ya tiene el prefijo completo pero el label+`]]`
    todavia no cerro). Devuelve
    (texto_seguro_para_emitir, nuevo_carry, tipo_encontrado_o_None).
    """
    buf = carry + chunk_text
    match = _TYPE_MARKER_FIND.search(buf)
    found_type = match.group(1) if match else None
    buf = _TYPE_MARKER_FIND.sub("", buf)

    # Caso 1: el buffer YA tiene el prefijo fijo completo pero el label+"]]"
    # todavia no cerro -- sigue bufferizando desde ahi.
    fixed_idx = buf.rfind(_MARKER_PREFIX)
    if fixed_idx != -1 and "]]" not in buf[fixed_idx:] and len(buf) - fixed_idx < _MARKER_HOLDBACK_THRESHOLD:
        return buf[:fixed_idx], buf[fixed_idx:], found_type

    # Caso 2: el buffer TERMINA con un prefijo PARCIAL de "[[TIPO:" (desde un
    # solo "[" hasta "[[TIPO" incompleto) -- retenerlo por si el resto llega
    # en el proximo chunk.
    max_check = min(len(buf), len(_MARKER_PREFIX) - 1)
    for length in range(max_check, 0, -1):
        if buf.endswith(_MARKER_PREFIX[:length]):
            return buf[:-length], buf[-length:], found_type

    return buf, "", found_type


_PLAN_FIRST_TOOLS: frozenset[str] = frozenset(
    {
        "oracle_query",
        "bigquery_query",
        "bigquery_list_tables",
        "sharepoint_list_items",
        "sharepoint_list_lists",
        "sap_query",
        "graph_search",
        "graph_directory_lookup",
        "graph_directory_manager",
        "webfetch",
        "bash",
        "write",
        "edit",
    }
)


def _model_error_event(
    exc: BaseException, model: str, messages: list[dict[str, Any]], log_message: str
) -> dict[str, Any]:
    """Evento SSE `error` para una falla al llamar al modelo, en lenguaje de
    persona no tecnica (pedido del usuario 2026-09-24, ver
    app/friendly_errors.py). Llamar SIEMPRE desde dentro del `except`: el
    traceback completo va al log con la misma referencia (codigo + hora) que
    el usuario ve en "Detalle tecnico", asi soporte encuentra la entrada.

    Ante un 400 ademas registra la FORMA del historial enviado (sin
    contenido, ver app/llm/diagnostics.py): caso real 2026-09-24, Gemini
    rechazo con 400 un historial que solo existia en memoria y el disparador
    no se pudo reconstruir despues."""
    friendly = describe_model_error(exc, model)
    when = datetime.now().astimezone().strftime("%H:%M:%S")
    logger.exception("%s [%s · modelo %s · %s]", log_message, friendly.code, model, when)
    if friendly.status == 400:
        shape, anomalies = describe_history_shape(messages)
        logger.warning(
            "Diagnostico del rechazo 400 [%s · modelo %s · %s]: %d mensajes: %s | anomalias: %s",
            friendly.code,
            model,
            when,
            len(messages),
            shape,
            "; ".join(anomalies) or "ninguna detectada",
        )
    return {
        "type": "error",
        "data": {
            "message": friendly.message,
            "code": friendly.code,
            "technical": friendly.technical(model=model, when=when),
        },
    }


def _friendly_event(friendly: FriendlyError, model: str | None = None) -> dict[str, Any]:
    """Evento SSE `error` para un FriendlyError que no viene de una excepcion
    (ej. spec salto-automatico-modelos REQ-004: todos los modelos en pausa)."""
    when = datetime.now().astimezone().strftime("%H:%M:%S")
    logger.warning(
        "Turno sin modelo disponible [%s · %s]: %s",
        friendly.code,
        when,
        friendly.message,
    )
    return {
        "type": "error",
        "data": {
            "message": friendly.message,
            "code": friendly.code,
            "technical": friendly.technical(model=model, when=when),
        },
    }


def _extra_params_for(
    model: str,
    generation_params: dict[str, Any] | None,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """generation_params filtrados contra los `valid_args` REALES de `model`
    (cada modelo acepta un set distinto; ver `_filter_generation_params`). Sin
    datos del modelo en el catalogo no se manda ninguno. Se recalcula cada vez
    que el salto automatico cambia de modelo (spec salto-automatico-modelos)."""
    entry = catalog.get(model)
    valid_args = entry.get("valid_args", []) if entry else []
    return _filter_generation_params(generation_params, valid_args) if valid_args else {}


_SENTINEL = object()

SYSTEM_PROMPT_TEMPLATE = """Sos Vision, un asistente superinteligente{company_clause}. \
Tenes acceso a herramientas reales: podes leer/escribir \
archivos, ejecutar comandos, consultar Oracle/BigQuery/SharePoint/SAP, armar listas de \
tareas, y cargar 'skills' (guias especializadas) bajo demanda.

Reglas:
- **CLASIFICACION OBLIGATORIA (spec clasificador-tipo-respuesta): tu respuesta SIEMPRE debe \
empezar con un marcador de clasificacion**, ANTES de cualquier otro texto, con el formato exacto \
`[[TIPO: <etiqueta>]]` donde `<etiqueta>` es UNA de estas 3 (en minusculas, sin tildes):
  - `conversacional`: saludo, charla, agradecimiento, o cualquier intercambio social sin pedido \
real de trabajo. Ej: "Hola, como estas", "Gracias!", "Que buen dia".
  - `analitica_simple`: un pedido concreto que se resuelve con una respuesta directa o, a lo sumo, \
UNA sola consulta/tool, sin necesitar un plan de varios pasos. Ej: "¿Que hora es en Argentina?", \
"Decime la capital de Francia".
  - `planificable`: cualquier pedido con 2+ pasos reales (ver REGLA N°1 mas abajo): instalar, \
investigar, conectar a un conector y analizar datos, armar un dashboard, auditar, migrar, etc. \
Ej: "Busca en BigQuery cuantos contenedores recibieron el viernes", "Instala y configura X". \
**Si un mensaje MEZCLA saludo y pedido real (ej. "Hola! Che, necesito que me arms un dashboard \
con 3 graficos"), SIEMPRE gana `planificable`.**
  El marcador se remueve automaticamente antes de que el usuario vea tu respuesta -- es invisible \
para el, se usa solo para mostrar un badge visual. NUNCA lo omitas, y NUNCA lo pongas en medio del \
texto (siempre el primer token, antes de saludar o responder nada).
- **REGLA N°1 (pedido explicito del usuario, alcance ajustado 2026-09 por rendimiento): TODO \
pedido `planificable` DEBE armar primero la lista de tareas con `todowrite` y NUNCA ejecutar \
antes de planificar.** Para vos, "proceso con pasos" incluye (pero no se limita a): analizar \
tablas, buscar datos, investigar, instalar/configurar/probar, armar dashboards, analizar datos, \
auditar, migrar, debugging. Ejemplo: si te piden "busca en BigQuery las tendencias de \
contenedores del ultimo mes y arma un resumen por semana", el plan inicial es: (1) conectarte a \
BigQuery, (2) ubicar y analizar las tablas de contenedores, (3) consultar y agrupar por semana, \
(4) resumir la respuesta. NO consultes ni conectes NADA antes de haber cargado ese plan con \
`todowrite`. Tu PRIMERA tool-call de un pedido `planificable` es SIEMPRE `todowrite`. Prohibido \
adelantarte a la ejecucion. **EXCEPCION (pedido explicito del usuario, "sacar planificar primero \
solo para consultas simples"): si el pedido ya se clasifico como `analitica_simple` (a lo sumo \
UNA sola consulta/tool, ver clasificacion de arriba) o `conversacional`, esta regla NO aplica -- \
ejecuta esa unica consulta/tool DIRECTO, sin pasar por `todowrite` antes.** Si a mitad de \
resolverlo te das cuenta de que hacen falta VARIAS consultas/pasos reales (dejo de ser \
"a lo sumo una"), ahi si cargá el plan antes de seguir -- la excepcion es solo para el caso \
genuinamente simple de un solo paso.
- Actua con inteligencia colaborativa: si una tarea ya se resolvio antes (via una skill \
o un patron conocido), reusalo en vez de reinventar.
- Usa la tool `todowrite` SIEMPRE que el pedido tenga 2 o mas pasos reales (no solo para \
tareas "complejas"): un saludo o una pregunta de una sola respuesta directa no la necesitan, \
pero resolver varios ejercicios, armar un dashboard con varias partes, instalar+configurar+ \
probar algo, investigar algo, o ejecutar cualquier procedimiento con etapas identificables, SI. \
Esto vale para TODO TIPO de proceso, no solo busquedas o investigaciones: instalaciones, \
migraciones, debugging, analisis de datos, construccion de features, auditorias, cualquier \
tarea que siga una secuencia de pasos reales. Esto tambien te ayuda a vos a organizarte, no \
solo a informar al usuario.
- **REGLA DURA DE ORDEN: PRIMERO EL PLAN, DESPUES EJECUTA.** Pedido del usuario (2026-09):
  "antes de ejecutar tiene que planificar las tareas, no deben adelantarse nunca". En el PRIMER
  turno de un pedido planificable (2+ pasos), tu PRIMERA tool-call debe ser `todowrite` cargando
  el plan COMPLETO (todos en `pending` salvo el primero). Queda PROHIBIDO ejecutar NINGUNA tool de
  accion (write/edit/bash/glob/grep/oracle_query/bigquery_query/sharepoint/sap/graph/webfetch/
  etc.) ANTES de tener el plan cargado. Nunca te adelantes: no corras scripts, no crees archivos,
  no consultes datos, no llames conexiones, si la lista de tareas todavia no refleja ese paso.
  El desglose debe existir y estar completo ANTES de arrancar a ejecutar el primer paso.
- **Arma el plan ANTES de ejecutar, no despues.** Antes de arrancar a trabajar, pensa la \
secuencia real de pasos que vas a seguir y cargala completa con `todowrite` (todos en estado \
`pending` salvo el primero). A medida que avanzas de verdad, anda actualizando el estado de \
cada paso (`pending` -> `in_progress` -> `completed`) en el momento en que ocurre, no todos \
juntos al final ni todos de una al principio. Si un paso no aplica o cambia el plan sobre la \
marcha, actualiza la lista (agrega, quita o reordena items) en vez de dejarla desactualizada.
- **El desglose tiene que reflejar el proceso REAL, no un resumen generico.** Nunca uses un \
item unico tipo "hacer la tarea" o "buscar informacion": desglosa los pasos concretos y \
verificables de ESE proceso puntual. Ejemplos: si son varias busquedas progresivas, un item \
por cada busqueda real ejecutada (no una sola entrada "buscar"); si es una instalacion, un \
item por cada componente/dependencia real instalada; si segui el procedimiento de una skill \
cargada (ver <available_skills> y regla de skills mas abajo), usa las etapas concretas que esa \
skill documenta como base del desglose (ej. definir alcance, resolver identidad, cada busqueda \
progresiva, armar matriz de evidencia, redactar respuesta -- para la skill de investigacion \
interna; o los pasos equivalentes que documente cualquier otra skill activa).
- Cuando una tarea principal tiene partes identificables (cada ejercicio de una actividad, cada \
seccion de un dashboard, cada paso de una instalacion, cada etapa de un procedimiento), \
desglosalas como `subtasks` de esa tarea (2 niveles: tarea principal + sus subtasks, nunca un \
3er nivel) en vez de crearlas como items sueltos al mismo nivel -- asi se ve la relacion \
"pertenece a". Si una parte depende de otra (ej. un paso que necesita el resultado de otro), \
dejalo claro en el `content` de esa subtask.
- Si necesitas aclaracion antes de continuar, usa la tool `question` en vez de asumir.
- **REGLA DURA: cualquier confirmacion de riesgo (borrar/sobrescribir datos, DROP, DELETE \
irreversible, o cualquier accion que pueda danar archivos/datos de la compania) DEBE hacerse \
con la tool `question` (agregando "(Riesgo)" a la opcion peligrosa), NUNCA como texto plano en \
tu respuesta.** Escribir "¿confirmas que...?" en texto normal NO pausa el loop ni suena la \
alarma -- el usuario puede no verlo si esta lejos de la pantalla. La tool `question` es la UNICA \
forma de asegurar que el sistema se detenga y avise (alarma + banner) hasta que el usuario decida.
- **Data Discovery de BigQuery (REQ-001, spec controles-seguridad-optimizacion-lightspeed-\
bigquery): orden de PRIORIDAD, no un bloqueo duro.** Antes de escribir una consulta analitica \
de negocio sobre una tabla de BigQuery que todavia no exploraste en esta sesion, priorizá \
consultar primero su `INFORMATION_SCHEMA.TABLES`/`TABLE_OPTIONS` (description + labels) y \
`INFORMATION_SCHEMA.COLUMNS`/`COLUMN_FIELD_PATHS` (tipos y comentarios de columnas) -- asi \
escribis el SQL real con el esquema correcto en vez de adivinar nombres de columnas. **Si esa \
metadata viene vacia o no aporta nada util** (tabla sin `description`/labels/comentarios \
cargados), no te bloquees: segui con el proceso normal y arma la consulta con el criterio que \
tengas. Ademas, `bigquery_query`/`oracle_query` tienen sus propios guardrails de seguridad/costo \
que pueden devolver un bloqueo en vez de resultados -- ver la descripcion de esa tool para el \
protocolo de reintento tras confirmacion del usuario. **Si el bloqueo es por costo/riesgo y el \
usuario ya confirmo que si, reintenta la MISMA consulta exacta en tu proximo turno -- el sistema \
recuerda la aprobacion y esa vez la ejecuta de verdad, no hace falta "optimizar columnas" ni \
buscar una alternativa.**
- **Bug real corregido (2026-09-21): nunca agregues tu propio `LIMIT`/`FETCH FIRST` bajo (ej. \
LIMIT 50) a una consulta de BigQuery/Oracle "por prudencia" cuando el usuario pidio el detalle, \
todos los registros, o el dataset completo.** Eso le OCULTA filas reales al usuario y desactiva \
el export automatico a CSV completo con link de descarga que Vision genera cuando el resultado \
real supera el umbral configurado (ver `export_row_threshold` en Configuracion). Dejá que Vision \
decida el preview/export segun esa configuracion real -- si de verdad necesitas acotar el \
resultado (por pedido EXPLICITO del usuario, ej. "solo los primeros 10"), ahi si es correcto usar \
LIMIT.
- Cuando una tarea coincide con una skill disponible (ver <available_skills> abajo), \
cargala con la tool `skill` antes de proceder.
- Se conciso y directo. Cuando ejecutes tools, explica brevemente que estas haciendo.

Como ESCRIBIR tus respuestas (importante -- el chat renderiza Markdown):
- Escribi en español rioplatense correcto y prolijo: con TILDES y signos de puntuacion \
completos (¿? ¡!), mayuscula al empezar cada oracion y punto al terminarla. Nunca respondas \
en un solo bloque de texto corrido.
- **Un parrafo = una idea.** Separa los parrafos con una linea en blanco entre ellos (asi el \
chat los muestra como parrafos de verdad y no como un muro de texto). Manten los parrafos \
cortos: 2 a 4 oraciones como maximo.
- Usa la estructura de Markdown que el chat ya sabe mostrar, cuando aporte claridad:
  - `## Titulos` para separar secciones en respuestas largas.
  - Listas con `-` para enumerar cosas (opciones, pasos, hallazgos).
  - Listas numeradas `1.` solo cuando el ORDEN importa de verdad (instrucciones paso a paso).
  - **Negrita** para el dato clave de una linea, no para frases enteras.
  - Tablas Markdown cuando compares varios items por varios atributos.
  - Bloques de codigo con ``` y el lenguaje (```python, ```bash) para codigo, comandos y \
salidas de consola. Nombres de archivo, rutas, funciones y variables van en `codigo inline`.
  - Formulas matematicas/quimicas/estadisticas: SIEMPRE encerralas entre delimitadores LaTeX \
($...$ para una formula en linea, $$...$$ para una formula en su propio bloque/centrada) -- el \
chat las renderiza como simbolos reales (fracciones, derivadas, integrales, griego, quimica con \
\\ce{{}}) SOLO si estan entre esos delimitadores. Sin ellos, el LaTeX se muestra crudo e \
ilegible. Nunca escribas una formula sin encerrarla, ni siquiera una simple como $x^2$.
- Empeza por la respuesta o la conclusion, y despues el detalle. No arranques con preambulos \
tipo "Voy a explicarte..." ni cierres con resumenes que repiten lo que ya dijiste.
- Ser conciso NO significa escribir todo junto: significa no decir cosas de mas. Una respuesta \
breve tambien lleva sus puntos aparte y su puntuacion.
- Adapta el formato al tamaño de la respuesta: si es una sola frase, respondela en una sola \
frase bien escrita, sin titulos ni listas de un solo item. Reserva titulos y tablas para \
cuando el contenido realmente los justifique.

Terminal proactiva (spec terminal-tools-avanzadas REQ-001/002/003): tenes la tool `bash` sin \
restricciones (mismo criterio "acceso total sin friccion" del resto del proyecto) -- usala \
PROACTIVAMENTE, sin esperar a que te lo pidan literalmente:
- **Tests y linter (REQ-001/003)**: despues de escribir o modificar codigo, ANTES de dar la tarea \
por terminada, detecta y corre el test-runner/linter real de ESE proyecto -- nunca asumas \
pytest/ruff de entrada. Mira el proyecto primero (`requirements.txt`, `pyproject.toml`, \
`package.json` scripts, presencia de `pytest.ini`/`ruff.toml`/`.eslintrc`, etc.) para saber que \
comando corresponde. Si no hay tests/linter configurados, no lo inventes ni lo fuerces.
- **Instalacion de paquetes**: si detectas que falta una dependencia (import que falla, modulo no \
encontrado), instalala vos mismo con `pip install`/`npm install` segun corresponda -- no pauses a \
preguntar, instala y segui, avisando en el chat que lo hiciste.
- **Windows Task Scheduler (REQ-002)**: podes crear, listar, ejecutar-ahora y borrar tareas \
programadas con `schtasks` via `bash`. Despues de crear una tarea, confirma que quedo registrada \
consultando `schtasks /query /tn "nombre"` en vez de asumir exito solo por el codigo de salida.

Specs formales dentro de esta sesion (spec carpeta-por-sesion REQ-011):
- Si el usuario te pide armar una spec formal de algo (ej. "hagamos una spec para X", \
"quiero documentar esto como spec antes de programar"), y esta sesion tiene una carpeta de \
proyecto asignada (ver seccion de contexto de proyecto abajo, si existe), guardala en \
`spec/<slug-de-la-feature>/spec.md` (y `progress.md` si aplica) DENTRO de esa carpeta -- \
mismo mecanismo/estructura que usa el propio equipo de Vision para sus specs (interview \
por categorias, scoring, gate de aprobacion), pero con alcance a la carpeta de ESTA sesion \
en vez del repositorio de Vision. Usa `write`/`read` normalmente (sin ruta absoluta): ya \
resuelven por defecto a la carpeta de esta sesion.

Panel de navegador integrado (vista previa en vivo):
- Tenes una carpeta dedicada para armar mini-proyectos VISUALIZABLES en vivo (dashboards, \
 demos, prototipos con HTML/CSS/JS): {browser_dir}
- Si el usuario pide un dashboard, una visualizacion, un prototipo web, o cualquier cosa \
 que tenga sentido "ver en un navegador", escribi los archivos ahi (con `write`/`edit`, o \
 generalos con `bash`) usando un `index.html` (o `dashboard.html`) como archivo de entrada. \
 Ese contenido se muestra en el panel "Navegador Integrado" de los accesos rapidos.
- IMPORTANTE (no lo silencies ni simules que ya se mostro): VOS NO PODES ABRIR ese panel -- \
 es UI del frontend que solo el usuario abre/cierra con el boton "Navegador Integrado" del \
 panel de accesos rapidos (o que el propio frontend lo abre solo la PRIMERA vez que este turno \
 crea una pagina nueva -- actualizar una pagina existente no lo reabre solo). Cuando termines \
 de escribir el contenido, DECILE al usuario que lo abra/revise con el boton "Navegador \
 Integrado" en los accesos rapidos (no digas "ya se abrio" ni "ya esta visible"). El panel se \
 auto-actualiza solo una vez abierto; explicale que si no lo ve, presione el boton "Navegador \
 Integrado".
- Ese contenido es TEMPORAL: vive mientras dure esta sesion de chat: se borra si el usuario \
 elimina la sesion o libera el cache. No es el lugar para guardar nada que deba persistir.

Dashboard AUTONOMO para compartir sin conexion (tool `exportar_dashboard`):
- Cuando termines de generar un dashboard, preguntale UNA sola vez con la tool `question` si quiere \
 guardarlo en la carpeta `browser/` de la sesion para compartirlo sin conexion. Opciones del tipo \
 "Si, guardarlo para compartir" / "No por ahora". NUNCA le pongas el sufijo "(Recomendada)" a ninguna: \
 guardar es una decision del usuario y el Loop de Tareas no debe contestarla solo. Si dice que no, no \
 vuelvas a preguntar por ese dashboard. Si el usuario pide exportarlo sin que preguntes, hacelo igual.
- El HTML autonomo es UN solo archivo que abre con doble click, sin servidor ni red. Reglas: (1) CSS en \
 <style> y JS en <script>, inline; (2) los datos van en `<script type="application/json" id="datos">` y \
 se leen con `JSON.parse(document.getElementById("datos").textContent)`; dentro del JSON escribi `</` \
 como `<\\/`; (3) PROHIBIDO `fetch(`, `XMLHttpRequest`, `import(`, `WebSocket`, `EventSource`; (4) \
 PROHIBIDO cualquier `http://`, `https://` o `//` en src/href/url()/@import y cualquier archivo \
 relativo; (5) NO uses CDN: para Chart.js pone `<script data-vendor="chart"></script>` y la tool lo \
 reemplaza por la libreria de {vendor_dir} (no la copies vos); (6) NO uses `<script type="module">` con \
 imports; (7) sin wrappers de JS invalidos como `(function(d)=>{{...}})(datos)`: usa funciones normales \
 (`function main(datos) {{...}}`) o flechas bien formadas; (8) nunca incluyas contrasenas, tokens ni \
 client_secret.
- Para datasets grandes NO escribas los datos a mano (tu salida tiene tope de tokens): arma el HTML con un \
 script de Python via `bash` que lea los datos y los embeba en el JSON.
- Guarda el HTML armado con `write` o `bash` y llama a `exportar_dashboard(path, nombre)`. La tool lo \
 valida abriendolo en un navegador real con la red bloqueada. Solo si devuelve `OK: <ruta>` decile al \
 usuario que esta listo y pasale la ruta. Si devuelve `NO EXPORTADO: ...`, corregi el HTML segun los \
 motivos y reintenta (maximo 2 veces); despues explicale el problema. NUNCA digas "listo para \
 compartir" sin un OK de la tool.

Como presentar tus capacidades cuando te preguntan "que podes hacer" o similar:
- La skill de investigacion interna (Graph/SharePoint/directorio) sigue funcionando \
igual de completa cuando alguien la necesita explicitamente (perfil detallado con \
evidencia y fechas, responsables de procesos, pendientes en ZCC/trackers) -- eso NO \
cambia y segui aplicando todos sus gates de autorizacion y reglas de privacidad tal \
cual estan documentados en esa skill.
- Pero cuando describas tus capacidades en general (sin que te pidan investigar a \
alguien puntual), NO la enumeres como "puedo decirte todo sobre una persona" ni \
"investigacion exhaustiva con evidencia y fechas" -- eso suena a vigilancia, no es \
como quiere presentarse este asistente. En su lugar, describila en terminos de valor \
de negocio para el equipo: "puedo sugerir colaboradores para un proyecto segun su \
puesto y experiencia en proyectos similares" y "puedo ayudarte a identificar quien es \
responsable de un proceso o que tiene pendiente en ZCC/trackers". Mismo motor, \
presentacion mas orientada a colaboracion que a "buscar todo sobre alguien".

{skills_block}

{connections_block}{project_context_block}"""


def _vendor_dir_for_prompt() -> str:
    """REQ-005 (spec dashboards-autonomos-sesion): ruta absoluta de
    `frontend/vendor/`, que el prompt le informa al modelo."""
    from app.tools.dashboard_export import vendor_dir  # import diferido: evita ciclo

    return str(vendor_dir())


def _build_system_prompt(session_id: str) -> str:
    skills_xml = list_available_skills_xml(session_id)
    skills_block = skills_xml if skills_xml else "(no hay skills locales cargadas en esta instalacion)"
    # Contexto de proyecto (carpeta PROPIA de esta sesion + SharePoint
    # global elegido desde Configuracion) -- ver app/project_context.py.
    # Se recalcula en cada nueva conversacion (no se cachea) para reflejar
    # cambios recientes en el proyecto sin necesidad de reiniciar el
    # backend.
    project_context = build_project_context_prompt(session_id)
    # Carpeta de vista previa en vivo de ESTA sesion (ver
    # app/browser_workspace.py) -- se crea recien aca, la primera vez que
    # arranca una conversacion nueva, no antes.
    browser_dir = get_or_create_browser_dir(session_id)
    # spec conexiones-con-nombre REQ-005: que conexiones hay (solo tipo y nombre, nunca campos ni valores). Vacio si no hay nada que elegir.
    from app import (
        connections as connections_module,
    )  # import diferido: evita traer el registro de conexiones al importar el bucle

    connections_text = connections_module.describe_for_prompt()
    # spec vision-generico REQ-005: el nombre de la empresa sale de la configuracion.
    company = (getattr(get_settings(), "vision_company_name", "") or "").strip()
    return SYSTEM_PROMPT_TEMPLATE.format(
        connections_block=f"{connections_text}\n\n" if connections_text else "",
        company_clause=f" para {company}" if company else "",
        skills_block=skills_block,
        project_context_block=project_context,
        browser_dir=browser_dir,
        vendor_dir=_vendor_dir_for_prompt(),
    )


async def _stream_chat_completion(client, session, **kwargs) -> AsyncGenerator[Any, None]:
    """Puente sync->async: el SDK de OpenAI expone `stream=True` como un
    iterador SINCRONO. Iterarlo directo dentro de una funcion async
    bloquearia el event loop de FastAPI (nada mas podria atenderse
    mientras el modelo esta generando). Se corre la iteracion real en un
    hilo aparte, empujando cada chunk a una `queue.Queue` (thread-safe),
    y este generador async solo espera items de esa cola sin bloquear el
    loop (via `asyncio.to_thread` para el `.get()` bloqueante).

    BUG REAL detectado por el usuario (2026-09-16, "el Detener demora
    muchisimo"): el chequeo de `session.stop_requested` vivia SOLO en el
    LLAMADOR (`async for chunk in stream` en `_run_turn_impl`), que
    depende de que YA haya llegado un chunk de esta cola. Si el proveedor de IA
    todavia no emitio ni un token (el caso mas comun de espera larga -- ver
    el incidente real de 13:44 min en perf.log, donde el modelo tardo
    minutos en el PRIMER chunk), ese chequeo nunca se alcanzaba: el
    `q.get()` de antes bloqueaba sin timeout, asi que aunque el usuario
    apretara "Detener" al toque, no habia forma de que este generador se
    enterara hasta que el proveedor de IA arrancara a responder por su cuenta -- el
    boton quedaba en "Deteniendo…" sin efecto real por minutos.
    Fix: se poll-ea la cola con timeout corto en vez de bloquear
    indefinidamente, y en cada timeout se chequea `stop_requested`. Si esta
    pedido, se cierra la conexion HTTP real contra el proveedor de IA
    (`stream.close()`, que el SDK de OpenAI expone para este fin exacto --
    ver openai/_streaming.py) para cortar la espera de una, en vez de
    limitarse a dejar de leer chunks que de todos modos no iban a llegar
    mas rapido por eso."""
    q: queue.Queue = queue.Queue()
    stream_holder: dict[str, Any] = {}

    def _producer() -> None:
        try:
            stream = client.chat.completions.create(**kwargs)
            stream_holder["stream"] = stream
            for chunk in stream:
                q.put(chunk)
        except Exception as exc:  # noqa: BLE001
            q.put(exc)
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    # 250ms: suficientemente seguido para que "Detener" se sienta instantaneo
    # (peor caso realista de demora), suficientemente espaciado para no
    # generar overhead perceptible contra un turno que responde normal.
    _POLL_TIMEOUT_S = 0.25
    while True:
        try:
            item = await asyncio.to_thread(q.get, True, _POLL_TIMEOUT_S)
        except queue.Empty:
            if session.stop_requested:
                stream = stream_holder.get("stream")
                if stream is not None:
                    await asyncio.to_thread(stream.close)
                return
            continue
        if item is _SENTINEL:
            break
        if isinstance(item, Exception):
            raise item
        yield item


def _filter_generation_params(params: dict[str, Any] | None, valid_args: list[str]) -> dict[str, Any]:
    """Filtra generation_params contra los `valid_args` REALES del modelo
    activo (consultados en vivo via el proveedor de IA /models_metadata, ver
    app/main.py get_model_detail) -- nunca se manda un parametro que el
    modelo no soporte (ver la documentacion del proveedor: cada modelo acepta un
    set distinto, ej. 'verbosity' solo existe en GPT-5.x, 'top_k' no
    existe en NINGUN modelo del roster actual). `reasoning_effort` se
    mapea siempre que el modelo tenga reasoning_levels no vacio, ya que
    valid_args a veces no lo incluye explicitamente pero si lo acepta."""
    if not params:
        return {}
    out: dict[str, Any] = {}
    for key in ("temperature", "top_p", "max_tokens"):
        value = params.get(key)
        if value is not None and key in valid_args:
            out[key] = value
    reasoning = params.get("reasoning_effort")
    if reasoning and "reasoning_effort" in valid_args:
        out["reasoning_effort"] = reasoning
    verbosity = params.get("verbosity")
    if verbosity and "verbosity" in valid_args:
        out["verbosity"] = verbosity
    return out


# REQ-003/010 (spec adjuntar-archivos-chat): campos de un adjunto que se
# persisten en el part 'file' (el resto del dict del cliente se descarta).
_FILE_PART_KEYS = (
    "name",
    "path",
    "size",
    "mime",
    "kind",
    "inplace",
    "pages",
    "token_estimate",
)


def _browser_index_snapshot(session_id: str) -> tuple[str | None, float | None]:
    """REQ-002 (spec navegador-integrado-ventana): huella minima de la
    pagina de entrada de la carpeta de vista previa (`backend/browser/
    <session_id>/`) -- nombre del archivo + su mtime, o (None, None) si
    todavia no hay ninguna. Se usa para comparar el estado ANTES/DESPUES
    de un turno completo, sin importar que tool la creo (write/edit/bash),
    a diferencia de detectar por tool individual."""
    name = find_index_file(session_id)
    if name is None:
        return None, None
    try:
        mtime = (get_browser_dir(session_id) / name).stat().st_mtime
    except OSError:
        return None, None
    return name, mtime


async def run_turn(
    session: SessionState,
    user_message: str,
    session_id: str,
    model: str | None = None,
    generation_params: dict[str, Any] | None = None,
    user_message_id: str | None = None,
    images: list[str] | None = None,
    voice_confirm: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    narrate: bool = False,
    narrate_level: str = "summary",
) -> AsyncGenerator[dict[str, Any], None]:
    """Wrapper fino de `_run_turn_impl` (ver su docstring para el
    comportamiento real del turno) -- investigacion de rendimiento 2026-09
    ("Vision tarda mucho"): arma un `TurnPerfRecorder` para ESTE turno y
    garantiza que `perf.finish()` se escriba en `backend/logs/perf.log` sin
    importar como termine el turno (normal, error, STOP, o el consumidor
    corta la iteracion antes de tiempo) -- un `try/finally` alrededor de
    TODO el cuerpo de `_run_turn_impl` (que tiene varios `return` internos y
    un loop de hasta MAX_STEPS pasos) hubiera significado re-indentar una
    funcion de cientos de lineas; delegar a una funcion interna con un
    unico punto de entrada/salida es mas seguro.

    REQ-002/REQ-007 (spec navegador-integrado-ventana): ademas se toma una
    huella de la pagina de entrada de la carpeta de vista previa ANTES de
    ejecutar el turno y se compara DESPUES -- si no existia y ahora existe,
    se yieldea (ver mas abajo) un evento 'browser-page-created' JUSTO ANTES
    del evento terminal ('done'/'error'/'stopped'); si ya existia y cambio
    de mtime, 'browser-page-updated'. Es DELIBERADAMENTE a nivel de TURNO
    completo (no dentro de la tool `write`/`edit`/`bash`): la deteccion
    debe disparar sin importar que tool escribio el archivo (ej. un script
    de `bash` que corre `fig.write_html(...)` de plotly), y debe ignorar
    reescrituras de una pagina que YA existia al empezar el turno (eso es
    una actualizacion, REQ-007, no una pagina 'nueva' que amerite abrir el
    panel solo -- la decision de abrir es 100% del usuario, REQ-001).

    Bug real encontrado armando el test e2e (2026-09-28): la primera
    implementacion empujaba este evento con `session.push_event` desde el
    `finally`, DESPUES de que el evento 'done' ya habia sido reenviado a la
    cola por `_launch_turn` (`app/main.py`) -- pero la conexion SSE que
    espera UN turno puntual (`wait_for_turn_id`, el caso normal del POST
    /api/chat) cierra la conexion apenas ve 'done', SIN drenar el resto de
    la cola primero (a diferencia de una reconexion generica, que si
    chequea `queue.empty()`). El evento nuevo llegaba a la cola, pero la
    conexion ya se habia cerrado. Fix real: se yieldea DENTRO del propio
    generador, intercalado justo ANTES del evento terminal -- el orden
    exacto en el que `_launch_turn` los reenvia a la cola queda
    garantizado: 'browser-page-created'/'browser-page-updated' primero,
    'done' despues."""
    perf = TurnPerfRecorder(session_id, model or get_settings().llm_model)
    status = "done"
    before_name, before_mtime = _browser_index_snapshot(session_id)
    _TERMINAL_TYPES = ("done", "error", "stopped")
    browser_event_emitted = False
    try:
        async for event in _run_turn_impl(
            session,
            user_message,
            session_id,
            perf,
            model=model,
            generation_params=generation_params,
            user_message_id=user_message_id,
            images=images,
            voice_confirm=voice_confirm,
            attachments=attachments,
            narrate=narrate,
            narrate_level=narrate_level,
        ):
            if event.get("type") in _TERMINAL_TYPES and not browser_event_emitted:
                browser_event_emitted = True
                after_name, after_mtime = _browser_index_snapshot(session_id)
                if after_name is not None:
                    if before_name is None:
                        yield {
                            "type": "browser-page-created",
                            "data": {
                                "file": after_name,
                                "message_id": user_message_id,
                            },
                        }
                    elif after_mtime != before_mtime:
                        yield {
                            "type": "browser-page-updated",
                            "data": {
                                "file": after_name,
                                "message_id": user_message_id,
                            },
                        }
            yield event
    except BaseException:
        status = "error"
        raise
    finally:
        await asyncio.to_thread(perf.finish, status)
        # Red de seguridad: si el turno termino SIN pasar por ninguno de los
        # 3 tipos terminales de arriba (ej. limite de MAX_STEPS -- ese
        # camino yieldea 'error' asi que ya esta cubierto, pero por si
        # aparece otro final atipico en el futuro) el evento no se pierde,
        # aunque en ese caso vuelve a depender de `push_event` (mejor
        # esfuerzo, no garantiza el orden con un 'done' que ya se haya
        # emitido por otra via).
        if not browser_event_emitted:
            after_name, after_mtime = _browser_index_snapshot(session_id)
            if after_name is not None:
                if before_name is None:
                    session.push_event(
                        "browser-page-created",
                        {"file": after_name, "message_id": user_message_id},
                    )
                elif after_mtime != before_mtime:
                    session.push_event(
                        "browser-page-updated",
                        {"file": after_name, "message_id": user_message_id},
                    )


def _empty_promise_correction() -> dict[str, str]:
    """Mensaje 'system' que se inyecta cuando el modelo anuncio una accion sin
    ejecutarla (bug real 2026-09-21).

    Va como 'system' y no como 'user' para que nunca parezca que lo escribio el
    usuario: la PROXIMA llamada del mismo turno lo ve y no puede repetir la
    promesa vacia sin violar una instruccion explicita."""
    return {
        "role": "system",
        "content": (
            "[Corrección automática de Vision] Tu respuesta anterior "
            "solo tenía texto anunciando una acción (buscar, cargar "
            "el plan, ejecutar algo), pero NO llamaste a ninguna "
            "herramienta real -- ni siquiera `todowrite`. Esto está "
            "PROHIBIDO: nunca anuncies una acción sin ejecutarla en "
            "el mismo turno. No pidas disculpas ni repitas la "
            "promesa en texto: emití AHORA la tool-call real "
            "(`todowrite` primero, si el pedido es planificable), "
            "sin más texto de anuncio antes."
        ),
    }


def _plan_first_message(tool_name: str) -> str:
    """Resultado que ve el modelo cuando pide una tool de accion sin haber
    cargado antes el plan con `todowrite` (Regla N°1, ver `_PLAN_FIRST_TOOLS`).
    La tool NO se ejecuta: este texto ocupa el lugar de su resultado."""
    return (
        f"[Regla de planificacion (pedido del usuario)] Aun no hay un "
        f"plan de tareas cargado, y queres ejecutar `{tool_name}`. La regla es "
        f"inflexible: PRIMERO EL PLAN, DESPUES EJECUTA. Cargá el plan "
        f"completo con la tool `todowrite` (los pasos concretos de ESTE "
        f"proceso: conectarse a la fuente, analizar/ubicar las tablas, "
        f"buscar los datos, resumir la respuesta) y recién después ejecuta "
        f"`{tool_name}`. No ejecutes ninguna accion antes de tener el plan."
    )


# REQ-009 (asistente-contextual-voz): resultado que ve el modelo en lugar de la
# ejecucion real de una tool peligrosa pedida desde el Modo Cerebro. No se
# ejecuta nada; el modelo debe preguntar "¿estas seguro?" en su respuesta, que
# se lee por voz, y esperar la contestacion del usuario.
_VOICE_CONFIRM_REQUIRED_MESSAGE = (
    "[Confirmación requerida, Modo Cerebro] Esta herramienta es "
    "potencialmente peligrosa (modifica archivos o ejecuta comandos) y "
    "esta intervención viene de una conversación por voz. Todavía NO se "
    "ejecutó. En tu respuesta de texto (que el usuario va a escuchar por "
    "voz), preguntale explícitamente si confirma que querés proceder "
    "(ej. '¿Estás seguro de que querés que haga esto?') y esperá su "
    "respuesta antes de intentar de nuevo esta herramienta."
)


# REQ-003 (spec fix-detener-y-progreso-visible): mientras el modelo escribe los argumentos de una tool-call (el `tool-call` recien sale
# al FINAL del stream) se avisa a la pantalla con `tool-writing` -- caso real medido: un `write` de 15 KB tardo ~69 s sin ningun evento,
# con la tarjeta quieta en «Ejecutando comando… 91%». Un aviso apenas se conoce el nombre y despues, a lo sumo, uno cada este intervalo
# por tool-call (y solo si llegaron caracteres nuevos): la pantalla ve crecer el tamaño sin inundar el stream con un evento por fragmento.
TOOL_WRITING_INTERVAL_S = 0.5

# Valor COMPLETO del argumento "path"/"file_path" dentro del JSON parcial de los argumentos. Exige que la clave venga despues de `{` o
# `,` (fuera de cualquier string: adentro de un string JSON todas las comillas van escapadas, `\"path\"`, asi que un "path" escrito en
# el CONTENIDO del archivo nunca coincide) y que el valor ya haya cerrado su comilla. Lineal: sin retroceso catastrofico.
_ARG_PATH_RE = re.compile(r'[{,]\s*"(?:path|file_path)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_MAX_WRITING_PATH_CHARS = 200


def _partial_args_basename(partial_args: str) -> str | None:
    """REQ-003: nombre del archivo (basename) que el modelo esta escribiendo, sacado del JSON PARCIAL de los argumentos; None si
    todavia no llego entero o no hay. Nunca lanza: es solo un dato para la pantalla."""
    match = _ARG_PATH_RE.search(partial_args)
    if match is None:
        return None
    try:
        value = json.loads(f'"{match.group(1)}"')
    except ValueError:
        return None
    # PureWindowsPath entiende "\" y "/" como separador (el modelo usa cualquiera de los dos, en cualquier sistema).
    name = PureWindowsPath(value).name if isinstance(value, str) else ""
    return name[:_MAX_WRITING_PATH_CHARS] or None


class _ToolWritingNotices:
    """REQ-003: decide cuando avisar `tool-writing` para cada tool-call del paso (por su `index` en el stream). Un objeto por paso."""

    def __init__(self) -> None:
        self._last_at: dict[int, float] = {}
        self._last_chars: dict[int, int] = {}
        self._path: dict[int, str] = {}

    def event_for(self, idx: int, entry: dict[str, Any]) -> dict[str, Any] | None:
        if not entry["name"]:
            return None  # sin nombre todavia no hay nada que mostrar
        chars = len(entry["arguments"])
        now = time.monotonic()
        last_at = self._last_at.get(idx)
        if last_at is not None and (chars == self._last_chars.get(idx) or now - last_at < TOOL_WRITING_INTERVAL_S):
            return None
        self._last_at[idx], self._last_chars[idx] = now, chars
        path = self._path.get(idx)
        if path is None:
            path = _partial_args_basename(entry["arguments"])
            if path is not None:
                self._path[idx] = path  # una vez completo no cambia: no se vuelve a buscar
        return {
            "type": "tool-writing",
            "data": {"id": entry["id"], "name": entry["name"], "chars": chars, "path": path},
        }


def _thinking_label(step: int, turn_had_tools: bool) -> str:
    """REQ-002 (spec fix-detener-y-progreso-visible): texto de la espera al modelo en CADA paso. Antes de la primera herramienta es
    «Pensando…» (tambien si se repite el paso por un salto de modelo o una edicion); despues de un resultado, el numero de paso humano."""
    if step == 0 or not turn_had_tools:
        return "Pensando…"
    return f"Pensando el próximo paso… (paso {step + 1})"


def _operation_key(call: dict[str, Any]) -> str:
    """Clave con la que se busca el tiempo historico de una tool-call.

    Casi siempre es el nombre de la tool; para `skill` es `skill:<nombre>`, el
    mismo criterio que usa `app/tools/skill.py` al registrar la duracion real
    (spec barra-progreso-adaptativa REQ-002/004/005). Si los argumentos no son
    un JSON valido se usa el nombre de la tool, sin fallar."""
    key = call["name"]
    if key == "skill":
        try:
            skill_name = json.loads(call["arguments"] or "{}").get("name", "")
            if skill_name:
                key = f"skill:{skill_name}"
        except (json.JSONDecodeError, AttributeError):
            pass
    return key


async def _gather_tools_stoppable(session: SessionState, coros: list[Any]) -> list[str]:
    """Como `asyncio.gather` de las tool-calls de un paso, pero un Detener real corta la espera.

    Sin Detener se comporta IGUAL que antes (espera a todas). Con un Detener real (`stop_requested` sin `edit_pending_text`) las tools que sigan
    corriendo tienen `STOP_GRACE_S` segundos para cerrar limpio; las que no, quedan con `_STOPPED_TOOL_RESULT` y el turno sigue hacia `stopped`.
    Las tareas abandonadas NO se cancelan con `.cancel()` (AGENTS.md: cancelacion cooperativa): se les agrega un callback que consume su
    resultado o excepcion, para que no queden «exception never retrieved» en el log, y terminan solas."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    stop_deadline: float | None = None
    while True:
        pending = [t for t in tasks if not t.done()]
        if not pending:
            break
        real_stop = session.stop_requested and session.edit_pending_text is None
        if real_stop and stop_deadline is None:
            stop_deadline = time.monotonic() + STOP_GRACE_S
        if stop_deadline is not None and time.monotonic() >= stop_deadline:
            break
        await asyncio.wait(pending, timeout=_STOP_POLL_S, return_when=asyncio.ALL_COMPLETED)
    results: list[str] = []
    for task in tasks:
        if task.done():
            results.append(task.result())
        else:
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            results.append(_STOPPED_TOOL_RESULT)
    return results


def _assistant_tool_message(assistant_text: str, ordered_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Mensaje 'assistant' en formato OpenAI con las tool_calls del paso.
    `content` queda en None si el modelo no escribio texto antes de pedirlas."""
    return {
        "role": "assistant",
        "content": assistant_text or None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in ordered_calls
        ],
    }


def _apply_message_edit(session: SessionState) -> dict[str, Any]:
    """Aplica la edicion pedida del mensaje en curso y devuelve el evento
    `message-edited` para el frontend (REQ-004, parada-y-cola-mensajes).

    Una edicion tambien marca `stop_requested`, pero con `edit_pending_text`: no
    es un STOP real. Se reemplaza el mensaje `user` de este turno EN EL LUGAR,
    dejando intactos los tool_calls y resultados ya generados, y el bucle
    sigue llamando al modelo con ese historial para que reuse lo que pueda.
    Lo usan los dos puntos donde el bucle detecta el pedido: entre pasos y al
    cortar el streaming. Exige `edit_pending_text` no nulo (el llamador lo chequea)."""
    new_text = session.edit_pending_text or ""  # el `or ""` solo calma al tipador
    session.stop_requested = False
    session.edit_pending_text = None
    session.replace_active_user_message(new_text)
    return {"type": "message-edited", "data": {"text": new_text}}


def _persist_user_text(
    session_id: str,
    user_message: str,
    attachments: list[dict[str, Any]] | None,
    user_msg_id: str,
    active_model: str,
) -> None:
    """Asegura la sesion en SQLite, la autonombra si todavia no tiene nombre y guarda el texto LIMPIO del mensaje del usuario."""
    persistence.ensure_session(session_id, model=active_model)
    # gestion-sesiones REQ-006: mismo criterio "primeras palabras del
    # primer mensaje" que ya usa el frontend (touchCurrentSessionInIndex
    # en app.js) pero persistido -- SOLO pisa el titulo si todavia no fue
    # renombrado a mano NI autonombrado antes (chequeo atomico en
    # db.autoname_session: title_manual=0 Y title todavia == placeholder).
    # Se llama en CADA turno (barato: sin UPDATE real una vez que el
    # titulo ya cambio) en vez de gatillar solo cuando `session.messages`
    # en memoria tiene 0 mensajes 'user' -- ese contador se resetea con
    # cada reinicio del proceso (uvicorn --reload/crash), asi que si el
    # reinicio coincidia con el primer turno de una sesion, la UNICA
    # oportunidad de nombrarla se perdia para siempre (bug real QA
    # 2026-09-16: sesiones con mensajes genuinos que quedaron con el
    # titulo "Nueva sesion - <timestamp>" sin poder autonombrarse nunca).
    if user_message.strip():
        persistence.maybe_autoname_session(session_id, user_message[:40])
    elif attachments:
        # REQ-003 d: sin texto, el autonombrado usa el nombre del primer archivo.
        persistence.maybe_autoname_session(session_id, attachments_module.autoname_from(attachments)[:40])
    persistence.record_message_start(session_id, user_msg_id, "user")
    persistence.record_part_start(session_id, user_msg_id, str(uuid.uuid4()), "text", {"text": user_message})


def _append_user_message(
    session: SessionState,
    session_id: str,
    user_message: str,
    user_msg_id: str,
    images: list[str] | None,
    attachments: list[dict[str, Any]] | None,
) -> None:
    """Agrega al historial el mensaje del usuario tal como lo ve el MODELO (texto + aviso de adjuntos; con imagenes, una lista de
    bloques) y persiste cada adjunto y cada imagen como su propio part. El texto persistido sigue limpio."""
    # REQ-003 (spec adjuntar-archivos-chat): el aviso de rutas lo arma el
    # BACKEND y se agrega SOLO al contenido que ve el LLM; el texto persistido
    # y mostrado (`user_message`) queda limpio (REQ-003 a). Cada adjunto se
    # persiste como su propio part 'file' (REQ-010), asi el aviso se rearma tras
    # un reinicio (`db.load_messages`).
    llm_user_text = attachments_module.with_notice(user_message, attachments) if attachments else user_message
    for att in attachments or []:
        persistence.record_part_start(
            session_id,
            user_msg_id,
            str(uuid.uuid4()),
            "file",
            {k: v for k, v in att.items() if k in _FILE_PART_KEYS},
        )

    if images:
        # REQ-006/008: content en formato lista -- un bloque de texto (aun
        # si esta vacio, se omite si no hay texto: mandar {"type":"text",
        # "text":""} junto a imagenes es valido pero innecesario) seguido
        # de un bloque image_url por imagen, SIEMPRE base64 inline (nunca
        # URL externa -- esa variante es exclusiva de OpenAI, rechazada
        # por Gemini/Claude/on-prem segun la skill skill-interna).
        content_blocks: list[dict[str, Any]] = []
        if llm_user_text:
            content_blocks.append({"type": "text", "text": llm_user_text})
        for data_uri in images:
            content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
        session.messages.append({"role": "user", "content": content_blocks})

        # REQ-008: cada imagen se persiste como su propio part -- misma
        # tabla `part` de la spec B, sin cambio de esquema (columna `data`
        # es TEXT sin limite practico). part_type="image" es nuevo; los
        # lugares que leen `part.type` (load_messages, transcript,
        # busqueda) ya ignoran defensivamente tipos desconocidos, pero se
        # les agrega soporte explicito para que sea funcional de punta a
        # punta (ver app/persistence/db.py y transcript.py).
        for data_uri in images:
            persistence.record_part_start(session_id, user_msg_id, str(uuid.uuid4()), "image", {"url": data_uri})
    else:
        session.messages.append({"role": "user", "content": llm_user_text})


def _voice_and_narration_instruction(
    session: SessionState, voice_confirm: bool, narrate: bool, narrate_level: str
) -> tuple[str, bool, str]:
    """Instrucciones extra que se le suman al mensaje segun el modo (Modo Cerebro, respuesta hablada a una confirmacion pendiente,
    informe tecnico hablado) y deja `session.narrate`/`narrate_level` al dia. Devuelve `(instruccion, voice_confirm_bypass,
    narrate_level_normalizado)`. Consume `session.voice_pending_confirmation`: una confirmacion nunca sobrevive mas de 1 turno."""
    # REQ-009 (spec asistente-contextual-voz): si habia una tool peligrosa
    # esperando confirmacion hablada Y este turno tambien viene del modo
    # Cerebro (la respuesta hablada del usuario a "¿estas seguro?"), se
    # resuelve ANTES de llamar al LLM -- se limpia el pendiente (nunca
    # sobrevive mas de 1 turno, evita que quede "colgado" si el usuario
    # cambia de tema en vez de responder) y se le agrega al mensaje user
    # una instruccion explicita para que el LLM interprete la respuesta
    # con flexibilidad (no una lista fija de palabras clave, ver spec) y
    # decida si repetir la tool-call original o abandonarla.
    # REQ-009: mientras dure ESTE turno especifico (la respuesta hablada a
    # una confirmacion pendiente), la interceptacion de tools peligrosas
    # se saltea por completo -- este turno YA ES la resolucion de esa
    # confirmacion, si el modelo decide volver a pedir la tool es porque
    # interpreto la respuesta del usuario como afirmativa (ver
    # instruccion inyectada abajo). Sin este bypass, la propia tool-call
    # de "reintento" volveria a quedar interceptada y jamas se ejecutaria
    # nada (loop infinito de "pregunto de nuevo").
    voice_confirm_bypass = False
    voice_confirm_instruction = ""
    if voice_confirm and session.voice_pending_confirmation is not None:
        pending = session.voice_pending_confirmation
        session.voice_pending_confirmation = None
        voice_confirm_bypass = True
        voice_confirm_instruction = (
            "\n\n[Confirmación pendiente, Modo Cerebro] Le preguntaste al usuario si "
            f"confirmaba ejecutar la herramienta '{pending['name']}' (argumentos: "
            f"{pending['arguments']}) y esto es lo que respondió, transcrito por voz. "
            "Interpretá la respuesta con flexibilidad (no busques palabras clave "
            "exactas): si suena a una confirmación afirmativa, volvé a pedir esa misma "
            "herramienta con los mismos argumentos (o ajustados si el usuario aclaró "
            "algo distinto) para que se ejecute; si suena a una cancelación, duda, o "
            "un cambio de tema, NO la vuelvas a pedir -- respondé reconociendo que no "
            "se va a ejecutar."
        )
    elif voice_confirm and session.voice_pending_confirmation is None:
        # REQ-011: instruccion de manejo de transcripcion ambigua -- SOLO
        # para turnos del modo Cerebro (el chat de texto nunca la recibe,
        # no la necesita). Se agrega en TODO turno de voz sin confirmacion
        # pendiente (no solo cuando "parece" confuso) porque es el propio
        # LLM quien debe evaluar, con el texto ya transcrito delante, si
        # esta transcripcion puntual amerita preguntar en vez de asumir.
        voice_confirm_instruction = (
            "\n\n[Modo Cerebro] El mensaje de arriba es una transcripción de voz "
            "(puede tener errores de reconocimiento). Si podés inferir razonablemente "
            "qué te pide, aunque la transcripción no sea perfecta, actuá con tu mejor "
            "interpretación. Solo si genuinamente no podés inferir ninguna acción o "
            "pregunta razonable (la transcripción no tiene sentido claro), preguntá "
            "explícitamente qué quiso decir el usuario, en vez de asumir o ejecutar "
            "algo al azar."
        )
    # spec informe-tecnico-hablado REQ-009/010: con el informe tecnico
    # hablado prendido, el modelo comenta cada seccion del codigo (de ahi sale
    # el recorrido en vivo) y `todowrite` anuncia el avance del plan. Mismo
    # mecanismo que la instruccion del Modo Cerebro: va en el mensaje del turno.
    session.narrate = narrate
    narrate_level = normalize_level(narrate_level)
    session.narrate_level = narrate_level
    if narrate:
        voice_confirm_instruction += (
            "\n\n[Informe técnico hablado] El usuario escucha en voz alta lo que hacés: cuando escribas "
            "o edites código, comentá en español cada sección y funcionalidad con una línea que diga qué "
            "hace y dónde va (HTML <!-- -->, JS/CSS // o /* */, Python #, SQL --), incluidas las "
            "conexiones a bases de datos o APIs."
        )
        # REQ-015 (reapertura 2): en el nivel Detallado se comenta TODO y se abre con el diseno.
        if narrate_level == LEVEL_DETAILED:
            voice_confirm_instruction += (
                " Nivel detallado: comentá TODO el código, cada bloque, no solo las secciones: cada "
                "etiqueta de estructura (encabezado, menú, contenido, pie de página), cada regla o grupo "
                "de estilos y cada función, con una línea corta en español que diga qué hace y por qué. "
                "Si armás una página o un dashboard, abrí el archivo con un comentario de cabecera que "
                "cuente el diseño y qué va a mostrar (indicadores, gráficos, filtros); es lo primero que "
                "el usuario va a escuchar."
            )
    return voice_confirm_instruction, voice_confirm_bypass, narrate_level


def _append_to_last_user_message(session: SessionState, instruction: str) -> None:
    """Suma `instruction` como continuacion del ULTIMO mensaje de usuario recien agregado: con `content` string se concatena; con
    la lista de bloques de imagenes se agrega un bloque de texto, sin pisar los existentes."""
    # Se agrega como continuacion del ULTIMO mensaje user recien
    # insertado (arriba) -- funciona tanto para el formato string
    # plano como para el formato lista con imagenes (se agrega un
    # bloque de texto adicional en ese caso, nunca se pisa el ya
    # existente).
    last = session.messages[-1]
    if isinstance(last.get("content"), list):
        last["content"].append({"type": "text", "text": instruction.strip()})
    else:
        last["content"] = (last.get("content") or "") + instruction


def _record_user_turn(
    session: SessionState,
    session_id: str,
    user_message: str,
    active_model: str,
    user_message_id: str | None,
    images: list[str] | None,
    attachments: list[dict[str, Any]] | None,
    voice_confirm: bool,
    narrate: bool,
    narrate_level: str,
) -> tuple[bool, str]:
    """Registra el mensaje del usuario en el historial y en SQLite, y le agrega
    las instrucciones propias de cada modo (voz, informe tecnico hablado).

    Es la primera mitad de `_run_turn_impl`, sacada a su propia funcion (BL-024
    fase 2) sin cambiar su comportamiento: no hace `yield` ni llama al modelo,
    solo prepara el estado del turno. Devuelve `(voice_confirm_bypass,
    narrate_level)`:
    - `voice_confirm_bypass`: True si este turno es la respuesta hablada a una
      confirmacion pendiente (el bucle no debe volver a interceptar la tool).
    - `narrate_level`: el nivel ya normalizado, que el bucle usa despues para
      crear los narradores en vivo."""

    user_msg_id = user_message_id or str(uuid.uuid4())
    _persist_user_text(session_id, user_message, attachments, user_msg_id, active_model)
    _append_user_message(session, session_id, user_message, user_msg_id, images, attachments)
    voice_confirm_instruction, voice_confirm_bypass, narrate_level = _voice_and_narration_instruction(
        session, voice_confirm, narrate, narrate_level
    )
    if voice_confirm_instruction:
        _append_to_last_user_message(session, voice_confirm_instruction)

    return voice_confirm_bypass, narrate_level


async def _load_model_catalog() -> dict[str, dict[str, Any]]:
    """Catalogo del proveedor de IA (`valid_args`, alias, aptitud de cada modelo) desde el cache de `app.main`. Si la consulta falla devuelve
    lo que haya (`{}`): el chat sigue sin catalogo, o sea sin parametros opcionales, en vez de romperse por un problema transitorio
    de `/models_metadata`."""
    # Catalogo del proveedor de IA (valid_args de cada modelo, alias, aptitud para datos
    # secretos) -- reusa el cache de app.main; si la consulta falla se sigue sin
    # catalogo, para no romper el chat por un problema transitorio de
    # /models_metadata.
    catalog: dict[str, dict[str, Any]] = {}
    try:
        from app.main import _MODELS_CACHE, list_models  # import diferido: evita ciclo

        if _MODELS_CACHE.get("by_id") is None:
            # Investigacion de rendimiento 2026-09: list_models() hace un GET
            # sincronico al proveedor de IA (/models_metadata) -- sin to_thread, esto
            # bloqueaba el event loop entero (todas las sesiones) cada vez
            # que el cache de 5 min expiraba, no solo esta sesion.
            await asyncio.to_thread(list_models)
        catalog = _MODELS_CACHE.get("by_id") or {}
    except Exception:
        logger.debug("No se pudo cargar el catalogo de modelos del proveedor de IA", exc_info=True)
    return catalog


def _ensure_system_prompt(session: SessionState, session_id: str, perf: TurnPerfRecorder) -> None:
    """Pone el system prompt como PRIMER mensaje si la sesion no lo tiene (sesion nueva o recuperada de SQLite tras un reinicio)."""
    # persistencia-sqlite-sesiones: el system prompt NUNCA se persiste en
    # SQLite (se reconstruye siempre en vivo, ver _build_system_prompt --
    # asi refleja el contexto de proyecto/skills actual, no uno viejo
    # congelado). Por eso el chequeo no puede ser "session.messages esta
    # vacio" cuando la sesion se recupera desde disco (REQ-003): en ese
    # caso session.messages YA tiene mensajes viejos pero NINGUNO es
    # 'system' -- sin este chequeo explicito, el modelo perderia sus
    # instrucciones en cualquier sesion recuperada tras un reinicio.
    if not any(m.get("role") == "system" for m in session.messages):
        _sysprompt_start = time.monotonic()
        session.messages.insert(0, {"role": "system", "content": _build_system_prompt(session_id)})
        perf.mark_system_prompt_built((time.monotonic() - _sysprompt_start) * 1000)


def _prune_history_best_effort(session: SessionState, active_model: str) -> dict:
    """spec aviso-compactacion-automatica REQ-001/002: poda los resultados viejos de herramientas si la conversacion llego al umbral y dice
    que paso (`action` = none | pruned | needs_summary, con `before_pct`/`after_pct`/`window`). NO llama al modelo y NUNCA lanza: ante
    cualquier problema devuelve `none` y el turno sigue como siempre. Va ANTES de `_compact_history_best_effort` para poder avisar antes de
    la espera del resumen (caso real 2026-10-24: una sesion al 100 % quedo en 43 % sin aviso porque solo se podo)."""
    from app.main import resolve_context_window  # import diferido: evita ciclo
    from app.session.compaction import prepare_compaction

    try:
        window = resolve_context_window(active_model)
        report = prepare_compaction(session.messages, window)
    except Exception:  # noqa: BLE001 -- avisar es accesorio: jamas corta el turno
        logger.warning("No se pudo podar el historial de la sesion antes del turno", exc_info=True)
        return {"action": "none", "before_pct": 0, "after_pct": 0, "window": 0}
    return {**report, "window": window}


def _compaction_notice(kind: str, report: dict, after_pct: int | None = None) -> str:
    """Los textos que ve la persona al compactar (spec aviso-compactacion-automatica REQ-004..008): sin jerga ni codigos."""
    if kind == "start":
        return "Estoy resumiendo la conversación para poder seguir (ya es muy larga). Puede tardar unos segundos."
    if kind == "pruned":
        return (
            "Limpié los resultados viejos de las herramientas para liberar espacio: la conversación pasó de "
            f"{report['before_pct']}% a {report['after_pct']}% de lo que el modelo puede leer. Si necesitás alguno, pedímelo de nuevo."
        )
    if kind == "summarized":
        return (
            "Resumí la parte más vieja de esta conversación para poder seguir (ya era muy larga): pasó de "
            f"{report['before_pct']}% a {after_pct}% de lo que el modelo puede leer. Lo importante se conserva."
        )
    return "No pude resumir la conversación esta vez; sigo con lo que hay. Si el modelo la rechaza por larga, escribí /compact o /clear."


async def _compaction_events(
    session: SessionState, session_id: str, active_model: str, picker: model_fallback.TurnModelPicker | None
) -> AsyncGenerator[dict, None]:
    """Poda/compacta el historial antes del turno y AVISA a la persona (spec aviso-compactacion-automatica REQ-004..007).

    - `pruned` (la poda de resultados viejos alcanzo): UNA nota con los porcentajes, sin nota de inicio.
    - `needs_summary`: nota de inicio ANTES de pedir el resumen (la espera dura segundos), y despues la de cierre, o la de «no pude» si el
      resumen no se hizo; el turno sigue igual (nunca se corta).
    - `none`: nada que avisar. Si de todos modos algo compacto (`maybe_compact` devuelve True), se avisa como siempre.
    Los avisos son accesorios (no se guardan en el historial) y la logica de compactar no cambia: sigue siendo `_compact_history_best_effort`."""
    report = _prune_history_best_effort(session, active_model)
    action = report["action"]
    if action == "pruned":
        yield {"type": "system-note", "data": {"text": _compaction_notice("pruned", report)}}
    elif action == "needs_summary":
        yield {"type": "system-note", "data": {"text": _compaction_notice("start", report)}}

    compacted = await _compact_history_best_effort(session, session_id, active_model, picker)

    if compacted:
        # REQ-014 b (spec fix-detener-y-progreso-visible): la compactacion rearma `session.messages` (resumen nuevo + turnos
        # recientes) y deja el mensaje de este turno al final. Lo de ANTES de este turno pasa a ser todo lo demas: si el
        # usuario borra este mensaje, se descarta solo lo suyo (sin esto se podia llevar el resumen de toda la conversacion).
        if getattr(session, "turn_baseline", None) is not None:
            session.turn_baseline = list(session.messages[:-1])
        window = report.get("window") or 0
        after = round(estimate_tokens(session.messages) / window * 100) if window else report["after_pct"]
        yield {"type": "system-note", "data": {"text": _compaction_notice("summarized", report, after)}}
    elif action == "needs_summary":
        yield {"type": "system-note", "data": {"text": _compaction_notice("failed", report)}}


async def _compact_history_best_effort(
    session: SessionState, session_id: str, active_model: str, picker: model_fallback.TurnModelPicker | None
) -> bool:
    """Poda y compacta el historial si supera el 85% del contexto del modelo activo. Mejor esfuerzo: si falla, el turno sigue sin
    compactar (el resumen se pide ANTES de tocar el historial, asi que no queda nada a medias). Devuelve si compacto."""
    # spec gestion-contexto-conversacion REQ-001: antes de la primera
    # llamada al LLM, poda y compacta si el historial supera el 85% del
    # contexto del modelo activo.
    from app.main import resolve_context_window  # import diferido: evita ciclo
    from app.session.compaction import maybe_compact

    context_window = resolve_context_window(active_model)
    # Compactar tambien llama al modelo. Bug real 2026-09-25 (prueba del salto
    # automatico): con el tope de consumo alcanzado, esta llamada lanzaba el 429
    # ANTES de que el turno llegara al modelo -- y como una conversacion larga
    # compacta en cada mensaje, cada mensaje terminaba en "problema interno".
    # Ahora: con el salto activo se compacta con el primer modelo disponible, y
    # si igual falla el turno sigue sin compactar (el resumen se pide ANTES de
    # tocar el historial, asi que no queda nada a medias).
    compact_model = picker.first_available() if picker is not None else active_model
    try:
        compacted = await maybe_compact(session_id, session.messages, context_window, compact_model)
    except Exception as exc:  # noqa: BLE001 -- compactar es mejor esfuerzo, nunca corta el turno
        compacted = False
        if picker is not None:
            picker.register_failure(compact_model, exc)  # el paso 0 ya salta y avisa
        logger.warning(
            "No se pudo compactar la sesion %s con %s; se sigue sin compactar: %s",
            session_id,
            compact_model,
            exc,
        )
    return compacted


async def _run_turn_impl(
    session: SessionState,
    user_message: str,
    session_id: str,
    perf: TurnPerfRecorder,
    model: str | None = None,
    generation_params: dict[str, Any] | None = None,
    user_message_id: str | None = None,
    images: list[str] | None = None,
    voice_confirm: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    narrate: bool = False,
    narrate_level: str = "summary",
) -> AsyncGenerator[dict[str, Any], None]:
    """Ejecuta un turno completo del agente (potencialmente varias vueltas
    de tool-calling) para un nuevo mensaje del usuario. Yields eventos
    dict listos para serializar como SSE.

    `session_id`: usado solo para armar el system prompt la primera vez
    (informarle al agente la ruta de su carpeta de vista previa en vivo,
    ver app/browser_workspace.py) -- el historial/estado real vive en
    `session` (SessionState), no aca.
    `model`: id/alias del proveedor de IA a usar en ESTE turno (ver /api/models y el
    selector del frontend). Si no se especifica, usa settings.llm_model
    (el default fijado en .env).
    `generation_params`: dict con temperature/top_p/max_tokens/
    reasoning_effort/verbosity elegidos en Configuracion -- se filtran
    contra los valid_args reales del modelo antes de mandarse (ver
    _filter_generation_params).
    `user_message_id` (persistencia-sqlite-sesiones REQ-002): id estable
    del UserTurn (mismo id que usa parada-y-cola-mensajes) para persistir
    el mensaje 'user' con un id predecible; si no se pasa (ej. llamadas
    viejas/tests), se genera uno interno solo para esta sesion de proceso
    -- no afecta el comportamiento en memoria, solo el id con el que
    quedaria la fila en SQLite.
    `images` (spec contexto-multimodal REQ-006): lista de data URIs
    base64 (`data:image/jpeg;base64,...`) YA comprimidas por el frontend
    (REQ-003) -- si viene no vacia, `session.messages` recibe un mensaje
    'user' con `content` en formato LISTA (texto + N bloques image_url),
    en vez del string plano de siempre. `user_message` (str) se mantiene
    SIEMPRE como el texto plano por separado -- se sigue usando tal cual
    para el autonombrado de sesion (REQ-006 gestion-sesiones) y otros usos
    que asumen texto, sin tener que tocarlos.
    `voice_confirm` (spec asistente-contextual-voz REQ-006/009/011): True
    cuando este turno se origino en una intervencion del modo Cerebro
    (conversacion por voz continua). Activa 2 comportamientos exclusivos
    de ese modo, sin afectar el chat de texto normal (False, default):
    (1) REQ-011: se le agrega al modelo una instruccion de como actuar
    ante una transcripcion de voz ambigua (preguntar en vez de asumir al
    azar, salvo que pueda inferir razonablemente que se le pide). (2)
    REQ-009: si el modelo pide ejecutar una tool `dangerous=True` (bash/
    write/edit), esta funcion la INTERCEPTA antes de `registry.dispatch`
    -- no la ejecuta, le devuelve al modelo un resultado de tool
    indicandole que debe preguntar "¿estas seguro?" en su respuesta (que
    se lee por TTS) y guarda esa tool pendiente en
    `session.voice_pending_confirmation`. La SIGUIENTE vez que este mismo
    `session_id` reciba un turno con `voice_confirm=True` (la respuesta
    hablada del usuario a esa pregunta), esta funcion detecta la
    confirmacion pendiente, la limpia, y bypassea la interceptacion por
    UNA sola vuelta de tool-calling -- si el modelo interpreto la
    respuesta como afirmativa, vuelve a pedir la misma tool y esta vez SI
    se ejecuta; si la interpreto como negativa, simplemente no la vuelve
    a pedir (la interpretacion de la respuesta es tarea del LLM, no de
    este codigo -- ver instruccion inyectada mas abajo)."""
    # MAPA DEL TURNO (BL-024): esta funcion es larga a proposito porque es un
    # generador que comparte estado entre fases (`yield` no se puede repartir en
    # sub-funciones sin reescribir el protocolo de eventos). Las fases, en orden:
    #   1. Preparacion: catalogo de modelos, picker del salto automatico, system
    #      prompt, mensaje del usuario (`_record_user_turn`) y compactacion.
    #   2. Bucle `for _step in range(MAX_STEPS)`, una vuelta por llamada al modelo:
    #      a. chequeo de stop/edicion entre pasos (`_apply_message_edit`);
    #      b. eleccion del modelo del paso (picker) y aviso `model-switch`;
    #      c. streaming: texto (filtrando el marcador `[[TIPO: ...]]`), tool-calls
    #         acumuladas y narracion en vivo; reintento unico ante 401 y salto al
    #         siguiente modelo si falla antes de responder;
    #      d. sin tool-calls: promesa vacia (reintento) o cierre con `done`;
    #      e. con tool-calls: se ejecutan en paralelo (`_dispatch_or_intercept`,
    #         que aplica la regla de plan primero y la confirmacion por voz).
    #   3. Si se agotan los pasos: evento `error` de limite (nunca silencio).
    # Invariante: cada turno termina con UN solo evento terminal
    # (`done`, `error` o `stopped`); lo fija `tests/test_loop_characterization.py`.
    settings = get_settings()
    registry = get_registry()

    catalog = await _load_model_catalog()

    # spec salto-automatico-modelos: con el salto activo, el modelo de CADA paso
    # lo elige el picker (REQ-002/003/004/009): el elegido a mano en el selector,
    # o el primero de la lista en modo Automatico, y si esta en pausa el
    # siguiente. Apagado (default), todo sigue como siempre (REQ-006).
    # spec varios-proveedores REQ-004/007: todo modelo viaja como
    # "proveedor/modelo" (uno sin prefijo es del proveedor principal).
    from app.main import default_model_id  # import diferido: evita ciclo

    model = llm_providers.canonical(model) if model else None
    fallback_settings = model_fallback.load_settings()
    picker = (
        model_fallback.TurnModelPicker(fallback_settings, model, default_model_id(settings.llm_model), catalog)
        if fallback_settings.enabled
        else None
    )
    active_model = picker.home if picker is not None else (model or default_model_id(settings.llm_model))
    extra_params = _extra_params_for(active_model, generation_params, catalog)
    session.active_model = active_model  # REQ-006: lo lee la tool `read`

    _ensure_system_prompt(session, session_id, perf)

    voice_confirm_bypass, narrate_level = _record_user_turn(
        session,
        session_id,
        user_message,
        active_model,
        user_message_id,
        images,
        attachments,
        voice_confirm,
        narrate,
        narrate_level,
    )

    # REQ-001/004: se limpia cualquier stop pendiente de un turno anterior
    # (ej. si este mismo run_turn es el reinicio disparado por editar un
    # mensaje en curso -- ver app/main.py endpoint PATCH de mensaje).
    # REQ-014 b (spec fix-detener-y-progreso-visible): salvo que el usuario ya haya BORRADO este mensaje (el borrado llego entre
    # que el turno salio de la cola y este punto): ese Detener es de ESTE turno y no se pierde -- sin esto el turno se
    # respondia entero aunque el usuario lo hubiera borrado.
    session.stop_requested = user_message_id is not None and user_message_id in getattr(session, "discard_turn_ids", ())
    session.edit_pending_text = None

    async for notice in _compaction_events(session, session_id, active_model, picker):
        yield notice

    # REQ-002 (spec clasificador-tipo-respuesta): filtro del marcador
    # `[[TIPO: xxx]]` -- ver `_filter_marker_chunk` para el detalle. `_carry`
    # vive a nivel de TURNO completo (no se resetea por paso) porque el
    # modelo puede re-emitir el marcador despues de cada vuelta de
    # tool-calling, no solo en la primera -- `turn_had_tools` tenia el mismo
    # bug (su propio comentario ya decia "acumulado a lo largo de las
    # vueltas" pero antes vivia DENTRO del `for _step` y se reseteaba en
    # cada paso).
    _carry = ""
    response_type: str | None = None  # se completa cuando se encuentra el 1er marcador
    turn_had_tools = False  # acumulado a lo largo de las vueltas de tool-calling
    # Bug real 2026-09-21 (ver _MAX_EMPTY_PROMISE_RETRIES arriba): cuenta
    # cuantas veces CONSECUTIVAS el modelo respondio con texto que anuncia
    # una accion sin emitir ninguna tool-call real -- se resetea apenas
    # una vuelta SI trae tool-calls de verdad (turn_had_tools ya cubre "en
    # algun momento hubo", esto cubre "la vuelta ANTERIOR fue una promesa
    # vacia consecutiva a esta").
    empty_promise_retries = 0

    for _step in range(MAX_STEPS):
        # Ultimo paso disponible del turno: se fuerza la llamada SIN tools
        # (ver tools_for_step mas abajo) para que el modelo NO pueda pedir
        # mas herramientas y concluya su respuesta en texto -- asi el turno
        # SIEMPRE termina con una respuesta util en vez de agotar el limite
        # y caer en el error "Se alcanzo el limite de pasos" al final del
        # loop (evita que el turno muera sin respuesta cuando una tarea
        # compleja necesita mas de MAX_STEPS iteraciones).
        final_step = _step == MAX_STEPS - 1

        # Chequeo de stop/edicion ENTRE pasos del loop (nunca a mitad de
        # una tool ya en ejecucion -- la espera de "cierre limpio" ocurre
        # naturalmente aca, porque este punto del loop solo se alcanza
        # DESPUES de que asyncio.gather() de las tool-calls del paso
        # anterior ya termino por completo).
        if session.stop_requested:
            if session.edit_pending_text is not None:
                # REQ-004: fue una EDICION del mensaje en curso, no un
                # STOP real -- se muta el mensaje `user` de este turno EN
                # EL LUGAR (dejando intactos los tool_calls/resultados/
                # todowrite ya generados en esta misma corrida) y se
                # CONTINUA el mismo loop llamando de nuevo al LLM con ese
                # historial enriquecido. El modelo ve su propio progreso
                # previo y decide que reusar.
                yield _apply_message_edit(session)
                continue

            # STOP real (no edicion): no se agrega nada falso al historial
            # real de OpenAI -- el historial ya esta en un estado
            # consistente en este punto (entre pasos). El frontend muestra
            # la nota visual de "turno detenido" a partir del evento
            # 'stopped', no del historial.
            yield {"type": "stopped", "data": {"reason": "user_requested"}}
            return

        # spec varios-proveedores REQ-006: un modelo gratuito elegido a mano no se
        # usa hasta que el usuario confirme ese proveedor (con el salto activo,
        # esos modelos ni siquiera estan en el catalogo y el picker los saltea).
        if _step == 0 and picker is None:
            from app.main import model_needs_consent  # import diferido: evita ciclo

            consent_label = model_needs_consent(active_model)
            if consent_label:
                yield _friendly_event(
                    FriendlyError(
                        "MODELO-CONSENTIMIENTO",
                        f"{consent_label} es un servicio gratuito que puede usar lo que le mandás para "
                        "mejorar sus productos. Si estás de acuerdo, confirmalo en Configuración > Modelo e "
                        "inteligencia > Proveedores y volvé a enviar tu mensaje; si no, elegí otro modelo en "
                        "el selector de arriba.",
                    ),
                    active_model,
                )
                return

        # spec salto-automatico-modelos: modelo de ESTE paso. `model-switch` va
        # siempre al arrancar el turno (el chip muestra el modelo que responde
        # de verdad) y cada vez que cambia, con el aviso si hubo salto.
        if picker is not None:
            choice = picker.choose(task_started=turn_had_tools)
            if choice.error is not None:
                yield _friendly_event(choice.error, active_model)  # REQ-004 / REQ-009
                return
            if _step == 0 or choice.model != active_model or choice.notice:
                active_model = choice.model or active_model
                session.active_model = active_model  # REQ-006: lo lee la tool `read`
                extra_params = _extra_params_for(active_model, generation_params, catalog)
                yield {
                    "type": "model-switch",
                    "data": {
                        "model": active_model,
                        "home": picker.home,
                        "text": choice.notice,
                        # REQ-010: a esa hora el frontend pregunta de nuevo y
                        # muestra la vuelta sin esperar al proximo mensaje.
                        "resume_at": picker.resume_at(),
                    },
                }

        assistant_text = ""
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        # spec informe-tecnico-hablado REQ-008: un narrador por tool-call.
        live_narrators: dict[int, LiveNarrator] = {}
        finish_reason: str | None = None
        stopped_mid_stream = False
        jump_to_next_model = False

        # persistencia-sqlite-sesiones REQ-002: ids generados de antemano
        # para este paso del loop -- se insertan en SQLite recien cuando
        # hay contenido real que guardar (primer delta de texto / primera
        # tool-call conocida), nunca antes, para no dejar filas vacias.
        assistant_msg_id = str(uuid.uuid4())
        text_part_id = str(uuid.uuid4())
        # Dos banderas, no una (bug real, auditoria 2026-10-08): con una sola, si el
        # modelo pedia una tool ANTES de escribir texto en el mismo paso, el part de
        # texto nunca se insertaba y su update final caia sobre un id inexistente --
        # ese texto no quedaba en SQLite y no volvia tras recargar ni reiniciar.
        assistant_msg_started = False  # fila `message` del assistant de este paso ya insertada
        text_part_started = False  # part 'text' de este paso ya insertado
        tool_part_ids: dict[str, str] = {}

        # REQ-002 (spec barra-progreso-adaptativa): estimacion generica de
        # "pensando" antes de cada llamada al LLM. Se guarda `thinking_start`
        # para registrar la duracion real cuando el PRIMER paso termine (con
        # o sin tool-calls).
        # REQ-002 (spec fix-detener-y-progreso-visible): antes se emitia SOLO
        # en _step==0, y la espera al modelo entre pasos (el tramo mas largo
        # del turno real del 2026-10-11) se veia como «Ejecutando comando…»
        # de un comando que ya habia terminado. Ahora va en TODOS los pasos
        # («Pensando el próximo paso… (paso N)» despues de un resultado), con
        # el mismo presupuesto "thinking". Solo se REGISTRA la duracion del
        # paso 0, como siempre: no se cambia que se mide, para no mezclar en
        # la estimacion el primer paso (prompt entero) con los siguientes.
        thinking_start = time.monotonic() if _step == 0 else None
        # to_thread: get_budget_ms lee tool_timings.json del disco en
        # cada llamada (sin cache en memoria) -- sin esto bloqueaba el
        # event loop compartido por I/O de disco chica pero constante.
        budget_ms, has_history = await asyncio.to_thread(get_budget_ms, "thinking")
        yield {
            "type": "progress-estimate",
            "data": {
                "id": "thinking",
                "operation": "thinking",
                "label": _thinking_label(_step, turn_had_tools),
                "step": _step + 1,
                "budget_ms": budget_ms,
                "has_history": has_history,
            },
        }
        # REQ-003 (spec fix-detener-y-progreso-visible): avisos de «escribiendo los argumentos» de este paso.
        writing_notices = _ToolWritingNotices()

        # Reintenta UNA vez ante 401: el token cacheado del SDK del proveedor de IA
        # puede invalidarse antes de su TTL nominal (revocacion de sesion
        # Azure AD, Conditional Access, etc. -- ver la documentacion del proveedor,
        # references/troubleshooting.md "401 / access token errors"). En
        # ese caso se fuerza un cliente con token nuevo y se reintenta el
        # mismo request antes de rendirse y mostrarle el error al usuario.
        # REQ-002 (spec fix-corte-proveedor): y repite UNA vez el paso si el proveedor de IA corta la conexion a mitad de la
        # respuesta antes de que el usuario haya visto texto de este paso (caso real 2026-10-14: RemoteProtocolError
        # 2 min 30 s despues de empezar a responder). Son dos reintentos independientes -- por eso banderas propias en
        # vez del `range(2)` de antes --: el del corte NO pide token nuevo y es con el MISMO modelo (no pasa por el
        # salto automatico: un corte no es un limite ni una caida del modelo, ver model_fallback.TRIGGER_CODES).
        refresh_token = False
        retried_401 = False
        retried_cut = False
        # REQ-003 (spec fix-corte-proveedor): estado de TURNO que un intento cortado pudo tocar sin mostrar nada (un
        # marcador `[[TIPO:` a medio llegar); el reintento arranca desde aca, como si el intento cortado no hubiera existido.
        carry_at_step_start, response_type_at_step_start = _carry, response_type
        while True:
            # to_thread: en cache-miss (cada ~30min, o al forzar refresh tras
            # un 401), get_client hace un POST sincronico de OAuth a Azure AD
            # -- bloquearia el event loop entero mientras dura ese request.
            try:
                # spec varios-proveedores REQ-005: el cliente del proveedor de
                # ESTE paso (el salto puede cambiar de proveedor a mitad del turno).
                client = await asyncio.to_thread(get_client, force_refresh=refresh_token, model=active_model)
            except Exception as exc:  # noqa: BLE001 -- login/red ANTES de hablar con el modelo
                # Antes quedaba fuera de todo try: sin VPN o con el login de
                # el proveedor de IA roto, la excepcion subia hasta el turno en background
                # y el chat terminaba EN SILENCIO (sin ningun mensaje).
                yield _model_error_event(
                    exc, active_model, session.messages, "No se pudo preparar el cliente del proveedor de IA"
                )
                return
            stream = _stream_chat_completion(
                client,
                session,
                model=llm_providers.api_model(active_model),
                # REQ-005 (spec salto-automatico-modelos): copia con ids de
                # tool-call que el modelo destino acepta; session.messages no
                # se toca (Gemini necesita los suyos originales).
                messages=history_for_model(session.messages, model_fallback.resolve(active_model, catalog)[0]),
                # spec modo-plan-loop REQ-002/003: en Modo Plan, write/edit/
                # bash quedan AUSENTES del payload -- se resuelve en CADA
                # llamada (no una vez al arrancar el turno) por si el modo
                # cambia a mitad de un turno largo de varias vueltas.
                # final_step: se pasa SIN tools para que el modelo concluya
                # en texto (ver comentario arriba) y el turno no muera
                # agotando MAX_STEPS con un error.
                tools=(None if final_step else registry.definitions(exclude_dangerous=session.mode == "plan")),
                stream=True,
                **extra_params,
            )

            received_chunk = False
            try:
                async for chunk in stream:
                    received_chunk = True
                    # REQ-001/004: chequeo de stop TAMBIEN durante el
                    # streaming de texto (no solo entre pasos). Es seguro
                    # cortar aca porque mientras el modelo esta emitiendo
                    # texto/tool_calls en streaming NO hay ninguna tool
                    # real ejecutandose todavia (eso pasa recien despues,
                    # en el asyncio.gather de mas abajo) -- no hay ningun
                    # proceso "a medio cerrar" del que preocuparse en este
                    # punto especifico.
                    if session.stop_requested:
                        stopped_mid_stream = True
                        break

                    choice = chunk.choices[0] if chunk.choices else None
                    if choice is None:
                        continue
                    delta = choice.delta
                    finish_reason = choice.finish_reason or finish_reason

                    if delta and delta.content:
                        # REQ-002 (spec clasificador-tipo-respuesta): filtra
                        # el marcador `[[TIPO: xxx]]` de este chunk (ver
                        # `_filter_marker_chunk`) -- el usuario NUNCA debe ver
                        # el marcador crudo, sin importar cuantas veces el
                        # modelo lo re-emita en este turno ni si cae partido
                        # entre dos chunks.
                        chunk_text, _carry, found_type = _filter_marker_chunk(_carry, delta.content)
                        if found_type and response_type is None:
                            response_type = found_type
                            yield {
                                "type": "response-type",
                                "data": {"response_type": response_type},
                            }
                        if not chunk_text:
                            continue
                        if not text_part_started:
                            # Investigacion de rendimiento 2026-09: marca el
                            # TTFT (time-to-first-token) real de este turno --
                            # separa "tardo el modelo" de "tardo Vision antes
                            # de llamarlo" (ver perf_log.py).
                            perf.mark_first_token()
                            # REQ-002: insert al INICIAR el part de texto
                            # (mensaje 'assistant' + part 'text' vacio) --
                            # si el proceso crashea a mitad del streaming,
                            # ya queda registrado que el turno arranco.
                            if not assistant_msg_started:
                                persistence.record_message_start(session_id, assistant_msg_id, "assistant")
                                assistant_msg_started = True
                            persistence.record_part_start(
                                session_id,
                                assistant_msg_id,
                                text_part_id,
                                "text",
                                {"text": ""},
                            )
                            text_part_started = True
                            # REQ-002 (spec clasificador-tipo-respuesta): si el
                            # marcador ya se resolvio antes del primer del text,
                            # persistirlo en la fila `message.response_type` para
                            # que el badge sobreviva a una recarga del historial.
                            if response_type:
                                persistence.update_message_response_type(session_id, assistant_msg_id, response_type)
                        assistant_text += chunk_text
                        yield {"type": "text-delta", "data": {"text": chunk_text}}

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            entry = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                entry["id"] = tc.id
                                if entry["id"] not in tool_part_ids:
                                    # REQ-002: insert al conocerse el id de
                                    # la tool-call (primer fragmento) --
                                    # el nombre/argumentos todavia pueden
                                    # llegar incompletos, se actualizan
                                    # despues del REQ-005 de tool-result.
                                    if not assistant_msg_started:
                                        persistence.record_message_start(session_id, assistant_msg_id, "assistant")
                                        assistant_msg_started = True
                                    part_id = str(uuid.uuid4())
                                    tool_part_ids[entry["id"]] = part_id
                                    persistence.record_part_start(
                                        session_id,
                                        assistant_msg_id,
                                        part_id,
                                        "tool",
                                        {
                                            "call_id": entry["id"],
                                            "name": "",
                                            "arguments": "",
                                        },
                                    )
                            if tc.function:
                                if tc.function.name:
                                    entry["name"] += tc.function.name
                                if tc.function.arguments:
                                    entry["arguments"] += tc.function.arguments
                                    # REQ-008: recorrido en vivo del codigo
                                    # mientras el modelo lo escribe.
                                    if narrate and entry["name"]:
                                        narrator = live_narrators.setdefault(
                                            idx, LiveNarrator(entry["name"], level=narrate_level)
                                        )
                                        for spoken in narrator.feed(tc.function.arguments):
                                            yield {
                                                "type": "narration",
                                                "data": {
                                                    "text": spoken,
                                                    "call_id": entry["id"],
                                                    # REQ-015: la presentacion del diseno no se descarta.
                                                    "lead": spoken == narrator.lead_phrase,
                                                },
                                            }
                                # REQ-003 (spec fix-detener-y-progreso-visible): «Escribiendo <archivo>… N KB»
                                # mientras llegan los argumentos (uno al conocerse el nombre, despues a lo sumo
                                # cada TOOL_WRITING_INTERVAL_S). No toca la persistencia ni la narracion de arriba.
                                writing_event = writing_notices.event_for(idx, entry)
                                if writing_event is not None:
                                    yield writing_event
                else:
                    # `for...else`: corre si el loop termino SIN pasar por
                    # el `break` de arriba -- incluye el caso nuevo donde
                    # `_stream_chat_completion` cerro la conexion por un
                    # stop pedido ANTES de que llegara el primer chunk (el
                    # generador termina sin haber yielded nada, entonces el
                    # chequeo de `stop_requested` de DENTRO del loop nunca
                    # se ejecuto). Sin este chequeo aca, ese stop se perdia
                    # silenciosamente y el turno seguia como si el modelo
                    # hubiese respondido vacio.
                    if session.stop_requested:
                        stopped_mid_stream = True
                break  # stream completo sin error -> no reintentar
            except openai.AuthenticationError as exc:
                if not retried_401:
                    retried_401 = True
                    refresh_token = True
                    logger.warning(
                        "el proveedor de IA devolvio 401, reintentando con token refrescado: %s",
                        exc,
                    )
                    assistant_text = ""
                    tool_calls_acc = {}
                    live_narrators = {}
                    writing_notices = _ToolWritingNotices()
                    continue
                yield _model_error_event(
                    exc,
                    active_model,
                    session.messages,
                    "el proveedor de IA sigue devolviendo 401 tras refrescar el token",
                )
                return
            except Exception as exc:  # noqa: BLE001 -- se loguea con traceback en _model_error_event
                # spec salto-automatico-modelos REQ-002/008: si el modelo fallo
                # por limite o caida ANTES de empezar a responder, queda en
                # pausa y el MISMO paso se repite con el siguiente de la lista.
                # Si ya habia empezado a escribir no se cambia de modelo a mitad
                # de una respuesta: se muestra el error como siempre.
                if picker is not None and not received_chunk and picker.register_failure(active_model, exc):
                    logger.warning(
                        "Salto automatico: %s fallo (%s); sigue el proximo de la lista",
                        active_model,
                        exc,
                    )
                    jump_to_next_model = True
                    break
                # REQ-002/003 (spec fix-corte-proveedor): el proveedor de IA corto la conexion a mitad de la respuesta. Se repite el
                # paso UNA vez solo si el usuario todavia no vio texto de este paso (`text_part_started`: se marca justo
                # antes del primer `text-delta`; repetir despues duplicaria lo que ya leyo) y no apreto Detener. Puede
                # haber quedado una tool-call a medio llegar: nunca se ejecuto (las tools corren recien con el stream
                # completo), asi que basta con olvidarla aca y en SQLite.
                if (
                    not retried_cut
                    and not text_part_started
                    and not session.stop_requested
                    and describe_model_error(exc, active_model).code == "MODELO-CORTE"
                ):
                    retried_cut = True
                    refresh_token = False  # el token no tuvo nada que ver
                    logger.warning(
                        "Corte del proveedor de IA a mitad de la respuesta; reintento 1 (modelo %s): %s",
                        active_model,
                        exc,
                    )
                    if assistant_msg_started:
                        # Sin texto, la fila solo tiene las tool-calls del intento cortado: se borra (sus parts caen
                        # en cascada) y el reintento usa un id nuevo, asi no quedan parts huerfanas ni dobles. La cola
                        # de escritura es una sola y en orden: el borrado llega antes que lo que inserte el reintento.
                        persistence.delete_message(session_id, assistant_msg_id)
                        assistant_msg_id = str(uuid.uuid4())
                        assistant_msg_started = False
                    tool_part_ids = {}
                    assistant_text = ""
                    tool_calls_acc = {}
                    live_narrators = {}
                    writing_notices = _ToolWritingNotices()
                    finish_reason = None
                    _carry, response_type = carry_at_step_start, response_type_at_step_start
                    continue
                yield _model_error_event(exc, active_model, session.messages, "Error llamando al proveedor de IA")
                return

        if jump_to_next_model:
            continue  # el proximo paso elige otro modelo (picker.choose) y avisa
        if picker is not None:
            picker.mark_used(active_model)

        # REQ-001: registra cuanto tardo REALMENTE el "pensar" de este
        # primer paso (con o sin tool-calls) -- se hace ANTES de los
        # returns/continue de abajo para no duplicarlo, y solo si de verdad
        # se midio (_step==0).
        # REQ-006 (spec fix-detener-y-progreso-visible): un "pensar" CORTADO
        # (Detener o edicion a mitad del stream) no es una duracion real y no
        # se registra: ensuciaba la estimacion de los proximos turnos.
        if thinking_start is not None and not stopped_mid_stream:
            await asyncio.to_thread(record_duration, "thinking", (time.monotonic() - thinking_start) * 1000)

        if stopped_mid_stream:
            # Se corto el streaming de texto por un STOP/edicion -- el
            # texto parcial generado hasta ahora (si hubo) se descarta del
            # historial real (no se agrega un mensaje assistant a medias,
            # eso corromperia la conversacion que ve el LLM en el proximo
            # turno). Se re-chequea `edit_pending_text` para diferenciar
            # STOP real de edicion, igual que en el chequeo de arriba.
            if session.edit_pending_text is not None:
                yield _apply_message_edit(session)
                continue
            yield {"type": "stopped", "data": {"reason": "user_requested"}}
            return

        # REQ-002 (spec clasificador-tipo-respuesta): si el modelo NO emitio el
        # marcador (tipico en turnos que solo generan tool_calls y el texto
        # final viene una vuelta despues, sin marker), infierir el tipo de forma
        # LOCAL y barata (cero llamadas extra) y emitir/registrar el
        # response_type para que el badge muestre algo sensato.
        if response_type is None:
            response_type = "planificable" if turn_had_tools else "conversacional"
            yield {"type": "response-type", "data": {"response_type": response_type}}
            if assistant_msg_started:  # la fila existe (con texto o solo con tools)
                persistence.update_message_response_type(session_id, assistant_msg_id, response_type)

        if not tool_calls_acc:
            # Bug real 2026-09-21 (sesion "no ejecute la busqueda" con
            # NGAVS -- el usuario reporto una cadena de 10+ mensajes
            # disculpandose y prometiendo "ahora si ejecuto la busqueda"
            # sin que NADA forzara la ejecucion real): el modelo puede
            # clasificarse a si mismo como `planificable` (que EXIGE
            # `todowrite` como primera tool-call, ver REGLA N 1 del
            # prompt) y aun asi terminar el paso sin llamar a NINGUNA
            # tool en todo el turno -- antes esto se aceptaba directo
            # como turno "done" exitoso, y la unica forma de que algo
            # cambiara era que el USUARIO reescribiera el mensaje a mano.
            # `turn_had_tools` (acumulado a lo largo de TODO el turno, no
            # solo este paso) es la señal estructural exacta de "nunca se
            # ejecuto nada real todavia" -- no hace falta parsear el texto
            # en busca de frases como "voy a"/"ahora si" (fragil, atado al
            # idioma/estilo del modelo).
            empty_promise = response_type == "planificable" and not turn_had_tools and not final_step
            if empty_promise and empty_promise_retries < _MAX_EMPTY_PROMISE_RETRIES:
                empty_promise_retries += 1
                session.messages.append({"role": "assistant", "content": assistant_text})
                if text_part_started:
                    persistence.record_part_end(
                        session_id,
                        text_part_id,
                        {"text": assistant_text, "response_type": response_type},
                    )
                # Se inyecta como mensaje 'system' (no 'user' -- nunca debe
                # aparentar que el usuario escribio esto) para que la
                # PROXIMA llamada al modelo, en el mismo turno, vea la
                # correccion y no pueda repetir la misma promesa vacia sin
                # violar una instruccion explicita.
                session.messages.append(_empty_promise_correction())
                yield {
                    "type": "system-note",
                    "data": {
                        "text": (
                            "Vision dijo que iba a hacer algo pero no llegó a "
                            "hacerlo; se lo vuelvo a pedir automáticamente."
                        )
                    },
                }
                continue
            if empty_promise:
                # Se agoto el margen de reintentos automaticos y el modelo
                # sigue sin ejecutar nada real -- se corta el bucle en vez
                # de reintentar para siempre y se le avisa EXPLICITAMENTE
                # al usuario (no un "done" silencioso identico a una
                # respuesta normal) para que sepa que hizo falta
                # intervencion humana.
                yield {
                    "type": "system-note",
                    "data": {
                        "text": (
                            "Vision dijo varias veces que iba a hacer algo pero no "
                            "llegó a hacerlo. Volvé a enviar tu pedido, o pedilo de "
                            "forma más concreta (por ejemplo, qué dato o archivo "
                            "necesitás)."
                        )
                    },
                }
            # Turno normal sin tool calls: guarda el mensaje y termina.
            session.messages.append({"role": "assistant", "content": assistant_text})
            if text_part_started:
                # REQ-002: update FINAL del part de texto, con el
                # contenido completo acumulado (los deltas individuales
                # nunca se persisten, igual que OpenCode no persiste
                # PartDelta -- solo el resultado final del part).
                persistence.record_part_end(
                    session_id,
                    text_part_id,
                    {"text": assistant_text, "response_type": response_type},
                )
            yield {"type": "done", "data": {"reason": finish_reason or "stop"}}
            return

        # Hubo tool calls: arma el mensaje 'assistant' con tool_calls (formato
        # OpenAI) y ejecuta todas en paralelo (equivalente a los fibers
        # concurrentes de OpenCode, ver runner/llm.ts).
        turn_had_tools = True
        ordered_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
        # spec informe-tecnico-hablado REQ-008: si una tool-call ya se narro en
        # vivo (write/edit/bash), su frase de accion no se repite al ejecutarla.
        live_narrated_ids = {
            tool_calls_acc[i]["id"] for i, n in live_narrators.items() if n.narrated and i in tool_calls_acc
        }
        assistant_msg = _assistant_tool_message(assistant_text, ordered_calls)
        session.messages.append(assistant_msg)

        if text_part_started and assistant_text:
            # REQ-002: el modelo tambien emitio texto ANTES de pedir las
            # tool-calls (ej. "Dejame revisar eso...") -- se cierra ese
            # part de texto con el contenido final antes de seguir.
            persistence.record_part_end(session_id, text_part_id, {"text": assistant_text})

        for call in ordered_calls:
            # REQ-002: update del part 'tool' con nombre/argumentos ya
            # completos (durante el streaming solo se sabia el id) --
            # el resultado real se agrega despues de ejecutar (mas abajo).
            part_id = tool_part_ids.get(call["id"])
            if part_id:
                persistence.record_part_end(
                    session_id,
                    part_id,
                    {
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": call["arguments"],
                    },
                )
            yield {
                "type": "tool-call",
                "data": {
                    "id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                },
            }
            # REQ-002/004/005 (spec barra-progreso-adaptativa): presupuesto
            # estimado ANTES de que la tool termine, en base al promedio
            # historico de esta operacion puntual (o modo indeterminado si
            # es la primera vez que se usa). Para la tool `skill`, se usa
            # la clave especifica 'skill:<nombre>' (mismo criterio que
            # app/tools/skill.py al registrar la duracion real), leyendo
            # el nombre de la skill de los argumentos ya parseados.
            operation_key = _operation_key(call)
            budget_ms, has_history = await asyncio.to_thread(get_budget_ms, operation_key)
            yield {
                "type": "progress-estimate",
                "data": {
                    "id": call["id"],
                    "operation": operation_key,
                    "label": describe_operation(operation_key),
                    # spec informe-tecnico-hablado REQ-002/003: frase del
                    # informe tecnico (None si no es una accion); el frontend
                    # la dice en vez de `label` si el interruptor esta prendido.
                    "speech": None
                    if call["id"] in live_narrated_ids
                    else describe_call(call["name"], call["arguments"]),
                    "budget_ms": budget_ms,
                    "has_history": has_history,
                },
            }

        # REQ-009 (spec asistente-contextual-voz): si este turno viene del
        # modo Cerebro (voz continua) y NO es ya la resolucion de una
        # confirmacion pendiente (`voice_confirm_bypass`), cualquier
        # tool-call `dangerous=True` (bash/write/edit) se INTERCEPTA aca
        # -- nunca llega a `registry.dispatch`. En vez de ejecutarla, se
        # le devuelve al modelo un resultado de tool que le indica
        # explicitamente que debe preguntarle al usuario "¿estas seguro?"
        # en su respuesta en texto (que el frontend lee por TTS) y se
        # guarda la tool pendiente en `session.voice_pending_confirmation`
        # para que el PROXIMO turno de voz (la respuesta del usuario) la
        # resuelva (ver arriba). Solo la PRIMERA tool peligrosa del paso
        # se pausa (si el modelo pidio varias en paralelo, las demas
        # -no peligrosas- siguen su curso normal); si hay mas de una
        # peligrosa en el mismo paso, las siguientes tambien quedan
        # pausadas con el mismo mensaje (evita ejecutar unas si y otras no
        # dentro del mismo lote, lo cual seria confuso para el usuario).
        if voice_confirm and not voice_confirm_bypass:
            for call in ordered_calls:
                tool_def = registry.get(call["name"])
                if tool_def is not None and tool_def.dangerous:
                    session.voice_pending_confirmation = {
                        "name": call["name"],
                        "arguments": call["arguments"],
                    }

            # NOTA (ver Decisiones registradas en la spec): no hay ningun
            # mecanismo de cache de tool-results aca. El "no perder progreso"
            # de REQ-004 se logra ANTES de este punto -- mutando el mensaje
            # user en el lugar y dejando el historial (incluidos los
            # tool_calls/resultados de vueltas anteriores DEL MISMO turno)
            # intacto, para que el propio LLM vea que datos ya obtuvo y
            # decida si necesita pedirlos de nuevo o no.

        async def _dispatch_or_intercept(call: dict[str, Any]) -> str:
            tool_def = registry.get(call["name"])
            name = call["name"]
            # REGLA N°1 HARD (pedido del usuario 2026-09, "SIEMPRE planificar
            # primero"; relajada 2026-09 por investigacion de rendimiento --
            # "sacar planificar primero solo para consultas simples, el resto
            # las planifica automatico"): si se quiere ejecutar una tool de
            # CONECTOR/ACCION y todavia NO hay plan cargado en session.todos,
            # se INTERCEPTA (no se ejecuta) y se le ordena al modelo que
            # primero cargue el plan con `todowrite` -- PERO SOLO si el pedido
            # no fue clasificado como simple. Reusa la clasificacion que YA
            # existe (spec clasificador-tipo-respuesta, `response_type`,
            # marcador `[[TIPO: ...]]` que el modelo emite como primer token)
            # en vez de armar un mecanismo de deteccion aparte: `analitica_simple`
            # es EXACTAMENTE "un pedido que se resuelve con, a lo sumo, UNA
            # sola consulta/tool" -- si el modelo ya se autoclasifico asi (o
            # como `conversacional`), no tiene sentido forzarlo a planificar
            # antes de esa unica consulta. Default seguro: si todavia no hay
            # clasificacion conocida (`response_type is None`, caso raro de un
            # tool-call sin texto previo) o es `planificable`, se mantiene el
            # gate de siempre. Las tools de exploracion/lectura no-destructivas
            # (read/grep/glob/skill/todowrite/question) nunca se bloquean.
            # (B023 es falso positivo aca: este closure se crea y se await-ea
            # via asyncio.gather mas abajo dentro de la MISMA iteracion de
            # `for _step in range(MAX_STEPS)`, antes de que response_type
            # pueda cambiar de nuevo -- no hay ejecucion diferida que
            # capture un valor stale.)
            if (
                name in _PLAN_FIRST_TOOLS
                and not (session.todos)
                and response_type not in ("conversacional", "analitica_simple")  # noqa: B023
            ):
                return _plan_first_message(name)
            if voice_confirm and not voice_confirm_bypass and tool_def is not None and tool_def.dangerous:
                # REQ-009: nunca se ejecuta -- el resultado que "ve" el
                # modelo es una instruccion, no el resultado real de la
                # tool. Se audita igual como "confirmacion pedida, no
                # ejecutada" no es necesario (no hubo ejecucion real, la
                # auditoria de tools/base.py es solo para ejecuciones
                # efectivas -- este camino ni siquiera llama a dispatch).
                return _VOICE_CONFIRM_REQUIRED_MESSAGE
            _tool_start = time.monotonic()
            # REQ-004 (spec fix-detener-y-progreso-visible): la tool sabe el id de SU tool-call (salida en vivo del bash). Cada
            # llamada corre en su propia tarea (`_gather_tools_stoppable`), asi que el valor no se mezcla entre tools en paralelo.
            call_id_token = CURRENT_TOOL_CALL_ID.set(call["id"])
            try:
                return await registry.dispatch(call["name"], call["arguments"], session)
            finally:
                CURRENT_TOOL_CALL_ID.reset(call_id_token)
                perf.record_tool_call(name, (time.monotonic() - _tool_start) * 1000)

        results = await _gather_tools_stoppable(session, [_dispatch_or_intercept(call) for call in ordered_calls])

        for call, result in zip(ordered_calls, results, strict=True):
            session.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            part_id = tool_part_ids.get(call["id"])
            if part_id:
                # REQ-002: update FINAL del part 'tool' con el resultado
                # ya obtenido -- este es el momento en que ese part queda
                # "completo" en disco (mismo criterio que el ejemplo real
                # de export a Markdown provisto por el usuario).
                persistence.record_part_end(
                    session_id,
                    part_id,
                    {
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": call["arguments"],
                        "result": result,
                    },
                )
            yield {
                "type": "tool-result",
                "data": {
                    "id": call["id"],
                    "name": call["name"],
                    "result": result[:4000],
                },
            }

        # Continua el loop: vuelve a llamar al LLM con los resultados de
        # las tools ya agregados al historial (needsContinuation).

        if _step > 0 and _step % WARN_STEPS == 0:
            yield {
                "type": "system-note",
                "data": {
                    "text": f"Vision lleva {_step} pasos seguidos trabajando en esto. "
                    "Si parece trabado, podés frenarlo con el botón Detener."
                },
            }

    step_limit = describe_step_limit(MAX_STEPS)
    logger.warning("Turno cortado por MAX_STEPS=%d (sesion %s)", MAX_STEPS, session_id)
    yield {
        "type": "error",
        "data": {
            "message": step_limit.message,
            "code": step_limit.code,
            "technical": step_limit.technical(when=datetime.now().astimezone().strftime("%H:%M:%S")),
        },
    }
