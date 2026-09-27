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
from typing import Any

import openai

from app.browser_workspace import get_or_create_browser_dir
from app.config import get_settings
from app.friendly_errors import FriendlyError, describe_model_error, describe_step_limit
from app.llm import model_fallback
from app.llm.client import get_client
from app.llm.diagnostics import describe_history_shape
from app.llm.history_compat import history_for_model
from app.perf_log import TurnPerfRecorder
from app.persistence import service as persistence
from app.project_context import build_project_context_prompt
from app.session.events import SessionState
from app.tool_timings import describe_operation, get_budget_ms, record_duration
from app.tools import get_registry
from app.tools.skill import list_available_skills_xml

logger = logging.getLogger(__name__)

MAX_STEPS = 100
WARN_STEPS = 20

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
    if (
        fixed_idx != -1
        and "]]" not in buf[fixed_idx:]
        and len(buf) - fixed_idx < _MARKER_HOLDBACK_THRESHOLD
    ):
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
    logger.warning("Turno sin modelo disponible [%s · %s]: %s", friendly.code, when, friendly.message)
    return {
        "type": "error",
        "data": {
            "message": friendly.message,
            "code": friendly.code,
            "technical": friendly.technical(model=model, when=when),
        },
    }


def _extra_params_for(
    model: str, generation_params: dict[str, Any] | None, catalog: dict[str, dict[str, Any]]
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
 Ese contenido se muestra en el panel "Navegador" de los accesos quick.
- IMPORTANTE (no lo silencies ni simules que ya se mostro): VOS NO PODES ABRIR ese panel -- \
 es UI del frontend que solo el usuario abre/cierra con el boton "Navegador" del panel de \
 accesos rapidos (o que el propio frontend abre solo cuando hay contenido nuevo). Cuando \
 termines de escribir el contenido, DECILE al usuario que lo abra/revise con el boton \
 "Navegador" en los accesos rapidos (no digas "ya se abrio" ni "ya esta visible"). El panel se \
 auto-actualiza solo una vez abierto; explicale que si no lo ve, presione el boton "Navegador".
- Ese contenido es TEMPORAL: vive mientras dure esta sesion de chat: se borra si el usuario \
 elimina la sesion o libera el cache. No es el lugar para guardar nada que deba persistir.

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

{project_context_block}"""


def _build_system_prompt(session_id: str) -> str:
    skills_xml = list_available_skills_xml(session_id)
    skills_block = (
        skills_xml
        if skills_xml
        else "(no hay skills locales cargadas en esta instalacion)"
    )
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
    # spec vision-generico REQ-005: el nombre de la empresa sale del .env.
    company = get_settings().vision_company_name.strip()
    return SYSTEM_PROMPT_TEMPLATE.format(
        company_clause=f" para {company}" if company else "",
        skills_block=skills_block,
        project_context_block=project_context,
        browser_dir=browser_dir,
    )


async def _stream_chat_completion(
    client, session, **kwargs
) -> AsyncGenerator[Any, None]:
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


def _filter_generation_params(
    params: dict[str, Any] | None, valid_args: list[str]
) -> dict[str, Any]:
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


async def run_turn(
    session: SessionState,
    user_message: str,
    session_id: str,
    model: str | None = None,
    generation_params: dict[str, Any] | None = None,
    user_message_id: str | None = None,
    images: list[str] | None = None,
    voice_confirm: bool = False,
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
    unico punto de entrada/salida es mas seguro."""
    perf = TurnPerfRecorder(session_id, model or get_settings().llm_model)
    status = "done"
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
        ):
            yield event
    except BaseException:
        status = "error"
        raise
    finally:
        await asyncio.to_thread(perf.finish, status)


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
    settings = get_settings()
    registry = get_registry()

    # Catalogo de VisionLLM (valid_args de cada modelo, alias, aptitud para datos
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

    # spec salto-automatico-modelos: con el salto activo, el modelo de CADA paso
    # lo elige el picker (REQ-002/003/004/009): el elegido a mano en el selector,
    # o el primero de la lista en modo Automatico, y si esta en pausa el
    # siguiente. Apagado (default), todo sigue como siempre (REQ-006).
    fallback_settings = model_fallback.load_settings()
    picker = (
        model_fallback.TurnModelPicker(fallback_settings, model, settings.llm_model, catalog)
        if fallback_settings.enabled
        else None
    )
    active_model = picker.home if picker is not None else (model or settings.llm_model)
    extra_params = _extra_params_for(active_model, generation_params, catalog)

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
        session.messages.insert(
            0, {"role": "system", "content": _build_system_prompt(session_id)}
        )
        perf.mark_system_prompt_built((time.monotonic() - _sysprompt_start) * 1000)

    user_msg_id = user_message_id or str(uuid.uuid4())
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
    persistence.record_message_start(session_id, user_msg_id, "user")
    persistence.record_part_start(
        session_id, user_msg_id, str(uuid.uuid4()), "text", {"text": user_message}
    )

    if images:
        # REQ-006/008: content en formato lista -- un bloque de texto (aun
        # si esta vacio, se omite si no hay texto: mandar {"type":"text",
        # "text":""} junto a imagenes es valido pero innecesario) seguido
        # de un bloque image_url por imagen, SIEMPRE base64 inline (nunca
        # URL externa -- esa variante es exclusiva de OpenAI, rechazada
        # por Gemini/Claude/on-prem segun la la documentacion del proveedor).
        content_blocks: list[dict[str, Any]] = []
        if user_message:
            content_blocks.append({"type": "text", "text": user_message})
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
            persistence.record_part_start(
                session_id, user_msg_id, str(uuid.uuid4()), "image", {"url": data_uri}
            )
    else:
        session.messages.append({"role": "user", "content": user_message})

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
    if voice_confirm_instruction:
        # Se agrega como continuacion del ULTIMO mensaje user recien
        # insertado (arriba) -- funciona tanto para el formato string
        # plano como para el formato lista con imagenes (se agrega un
        # bloque de texto adicional en ese caso, nunca se pisa el ya
        # existente).
        last = session.messages[-1]
        if isinstance(last.get("content"), list):
            last["content"].append(
                {"type": "text", "text": voice_confirm_instruction.strip()}
            )
        else:
            last["content"] = (last.get("content") or "") + voice_confirm_instruction

    # REQ-001/004: se limpia cualquier stop pendiente de un turno anterior
    # (ej. si este mismo run_turn es el reinicio disparado por editar un
    # mensaje en curso -- ver app/main.py endpoint PATCH de mensaje).
    session.stop_requested = False
    session.edit_pending_text = None

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
        compacted = await maybe_compact(
            session_id, session.messages, context_window, compact_model
        )
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
    if compacted:
        yield {
            "type": "system-note",
            "data": {
                "text": "Resumí la parte más vieja de esta conversación para poder seguir "
                "(ya era muy larga). Lo importante se conserva."
            },
        }

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
                new_text = session.edit_pending_text
                session.stop_requested = False
                session.edit_pending_text = None
                session.replace_active_user_message(new_text)
                yield {"type": "message-edited", "data": {"text": new_text}}
                continue

            # STOP real (no edicion): no se agrega nada falso al historial
            # real de OpenAI -- el historial ya esta en un estado
            # consistente en este punto (entre pasos). El frontend muestra
            # la nota visual de "turno detenido" a partir del evento
            # 'stopped', no del historial.
            yield {"type": "stopped", "data": {"reason": "user_requested"}}
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
        finish_reason: str | None = None
        stopped_mid_stream = False
        jump_to_next_model = False

        # persistencia-sqlite-sesiones REQ-002: ids generados de antemano
        # para este paso del loop -- se insertan en SQLite recien cuando
        # hay contenido real que guardar (primer delta de texto / primera
        # tool-call conocida), nunca antes, para no dejar filas vacias.
        assistant_msg_id = str(uuid.uuid4())
        text_part_id = str(uuid.uuid4())
        text_part_started = False
        tool_part_ids: dict[str, str] = {}

        # REQ-002 (spec barra-progreso-adaptativa): estimacion generica de
        # "pensando" antes de la primera llamada al LLM de este turno --
        # solo en _step==0 (las vueltas posteriores de tool-calling ya
        # tienen su propio progress-estimate por tool, ver mas abajo). Se
        # guarda `thinking_start` para registrar la duracion real cuando
        # este primer paso termine (con o sin tool-calls).
        thinking_start = None
        if _step == 0:
            thinking_start = time.monotonic()
            # to_thread: get_budget_ms lee tool_timings.json del disco en
            # cada llamada (sin cache en memoria) -- sin esto bloqueaba el
            # event loop compartido por I/O de disco chica pero constante.
            budget_ms, has_history = await asyncio.to_thread(get_budget_ms, "thinking")
            yield {
                "type": "progress-estimate",
                "data": {
                    "id": "thinking",
                    "operation": "thinking",
                    "label": "Pensando…",
                    "budget_ms": budget_ms,
                    "has_history": has_history,
                },
            }

        # Reintenta UNA vez ante 401: el token cacheado del SDK del proveedor de IA
        # puede invalidarse antes de su TTL nominal (revocacion de sesion
        # Azure AD, Conditional Access, etc. -- ver la documentacion del proveedor,
        # references/troubleshooting.md "401 / access token errors"). En
        # ese caso se fuerza un cliente con token nuevo y se reintenta el
        # mismo request antes de rendirse y mostrarle el error al usuario.
        for attempt in range(2):
            # to_thread: en cache-miss (cada ~30min, o al forzar refresh tras
            # un 401), get_client hace un POST sincronico de OAuth a Azure AD
            # -- bloquearia el event loop entero mientras dura ese request.
            try:
                client = await asyncio.to_thread(get_client, force_refresh=(attempt == 1))
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
                model=active_model,
                # REQ-005 (spec salto-automatico-modelos): copia con ids de
                # tool-call que el modelo destino acepta; session.messages no
                # se toca (Gemini necesita los suyos originales).
                messages=history_for_model(
                    session.messages, model_fallback.resolve(active_model, catalog)[0]
                ),
                # spec modo-plan-loop REQ-002/003: en Modo Plan, write/edit/
                # bash quedan AUSENTES del payload -- se resuelve en CADA
                # llamada (no una vez al arrancar el turno) por si el modo
                # cambia a mitad de un turno largo de varias vueltas.
                # final_step: se pasa SIN tools para que el modelo concluya
                # en texto (ver comentario arriba) y el turno no muera
                # agotando MAX_STEPS con un error.
                tools=(
                    None
                    if final_step
                    else registry.definitions(exclude_dangerous=session.mode == "plan")
                ),
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
                        chunk_text, _carry, found_type = _filter_marker_chunk(
                            _carry, delta.content
                        )
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
                            persistence.record_message_start(
                                session_id, assistant_msg_id, "assistant"
                            )
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
                                persistence.update_message_response_type(
                                    session_id, assistant_msg_id, response_type
                                )
                        assistant_text += chunk_text
                        yield {"type": "text-delta", "data": {"text": chunk_text}}

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            entry = tool_calls_acc.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.id:
                                entry["id"] = tc.id
                                if entry["id"] not in tool_part_ids:
                                    # REQ-002: insert al conocerse el id de
                                    # la tool-call (primer fragmento) --
                                    # el nombre/argumentos todavia pueden
                                    # llegar incompletos, se actualizan
                                    # despues del REQ-005 de tool-result.
                                    if not text_part_started:
                                        persistence.record_message_start(
                                            session_id, assistant_msg_id, "assistant"
                                        )
                                        text_part_started = True
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
                if attempt == 0:
                    logger.warning(
                        "el proveedor de IA devolvio 401, reintentando con token refrescado: %s",
                        exc,
                    )
                    assistant_text = ""
                    tool_calls_acc = {}
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
                yield _model_error_event(
                    exc, active_model, session.messages, "Error llamando al proveedor de IA"
                )
                return

        if jump_to_next_model:
            continue  # el proximo paso elige otro modelo (picker.choose) y avisa
        if picker is not None:
            picker.mark_used(active_model)

        # REQ-001: registra cuanto tardo REALMENTE el "pensar" de este
        # primer paso (con o sin tool-calls, terminado o interrumpido) --
        # se hace ANTES de los returns/continue de abajo para no
        # duplicarlo, y solo si de verdad se midio (_step==0).
        if thinking_start is not None:
            await asyncio.to_thread(
                record_duration, "thinking", (time.monotonic() - thinking_start) * 1000
            )

        if stopped_mid_stream:
            # Se corto el streaming de texto por un STOP/edicion -- el
            # texto parcial generado hasta ahora (si hubo) se descarta del
            # historial real (no se agrega un mensaje assistant a medias,
            # eso corromperia la conversacion que ve el LLM en el proximo
            # turno). Se re-chequea `edit_pending_text` para diferenciar
            # STOP real de edicion, igual que en el chequeo de arriba.
            if session.edit_pending_text is not None:
                new_text = session.edit_pending_text
                session.stop_requested = False
                session.edit_pending_text = None
                session.replace_active_user_message(new_text)
                yield {"type": "message-edited", "data": {"text": new_text}}
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
            if text_part_started:
                persistence.update_message_response_type(
                    session_id, assistant_msg_id, response_type
                )

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
            empty_promise = (
                response_type == "planificable"
                and not turn_had_tools
                and not final_step
            )
            if empty_promise and empty_promise_retries < _MAX_EMPTY_PROMISE_RETRIES:
                empty_promise_retries += 1
                session.messages.append(
                    {"role": "assistant", "content": assistant_text}
                )
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
                session.messages.append(
                    {
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
                )
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
        assistant_msg: dict[str, Any] = {
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
        session.messages.append(assistant_msg)

        if text_part_started and assistant_text:
            # REQ-002: el modelo tambien emitio texto ANTES de pedir las
            # tool-calls (ej. "Dejame revisar eso...") -- se cierra ese
            # part de texto con el contenido final antes de seguir.
            persistence.record_part_end(
                session_id, text_part_id, {"text": assistant_text}
            )

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
            operation_key = call["name"]
            if operation_key == "skill":
                try:
                    skill_name = json.loads(call["arguments"] or "{}").get("name", "")
                    if skill_name:
                        operation_key = f"skill:{skill_name}"
                except (json.JSONDecodeError, AttributeError):
                    pass
            budget_ms, has_history = await asyncio.to_thread(
                get_budget_ms, operation_key
            )
            yield {
                "type": "progress-estimate",
                "data": {
                    "id": call["id"],
                    "operation": operation_key,
                    "label": describe_operation(operation_key),
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
                return (
                    f"[Regla de planificacion (pedido del usuario)] Aun no hay un "
                    f"plan de tareas cargado, y queres ejecutar `{name}`. La regla es "
                    f"inflexible: PRIMERO EL PLAN, DESPUES EJECUTA. Cargá el plan "
                    f"completo con la tool `todowrite` (los pasos concretos de ESTE "
                    f"proceso: conectarse a la fuente, analizar/ubicar las tablas, "
                    f"buscar los datos, resumir la respuesta) y recién después ejecuta "
                    f"`{name}`. No ejecutes ninguna accion antes de tener el plan."
                )
            if (
                voice_confirm
                and not voice_confirm_bypass
                and tool_def is not None
                and tool_def.dangerous
            ):
                # REQ-009: nunca se ejecuta -- el resultado que "ve" el
                # modelo es una instruccion, no el resultado real de la
                # tool. Se audita igual como "confirmacion pedida, no
                # ejecutada" no es necesario (no hubo ejecucion real, la
                # auditoria de tools/base.py es solo para ejecuciones
                # efectivas -- este camino ni siquiera llama a dispatch).
                return (
                    "[Confirmación requerida, Modo Cerebro] Esta herramienta es "
                    "potencialmente peligrosa (modifica archivos o ejecuta comandos) y "
                    "esta intervención viene de una conversación por voz. Todavía NO se "
                    "ejecutó. En tu respuesta de texto (que el usuario va a escuchar por "
                    "voz), preguntale explícitamente si confirma que querés proceder "
                    "(ej. '¿Estás seguro de que querés que haga esto?') y esperá su "
                    "respuesta antes de intentar de nuevo esta herramienta."
                )
            _tool_start = time.monotonic()
            try:
                return await registry.dispatch(call["name"], call["arguments"], session)
            finally:
                perf.record_tool_call(name, (time.monotonic() - _tool_start) * 1000)

        results = await asyncio.gather(
            *[_dispatch_or_intercept(call) for call in ordered_calls]
        )

        for call, result in zip(ordered_calls, results):
            session.messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
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
