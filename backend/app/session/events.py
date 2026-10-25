"""
Bus de eventos en memoria por sesion de chat, usado para:
    1. Que las tools `todowrite`/`question` empujen actualizaciones "fuera
       de banda" (checklist, pregunta con opciones) hacia el stream SSE.
    2. Que el turno del agente (`run_turn`) corra en una tarea de fondo
       PROPIA de la sesion, independiente del ciclo de vida de la conexion
       HTTP que lo escucha.
    3. (spec parada-y-cola-mensajes) Que un mensaje de usuario tenga un
       identificador propio (`message_id`, generado por el frontend) para
       poder detenerse (REQ-001), encolarse (REQ-003/008/009), y
       editarse/eliminarse ya sea en curso (REQ-004), en cola (REQ-005) o
       en historial pasado (REQ-010).

Punto (2) es lo que soluciona que cambiar/crear otra sesion (lo cual
recarga la pagina y corta la conexion SSE anterior) ya NO cancele el turno
en curso de la sesion anterior: antes, `run_turn` corria como una task
HIJA del generador async del endpoint (`asyncio.create_task(_run())` con
`finally: task.cancel()`), asi que cerrar la conexion mataba tambien el
turno real en el backend. Ahora `session.current_task` es una tarea
completamente independiente -- el stream HTTP solo LEE de
`session.event_queue`; si nadie esta escuchando, los eventos se acumulan
en la cola y el turno sigue corriendo hasta terminar de verdad.

Analogo simplificado (sin persistencia durable ni replay) del
`EventV2`/SSE de sesion de OpenCode (packages/server/src/handlers/event.ts).
Como el usuario definio "sin persistencia, solo memoria" para esta v1
(ver AGENTS.md), alcanza con una cola asyncio por sesion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Estados posibles de un UserTurn -- ver spec specs/parada-y-cola-mensajes/spec.md
TURN_STATUS_QUEUED = "queued"
TURN_STATUS_ACTIVE = "active"
TURN_STATUS_DONE = "done"
TURN_STATUS_CANCELLED = "cancelled"


@dataclass
class UserTurn:
    """REQ-003/004/005/008/010: representa UN mensaje de usuario (turno)
    identificable por `id` (generado por el frontend, ej. uuid), para que
    la UI pueda referenciarlo al pedir stop/editar/eliminar/reordenar,
    algo que el `session.messages` crudo (formato OpenAI, sin ids propios)
    no permite por si solo."""

    id: str
    text: str
    model: str | None = None
    generation_params: dict[str, Any] | None = None
    status: str = TURN_STATUS_QUEUED
    # Indice en `session.messages` donde arranca este turno (el mensaje
    # role=user) -- permite truncar el historial ahi si se edita/elimina
    # un turno ya viejo (REQ-010) o se necesita reiniciar el turno activo
    # tras una edicion (REQ-004).
    messages_start_index: int | None = None
    # spec contexto-multimodal REQ-004/006: data URIs base64 ya
    # comprimidas por el frontend -- viajan junto con el turno para que
    # sobrevivan si este mensaje queda EN COLA (no se pierden las
    # imagenes esperando a que termine el turno activo).
    images: list[str] | None = None
    # REQ-006/009 (spec asistente-contextual-voz, Modo Cerebro): True si
    # este turno se origino en el modo Cerebro (conversacion por voz
    # continua) -- activa la confirmacion hablada antes de ejecutar tools
    # peligrosas (ver `voice_pending_confirmation` en SessionState y el
    # manejo en run_turn/app/llm/loop.py). False (default) = comportamiento
    # normal del chat de texto, sin cambios.
    voice_confirm: bool = False
    # spec informe-tecnico-hablado REQ-008/009/010: True si el usuario tiene
    # prendido "Hablar informe tecnico" (y "Leer en voz alta"): el turno narra
    # el codigo en vivo, pide comentarios y anuncia el avance del plan.
    narrate: bool = False


@dataclass
class SessionState:
    """Estado en memoria de una conversacion activa: historial de mensajes
    (formato OpenAI chat.completions) + checklist de tareas (todowrite) +
    suscriptores de eventos SSE (turno + fuera-de-banda, ver `subscribe`/
    `push_event` mas abajo) + la tarea de fondo del turno en curso, si hay
    alguno + (spec parada-y-cola-mensajes) la cola de turnos de usuario
    pendientes y el estado de stop solicitado.

    FAN-OUT de eventos (spec parada-y-cola-mensajes, REQ-003): antes habia
    UNA sola `event_queue` compartida por TODAS las conexiones SSE de la
    sesion -- si dos conexiones estaban escuchando a la vez (ej. la
    conexion original de un turno A que sigue abierta, mas una conexion
    nueva abierta para mandar el mensaje B mientras A segue ocupado), cada
    evento se lo llevaba UNA sola de las dos (bug real detectado en
    pruebas: los eventos de A y B se REPARTIAN entre ambas conexiones en
    vez de que cada una viera el turno completo que le correspondia).
    Ahora `push_event` reparte una COPIA de cada evento a cada cola
    suscripta (`_subscriber_queues`), asi cualquier cantidad de conexiones
    viendo la misma sesion reciben el stream completo de eventos, no una
    porcion arbitraria."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    todos: list[dict[str, Any]] = field(default_factory=list)
    current_task: asyncio.Task | None = None
    _subscriber_queues: list[asyncio.Queue] = field(default_factory=list, repr=False)

    # --- persistencia-sqlite-sesiones: id propio de la sesion ------------
    # Necesario para que codigo que solo recibe `session` (ej. la tool
    # `todowrite`, que no conoce el session_id del request HTTP) pueda
    # persistir contra la fila correcta. Lo fija SessionStore.get_or_create
    # al crear/recuperar la sesion.
    session_id: str = ""

    # --- REQ-003/005/008/009: cola de turnos de usuario -----------------
    # `turns`: TODOS los turnos conocidos de la sesion (activo, en cola,
    # ya terminados), keyed por su id, para poder editar/eliminar
    # cualquiera de ellos sin importar su estado actual.
    # `queue_order`: orden de PROCESAMIENTO de los turnos aun no
    # arrancados (status == queued) -- separado de `turns` para poder
    # reordenar (REQ-008) sin tocar el diccionario de turnos en si.
    turns: dict[str, UserTurn] = field(default_factory=dict)
    queue_order: list[str] = field(default_factory=list)
    active_turn_id: str | None = None

    # --- REQ-001/002/004: estado de "stop" solicitado por el usuario ----
    # Se marca en True cuando el usuario pide detener el turno activo (via
    # STOP explicito o via editar el mensaje en curso, que reusa el mismo
    # mecanismo); el loop de tool-calling (`run_turn`) lo chequea entre
    # pasos para saber si debe cortar en vez de seguir con otra vuelta de
    # tool-calls. NO se fuerza el corte a mitad de una tool ya en
    # ejecucion (ver spec: "esperar cierre limpio").
    stop_requested: bool = False

    # --- REQ-004: cuando el stop fue disparado por una EDICION del
    # mensaje en curso (no un STOP manual), este campo guarda el nuevo
    # texto -- run_turn lo usa para mutar `session.messages` EN EL LUGAR
    # (reemplazar el contenido del mensaje role=user de este turno) y
    # continuar el MISMO turno, en vez de terminar el generador como hace
    # un STOP normal. None = fue un STOP real, no una edicion.
    edit_pending_text: str | None = None

    # --- spec modo-plan-loop REQ-001/002/003: modo activo de la sesion --
    # "plan" = write/edit/bash ausentes del array de tools (REQ-002);
    # "active" (default) = comportamiento actual de Vision, sin cambios
    # (REQ-003). Vive en SessionState (no por-turno) porque el atajo Tab
    # cambia el modo para TODA la sesion hacia adelante, no un turno
    # puntual -- mas simple que el diseño real de OpenCode (que manda el
    # agente en cada mensaje), suficiente para el alcance acotado de esta
    # spec (ver Decisiones registradas: solo se porta la capa de filtro
    # duro de tools, no el sistema completo de agentes).
    mode: str = "active"

    # --- spec modo-plan-loop REQ-006/007/009/011: estado del loop autonomo
    # `loop_active`: True mientras /loop esta ejecutando tareas
    # encadenadas. `loop_turns_count`: cuenta turnos consumidos por ESTE
    # loop (para el limite de 30, REQ-007c). `loop_paused_for_question`:
    # True cuando el loop se pauso esperando respuesta humana a una
    # question SIN opcion "(Recomendada)" (REQ-009) -- dispara la alarma
    # del lado del frontend. `loop_auto` (REQ-011): modo auto del loop --
    # acepta TODAS las opciones (recomendada o, si no, la primera; y si no
    # hay opciones, "procede con tu criterio") sin pausar ni alarmar,
    # corriendo hasta completar todas las tareas.
    loop_active: bool = False
    loop_turns_count: int = 0
    loop_paused_for_question: bool = False
    loop_auto: bool = False

    # --- REQ-009 (spec asistente-contextual-voz, Modo Cerebro): confirmacion
    # hablada pendiente antes de ejecutar una tool peligrosa -----------------
    # None = no hay ninguna confirmacion pendiente. Cuando un turno con
    # `voice_confirm=True` (ver UserTurn) intenta llamar a una tool
    # dangerous=True, `run_turn` (app/llm/loop.py) NO la ejecuta todavia --
    # guarda aca que tool/argumentos quedaron en espera y le pide al propio
    # LLM que le pregunte al usuario "¿estas seguro?" en su respuesta (que
    # se lee por TTS). El SIGUIENTE turno de voz del modo Cerebro (la
    # respuesta hablada del usuario) llega como un turno nuevo normal --
    # `run_turn` detecta que habia una confirmacion pendiente y esta vez
    # SI ejecuta la tool sin volver a pausar (ver comentario detallado en
    # loop.py). Se limpia (vuelve a None) apenas se resuelve, en cualquier
    # sentido (confirmado o no) -- nunca queda "colgado" entre sesiones de
    # modo Cerebro distintas."""
    voice_pending_confirmation: dict[str, Any] | None = None

    # spec informe-tecnico-hablado REQ-010: si el turno EN CURSO pidio el
    # informe tecnico hablado (UserTurn.narrate). La lee la tool `todowrite`
    # para anunciar el avance del plan; la fija run_turn al empezar cada turno.
    narrate: bool = False

    # --- REQ-003 (spec controles-seguridad-optimizacion-lightspeed-bigquery):
    # guardrail estructural de riesgo en BigQuery (DROP/DELETE/TRUNCATE/
    # UPDATE/MERGE/CREATE TABLE/CREATE OR REPLACE TABLE) ------------------
    # Hash (sha256) de cada consulta SQL riesgosa que YA se le mostro al
    # usuario en ESTA sesion. La tool `_bigquery_query_execute` (dbquery.py)
    # bloquea la PRIMERA vez que ve un hash nuevo (no ejecuta nada, obliga
    # al agente a usar `question` con sufijo "(Riesgo)"); si el agente
    # reintenta la MISMA consulta despues (mismo hash, ya en este set), esa
    # segunda vez SI se ejecuta -- "bloquea una vez, permite el reintento
    # tras aprobacion humana" (decision explicita del usuario, ver spec).
    # Solo en memoria, se resetea si se reinicia el backend -- consistente
    # con el resto del estado de sesion (ver AGENTS.md).
    bq_risky_seen: set[str] = field(default_factory=set)

    # --- REQ-002 (spec controles-seguridad-optimizacion-lightspeed-bigquery),
    # FIX 2026-09-21: bug real reportado por el usuario -- "el sistema de
    # seguridad de BigQuery mantiene el bloqueo... incluso tras tu
    # confirmacion". Causa raiz: `_bigquery_dry_run_check` bloqueaba SIEMPRE
    # que el costo superaba el umbral, sin ningun mecanismo de "ya lo
    # aprobaste, dejalo pasar" -- a diferencia de `bq_risky_seen` (arriba),
    # que si lo tiene. Mismo patron: hash (sha256) de cada consulta que YA
    # se le mostro al usuario como bloqueo de costo EN ESTA SESION; si el
    # agente reintenta la MISMA consulta despues, esa vez SI se ejecuta.
    # Aprobacion por SESION (no global como el cache de dry run de abajo)
    # porque es una decision humana puntual de ESA conversacion, no un dato
    # tecnico reusable entre sesiones/usuarios distintos.
    bq_cost_approved: set[str] = field(default_factory=set)

    # NOTA: el cache de dry run de BigQuery (REQ-002) NO vive aca -- la spec
    # pide explicitamente que sea GLOBAL A TODO EL PROCESO (compartido entre
    # sesiones de la misma instancia local, no solo dentro de una sesion),
    # asi que vive como diccionario a nivel de modulo en
    # app/tools/dbquery.py (`_BQ_DRY_RUN_CACHE`), no en SessionState.

    # Eventos que NO pertenecen a ningun turno puntual -- deben llegar a
    # TODAS las conexiones suscriptas, sin importar cual sea el turno
    # activo en el momento del push. Bug real encontrado en validacion
    # end-to-end (parada-y-cola-mensajes, REQ-003): "queued"/"dequeued" ya
    # llevan su propio `message_id` en `data` (ver docstring de mas abajo)
    # pero se les asignaba igual `_turn_id = self.active_turn_id` (el
    # turno A que sigue corriendo) en vez de None -- la conexion de B
    # (que espera `_turn_id in (None, B.id)`) nunca veia su propio evento
    # "queued", quedando indistinguible de "se perdio el mensaje".
    _TURNLESS_EVENT_TYPES = frozenset({"queued", "dequeued"})

    def push_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Reparte una COPIA de este evento a CADA conexion SSE suscripta
        (fan-out) -- ver docstring de la clase para el bug que esto
        corrige. Si no hay ningun suscriptor (ej. el turno corre en
        background y nadie esta mirando esta sesion en este momento), el
        evento simplemente se descarta -- no hay replay/historial de
        eventos (ver AGENTS.md: "sin persistencia, solo memoria"), pero
        eso no afecta el ESTADO real (session.messages/todos/turns), que
        sigue avanzando igual.

        Cada evento se anota con `_turn_id` = el turno activo en el
        momento del push (o None para eventos que no pertenecen a ningun
        turno puntual, ej. 'queued'/'dequeued' que ya llevan su propio
        `message_id` en `data`). Esto permite que cada conexion SSE
        filtre y solo muestre el contenido del turno que le corresponde
        (ver app/main.py `_drain_session_queue`) -- sin este filtro, una
        conexion nueva abierta para un mensaje B que se encola mientras A
        esta en curso veria TAMBIEN el contenido de A mezclado con el de
        B, que no es lo que el usuario espera de esa conexion."""
        turn_id = (
            None if event_type in self._TURNLESS_EVENT_TYPES else self.active_turn_id
        )
        event = {"type": event_type, "data": data, "_turn_id": turn_id}
        for q in self._subscriber_queues:
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        """Registra una nueva conexion SSE como suscriptora. Devolver la
        cola y usarla con `unsubscribe()` en un `finally` es
        responsabilidad de quien la consume (ver app/main.py
        `_drain_session_queue`)."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscriber_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscriber_queues:
            self._subscriber_queues.remove(q)

    @property
    def is_busy(self) -> bool:
        return self.current_task is not None and not self.current_task.done()

    # --- REQ-003/005/008/009: helpers de la cola de turnos --------------
    def enqueue_turn(self, turn: UserTurn, position: int | None = None) -> None:
        """Agrega un turno a la cola. `position=None` lo agrega al final
        (comportamiento normal al mandar un mensaje nuevo); un indice
        explicito se usa al reordenar (REQ-008)."""
        self.turns[turn.id] = turn
        if position is None:
            self.queue_order.append(turn.id)
        else:
            self.queue_order.insert(position, turn.id)

    def reorder_queue(self, turn_id: str, new_position: int) -> bool:
        """REQ-008: mueve un turno YA en cola a una nueva posicion. Devuelve
        False si el turno no esta en la cola (ya se esta procesando, o no
        existe)."""
        if turn_id not in self.queue_order:
            return False
        self.queue_order.remove(turn_id)
        new_position = max(0, min(new_position, len(self.queue_order)))
        self.queue_order.insert(new_position, turn_id)
        return True

    def dequeue_next_turn(self) -> UserTurn | None:
        """Saca y devuelve el siguiente turno a procesar (posicion 0 de la
        cola), o None si no hay ninguno esperando."""
        if not self.queue_order:
            return None
        next_id = self.queue_order.pop(0)
        return self.turns.get(next_id)

    def remove_queued_turn(self, turn_id: str) -> bool:
        """REQ-005/008: elimina un turno de la cola SIN llegar a
        procesarlo. Devuelve False si no estaba en cola (ya en curso, ya
        terminado, o inexistente)."""
        if turn_id not in self.queue_order:
            return False
        self.queue_order.remove(turn_id)
        turn = self.turns.get(turn_id)
        if turn is not None:
            turn.status = TURN_STATUS_CANCELLED
        return True

    def edit_queued_turn(self, turn_id: str, new_text: str) -> bool:
        """REQ-005: edita el texto de un turno que sigue en cola (todavia
        no arranco a procesarse). Devuelve False si no esta en cola."""
        if turn_id not in self.queue_order:
            return False
        turn = self.turns.get(turn_id)
        if turn is None:
            return False
        turn.text = new_text
        return True

    @property
    def queue_length(self) -> int:
        return len(self.queue_order)

    # --- REQ-004: mover el mensaje `user` del turno EN CURSO al final ----
    def replace_active_user_message(self, new_text: str) -> bool:
        """Busca el ULTIMO mensaje role=user en `self.messages` (el turno
        activo -- siempre es el mas reciente, porque el loop solo agrega
        un mensaje user por turno al arrancar) y lo MUEVE al final del
        historial (no lo reemplaza en su posicion original) con el texto
        editado `new_text`.

        Por que mover y no solo reemplazar en el lugar (hallazgo real
        durante el build, confirmado con Claude Sonnet Y DeepSeek): si el
        mensaje user editado queda ANTES de los `assistant`/`tool` que
        esa misma corrida ya genero (ej. el resultado de una tool-call),
        el modelo lee el historial como "primero me pediste X, RECIEN
        DESPUES yo ejecute una tool" -- orden narrativo invertido que le
        impide conectar el pedido editado con el resultado ya obtenido.
        Moviendolo al final, el modelo ve el orden logico real: "ejecute
        esta tool, obtuve este resultado, AHORA el usuario pide X" --
        y puede razonar correctamente sobre datos que ya tiene.

        Deja intactos todos los mensajes assistant/tool intermedios
        (tool-calls ya ejecutadas, resultados, progreso de todowrite).
        Devuelve False si no habia ningun mensaje user (no deberia pasar
        si is_busy es True, pero se chequea igual)."""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                self.messages.pop(i)
                self.messages.append({"role": "user", "content": new_text})
                return True
        return False

    # --- REQ-009: truncar el historial desde un mensaje user viejo -------
    def find_user_message_index(self, occurrence_from_end: int) -> int | None:
        """Encuentra el indice del N-esimo mensaje role=user contando
        desde el final (0 = el mas reciente / turno activo, 1 = el
        anterior a ese, etc.) -- usado por REQ-009 para saber donde
        truncar `session.messages` al editar/eliminar un mensaje de
        historial ya cerrado."""
        count = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                count += 1
                if count == occurrence_from_end:
                    return i
        return None


class SessionStore:
    """Registro en memoria de sesiones activas, keyed por session_id
    (generado por el frontend, ej. un uuid). Sin persistencia -- se pierde
    al reiniciar el proceso, tal como se definio para esta v1."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: str) -> SessionState:
        if session_id not in self._sessions:
            session = SessionState(session_id=session_id)
            # persistencia-sqlite-sesiones REQ-003: si esta sesion ya
            # existe en disco (backend reiniciado, o sesion abierta desde
            # otra maquina compartiendo la misma carpeta de proyecto),
            # reconstruye el historial ANTES de que nada mas la use --
            # asi el primer mensaje nuevo ya tiene contexto real en vez
            # de arrancar de cero silenciosamente.
            from app.persistence import (
                service as persistence,
            )  # import diferido: evita ciclo

            if persistence.session_exists_in_db(session_id):
                session.messages = persistence.load_session_messages(session_id)
                session.todos = persistence.load_session_todos(session_id)
            self._sessions[session_id] = session
        return self._sessions[session_id]

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def exists(self, session_id: str) -> bool:
        """REQ-007 (parada-y-cola-mensajes): distingue 'esta sesion nunca
        existio' de 'existe pero esta vacia' -- usado para detectar la
        sesion 'huerfana' (el frontend tiene historial persistido en
        localStorage de un session_id que el backend actual ya no
        reconoce). Desde persistencia-sqlite-sesiones (REQ-003), tambien
        cuenta como existente si esta guardada en SQLite -- una sesion
        recuperable desde disco tras un reinicio ya NO es huerfana."""
        if session_id in self._sessions:
            return True
        from app.persistence import (
            service as persistence,
        )  # import diferido: evita ciclo

        return persistence.session_exists_in_db(session_id)

    def delete(self, session_id: str) -> bool:
        """Elimina una sesion por completo (boton de basura del frontend).
        A diferencia del fix de concurrencia (donde CAMBIAR de sesion NO
        cancela el turno de otra), ac\u00e1 el usuario esta pidiendo
        explicitamente terminar con esta sesion -- si tiene un turno en
        curso, se cancela de verdad."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        if session.current_task is not None and not session.current_task.done():
            session.current_task.cancel()
        return True


_store = SessionStore()


def get_session_store() -> SessionStore:
    return _store
