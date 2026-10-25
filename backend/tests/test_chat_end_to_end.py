"""El CORAZON de Vision de punta a punta: pregunta -> modelo -> razonamiento -> respuesta, por HTTP real (`POST /api/chat` + stream SSE).

Por que existe (refactor total, 2026-10-14): una companera uso una version nueva y le salian errores en el chat. Los tests que ya habia cubren
tramos (el loop con un cliente falso, los textos de error, el corte del proveedor de IA), pero ninguno recorria TODO el camino por el endpoint con
preguntas encadenadas. Esta es la red que hay que mantener verde antes de tocar `main.py::_launch_turn/_drain_session_queue` o `llm/loop.py`.

Se prueba lo que la persona ve y lo que recibe el modelo, no la implementacion:
- la pregunta llega al modelo con el system prompt, el historial previo y las tools; el stream SSE trae el texto completo UNA sola vez y cierra con `done`;
- el marcador interno `[[TIPO: ...]]` jamas llega a la pantalla (ni partido en pedazos);
- el razonamiento (`reasoning_effort`) se manda SOLO a los modelos que lo aceptan;
- una herramienta se ejecuta y el modelo recibe su resultado con el MISMO id (en Claude/GPT los ids se sanean sin romper el par llamada/respuesta);
- una falla del modelo llega en lenguaje simple (sin texto crudo) y la sesion queda libre: el mensaje siguiente funciona;
- el token vencido (401) se renueva solo y la persona no ve nada.
El modelo es un FAKE: nunca hay red ni login del proveedor de IA (ADR-015)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.llm import loop as loop_module

pytestmark = pytest.mark.integration

GEMINI = "gemini-3.1-pro"
CLAUDE = "claude-sonnet-test"
RAW = "TEXTO-CRUDO-DEL-PROVEEDOR"
_REQUEST = SimpleNamespace(method="POST", url="https://proveedor.test/v1/chat/completions")


# ---------------------------------------------------------------------------------------------------------------------
# El proveedor de IA falso: un guion con un paso por cada pedido al modelo. Un paso es una lista de chunks, o una funcion que la arma o que lanza.
# ---------------------------------------------------------------------------------------------------------------------
def chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def says(*pieces: str):
    """Respuesta de solo texto, entregada en los pedazos dados (asi se prueba el marcador partido entre chunks)."""
    return [*(chunk(content=p) for p in pieces), chunk(finish_reason="stop")]


def calls_tool(name: str, arguments: dict, call_id: str):
    fragment = SimpleNamespace(
        index=0, id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )
    return [chunk(tool_calls=[fragment]), chunk(finish_reason="tool_calls")]


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        pass


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Copia del historial EN ESE MOMENTO: el loop sigue agregando mensajes a la misma lista despues de la llamada.
        snapshot = dict(kwargs)
        snapshot["messages"] = json.loads(json.dumps(kwargs["messages"], default=str))
        self.requests.append(snapshot)
        step = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        return _Stream(step)


def status_error(cls, status: int, text: str):
    return cls(text, response=SimpleNamespace(status_code=status, headers={}, request=_REQUEST), body=None)


# ---------------------------------------------------------------------------------------------------------------------
# Entorno
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Todo lo que el turno consulta aparte del cliente de chat, sin red: modelo por defecto, ventana de contexto, catalogo y compactacion."""
    monkeypatch.setattr(loop_module, "get_settings", lambda: SimpleNamespace(llm_model=GEMINI))
    monkeypatch.setattr(main, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setattr(main, "list_models", lambda: None)
    monkeypatch.setitem(main._MODELS_CACHE, "by_id", {})

    async def _no_compact(*args, **kwargs):
        return False

    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _no_compact)


@pytest.fixture
def model(monkeypatch):
    """`model.use(script)` instala el el proveedor de IA falso. `model.refreshes` registra cada `get_client(force_refresh=...)`."""
    state = SimpleNamespace(client=None, refreshes=[])

    def use(script):
        state.client = FakeLLM(script)
        return state.client

    def _get_client(force_refresh: bool = False, **_kwargs):
        state.refreshes.append(force_refresh)
        return state.client

    monkeypatch.setattr(loop_module, "get_client", _get_client)
    state.use = use
    return state


@pytest.fixture
def sid():
    session_id = f"chat-e2e-{uuid.uuid4()}"
    yield session_id
    main.get_session_store().delete(session_id)


@pytest.fixture
def client():
    return TestClient(main.app)


def ask(client, sid: str, text: str, **extra) -> list[dict]:
    """Una pregunta por `POST /api/chat` y el stream SSE completo, ya parseado."""
    resp = client.post("/api/chat", json={"session_id": sid, "message": text, **extra})
    assert resp.status_code == 200, resp.text
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def shown_text(events: list[dict]) -> str:
    """Lo que la persona ve escrito: todos los text-delta juntos."""
    return "".join(e["data"]["text"] for e in events if e["type"] == "text-delta")


def user_messages(request: dict) -> list[str]:
    return [m["content"] for m in request["messages"] if m["role"] == "user"]


# ---------------------------------------------------------------------------------------------------------------------
# 1) La pregunta llega al modelo y la respuesta vuelve completa, una sola vez
# ---------------------------------------------------------------------------------------------------------------------
class TestAQuestionAndItsAnswer:
    def test_the_question_reaches_the_model_with_its_system_prompt_and_tools(self, client, model, sid):
        fake = model.use([says("[[TIPO: conversacional]]Hola, todo bien.")])

        ask(client, sid, "¿Cómo estás?")

        assert len(fake.requests) == 1
        request = fake.requests[0]
        roles = [m["role"] for m in request["messages"]]
        assert roles == ["system", "user"]  # el system prompt va primero y una sola vez
        assert request["messages"][0]["content"].strip()
        assert user_messages(request) == ["¿Cómo estás?"]
        assert request["stream"] is True
        assert request["model"] == GEMINI
        assert request["tools"], "el modelo tiene que ver las herramientas"

    def test_the_answer_arrives_complete_exactly_once_and_the_turn_closes_with_done(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]Hola, ", "todo bien.")])

        events = ask(client, sid, "hola")

        assert shown_text(events) == "Hola, todo bien."  # ni cortada ni duplicada
        assert types(events)[-1] == "done"
        assert "error" not in types(events)

    def test_the_session_is_free_after_the_turn_and_keeps_the_conversation(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]Listo.")])

        ask(client, sid, "hola")

        session = main.get_session_store().get_or_create(sid)
        assert not session.is_busy
        assert [m["role"] for m in session.messages] == ["system", "user", "assistant"]
        assert session.messages[-1]["content"].endswith("Listo.")

    def test_a_follow_up_question_carries_the_previous_question_and_answer(self, client, model, sid):
        fake = model.use(
            [says("[[TIPO: conversacional]]Soy Vision."), says("[[TIPO: conversacional]]Te ayudo con stock.")]
        )

        ask(client, sid, "¿quién sos?")
        ask(client, sid, "¿en qué me ayudás?")

        second = fake.requests[1]
        assert user_messages(second) == ["¿quién sos?", "¿en qué me ayudás?"]
        assistant = [m["content"] for m in second["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1 and "Soy Vision." in assistant[0]  # el modelo ve su propia respuesta anterior
        assert [m["role"] for m in second["messages"]].count("system") == 1  # el system prompt NO se repite

    def test_two_sessions_never_share_history(self, client, model):
        a, b = f"chat-e2e-a-{uuid.uuid4()}", f"chat-e2e-b-{uuid.uuid4()}"
        fake = model.use([says("[[TIPO: conversacional]]ok")])
        try:
            ask(client, a, "pregunta secreta de A")
            ask(client, b, "pregunta de B")
            assert user_messages(fake.requests[1]) == ["pregunta de B"]
        finally:
            main.get_session_store().delete(a)
            main.get_session_store().delete(b)


# ---------------------------------------------------------------------------------------------------------------------
# 2) El marcador interno [[TIPO: ...]] nunca se ve
# ---------------------------------------------------------------------------------------------------------------------
class TestTheTypeMarkerNeverReachesTheScreen:
    @pytest.mark.parametrize(
        "pieces",
        [
            ("[[TIPO: conversacional]]Hola.",),
            ("[[TIPO:", " conversacional]]Hola."),  # partido a mitad del marcador
            ("[", "[TIPO: conversacional", "]]Hola."),  # partido en tres
            ("[[TIPO: conversacional]]", "Hola."),
        ],
    )
    def test_the_marker_is_removed_even_when_it_arrives_split_between_chunks(self, client, model, sid, pieces):
        model.use([says(*pieces)])

        events = ask(client, sid, "hola")

        assert shown_text(events) == "Hola."
        assert "TIPO" not in shown_text(events)

    def test_an_answer_with_normal_brackets_is_not_swallowed(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]Usá la lista [a, b] y el rango [1..3].")])

        assert shown_text(ask(client, sid, "hola")) == "Usá la lista [a, b] y el rango [1..3]."


# ---------------------------------------------------------------------------------------------------------------------
# 3) El razonamiento: se manda solo a quien lo acepta
# ---------------------------------------------------------------------------------------------------------------------
class TestReasoning:
    def _catalog(self, monkeypatch, model_id: str, valid_args: list[str]):
        monkeypatch.setitem(main._MODELS_CACHE, "by_id", {model_id: {"id": model_id, "valid_args": valid_args}})

    def test_reasoning_effort_is_sent_when_the_model_accepts_it(self, client, model, sid, monkeypatch):
        self._catalog(monkeypatch, GEMINI, ["temperature", "reasoning_effort"])
        fake = model.use([says("[[TIPO: analitica_simple]]Resuelto.")])

        ask(client, sid, "analizá esto", reasoning_effort="high", temperature=0.2)

        assert fake.requests[0]["reasoning_effort"] == "high"
        assert fake.requests[0]["temperature"] == 0.2

    def test_a_parameter_the_model_does_not_accept_is_never_sent(self, client, model, sid, monkeypatch):
        """Mandar un parametro que el modelo no soporta es un 400 y la persona ve un error por algo que ella ni eligio."""
        self._catalog(monkeypatch, GEMINI, ["temperature"])  # sin reasoning_effort ni verbosity
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", reasoning_effort="high", verbosity="low", top_p=0.5, temperature=0.3)

        sent = fake.requests[0]
        assert "reasoning_effort" not in sent and "verbosity" not in sent and "top_p" not in sent
        assert sent["temperature"] == 0.3

    def test_without_catalog_data_no_generation_parameter_is_sent(self, client, model, sid):
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", reasoning_effort="high", temperature=0.9)

        assert not {"reasoning_effort", "temperature", "top_p", "max_tokens", "verbosity"} & fake.requests[0].keys()

    def test_the_model_chosen_in_the_selector_is_the_one_called(self, client, model, sid):
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", model=CLAUDE)

        assert fake.requests[0]["model"] == CLAUDE


# ---------------------------------------------------------------------------------------------------------------------
# 4) Herramientas: se ejecutan y el modelo recibe el resultado
# ---------------------------------------------------------------------------------------------------------------------
TODOS = {"todos": [{"content": "Revisar stock", "status": "in_progress", "priority": "high"}]}


class TestToolCalls:
    def test_the_tool_runs_and_the_model_gets_its_result_to_finish_the_answer(self, client, model, sid):
        fake = model.use([calls_tool("todowrite", TODOS, "call_abc"), says("Listo, armé el plan.")])

        events = ask(client, sid, "armame un plan")

        assert types(events).count("tool-call") == 1 and types(events).count("tool-result") == 1
        assert types(events).index("tool-call") < types(events).index("tool-result") < types(events).index("done")
        assert shown_text(events) == "Listo, armé el plan."
        assert len(fake.requests) == 2  # una para decidir la tool, otra con su resultado
        followup = fake.requests[1]["messages"]
        call_msg = next(m for m in followup if m["role"] == "assistant" and m.get("tool_calls"))
        tool_msg = next(m for m in followup if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == call_msg["tool_calls"][0]["id"]  # el par llamada/respuesta no se rompe
        assert tool_msg["content"].strip()

    def test_an_unknown_tool_does_not_break_the_turn(self, client, model, sid):
        """Las tools nunca lanzan: el modelo recibe el error como texto y puede seguir."""
        model.use([calls_tool("no_existe", {}, "call_x"), says("No pude usar esa herramienta, sigo sin ella.")])

        events = ask(client, sid, "hacé algo raro")

        assert "error" not in types(events)
        assert types(events)[-1] == "done"
        assert shown_text(events) == "No pude usar esa herramienta, sigo sin ella."

    def test_long_tool_ids_are_sanitized_for_claude_and_gpt_but_the_pair_still_matches(self, client, model, sid):
        """GPT da 400 con ids de mas de 40 caracteres y Claude da 500 con `content` None: el historial que va a esos modelos se sanea."""
        long_id = "call_" + "x" * 70
        fake = model.use([calls_tool("todowrite", TODOS, long_id), says("Listo.")])

        ask(client, sid, "plan", model=CLAUDE)

        followup = fake.requests[1]["messages"]
        call_msg = next(m for m in followup if m["role"] == "assistant" and m.get("tool_calls"))
        tool_msg = next(m for m in followup if m["role"] == "tool")
        sent_id = call_msg["tool_calls"][0]["id"]
        assert len(sent_id) <= 40
        assert tool_msg["tool_call_id"] == sent_id
        assert call_msg["content"] is not None

    def test_gemini_keeps_its_original_tool_ids(self, client, model, sid):
        long_id = "call_" + "y" * 70
        fake = model.use([calls_tool("todowrite", TODOS, long_id), says("Listo.")])

        ask(client, sid, "plan")  # modelo por defecto = Gemini

        call_msg = next(m for m in fake.requests[1]["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
        assert call_msg["tool_calls"][0]["id"] == long_id


# ---------------------------------------------------------------------------------------------------------------------
# 5) Cuando el modelo falla: lenguaje simple, sesion libre y el mensaje siguiente funciona
# ---------------------------------------------------------------------------------------------------------------------
class TestWhenTheModelFails:
    @pytest.mark.parametrize(
        ("error_cls", "status"),
        [
            (openai.RateLimitError, 429),
            (openai.InternalServerError, 500),
            (openai.InternalServerError, 502),
            (openai.BadRequestError, 400),
        ],
    )
    def test_the_failure_reaches_the_person_in_plain_words_and_never_the_raw_text(
        self, client, model, sid, error_cls, status
    ):
        model.use([status_error(error_cls, status, f"Error code: {status} - {RAW}")])

        events = ask(client, sid, "hola")

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["data"]["message"].strip()
        assert RAW not in errors[0]["data"]["message"]  # el crudo va al log, no a la pantalla
        assert errors[0]["data"]["code"]
        assert types(events)[-1] == "error"

    def test_after_a_failure_the_session_is_free_and_the_next_message_works(self, client, model, sid):
        fake = model.use(
            [
                status_error(openai.InternalServerError, 500, f"Error code: 500 - {RAW}"),
                says("[[TIPO: conversacional]]Ya estoy."),
            ]
        )

        failed = ask(client, sid, "hola")
        assert types(failed)[-1] == "error"
        assert not main.get_session_store().get_or_create(sid).is_busy

        retry = ask(client, sid, "hola de nuevo")

        assert types(retry)[-1] == "done" and shown_text(retry) == "Ya estoy."
        assert len(fake.requests) == 2

    def test_a_failure_does_not_leave_a_half_turn_in_the_history_sent_next_time(self, client, model, sid):
        fake = model.use([status_error(openai.InternalServerError, 500, "x"), says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "primera, falla")
        ask(client, sid, "segunda")

        roles = [m["role"] for m in fake.requests[1]["messages"]]
        assert roles[0] == "system" and roles.count("system") == 1
        assert user_messages(fake.requests[1])[-1] == "segunda"

    def test_an_expired_token_is_refreshed_silently_and_the_person_sees_no_error(self, client, model, sid):
        """401 `Invalid Token` aparece en el log de produccion varias veces por semana: se renueva y se reintenta sin que se note."""
        expired = status_error(openai.AuthenticationError, 401, "Error code: 401 - {'Error': 'Invalid Token'}")
        fake = model.use([expired, says("[[TIPO: conversacional]]Todo bien.")])

        events = ask(client, sid, "hola")

        assert "error" not in types(events)
        assert shown_text(events) == "Todo bien."
        assert True in model.refreshes  # se pidio un cliente con token nuevo
        assert len(fake.requests) == 2

    def test_a_crash_inside_vision_mid_turn_reaches_the_chat_and_frees_the_session(
        self, client, model, sid, monkeypatch
    ):
        async def _crashing_turn(*args, **kwargs):
            yield {"type": "text-delta", "data": {"text": "Arranco..."}}
            raise RuntimeError(f"bug interno {RAW}")

        monkeypatch.setattr(main, "run_turn", _crashing_turn)

        events = ask(client, sid, "hola")

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1 and RAW not in errors[0]["data"]["message"]
        assert not main.get_session_store().get_or_create(sid).is_busy


# ---------------------------------------------------------------------------------------------------------------------
# 6) Contrato del flujo SSE que el frontend espera
# ---------------------------------------------------------------------------------------------------------------------
class TestTheStreamContractTheFrontendRelyOn:
    def test_every_turn_event_carries_the_turn_id_of_the_message_that_started_it(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]hola")])

        events = ask(client, sid, "hola", message_id="msg-123")

        turn_events = [e for e in events if e["type"] in ("text-delta", "done")]
        assert turn_events and all(e.get("turn_id") == "msg-123" for e in turn_events)

    def test_the_internal_markers_are_never_sent_to_the_browser(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]hola")])

        events = ask(client, sid, "hola")

        assert not [e for e in events if str(e["type"]).startswith("__")]
        assert all("_turn_id" not in e for e in events)  # el campo interno se traduce a `turn_id`

    def test_the_stream_ends_by_itself_when_the_turn_ends(self, client, model, sid):
        """Si el stream no cerrara, el navegador se quedaria en «Vision está trabajando…» para siempre (bug real de 4 rondas, 2026-09-14)."""
        model.use([says("[[TIPO: conversacional]]hola")])

        resp = client.post("/api/chat", json={"session_id": sid, "message": "hola"})

        assert resp.status_code == 200  # TestClient solo vuelve si el generador SSE termino
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_an_empty_message_is_rejected_cleanly_or_answered_but_never_crashes_the_server(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]ok")])

        resp = client.post("/api/chat", json={"session_id": sid, "message": ""})

        assert resp.status_code in (200, 400, 422)
        assert resp.status_code != 500


# ---------------------------------------------------------------------------------------------------------------------
# 7) El comando /plan: el modelo recibe la orden de clasificar como Planificable, el resto de los mensajes NO se toca
# ---------------------------------------------------------------------------------------------------------------------
class TestThePlanCommand:
    def test_slash_plan_is_rewritten_for_the_model_with_the_request_after_it(self, client, model, sid):
        fake = model.use([says("[[TIPO: planificable]]ok")])

        ask(client, sid, "/plan armar el inventario de abril")

        sent = user_messages(fake.requests[0])[0]
        assert sent == (
            "OVER@ride de clasificacion: este mensaje llego con el comando /plan. "
            "Obligatoriamente printea como PRIMER TOKEN [[TIPO: Planificable]] y luego "
            "responde este pedido: armar el inventario de abril"
        )

    def test_slash_plan_with_leading_spaces_still_counts_as_the_command(self, client, model, sid):
        fake = model.use([says("[[TIPO: planificable]]ok")])

        ask(client, sid, "   /plan hacer algo")

        assert user_messages(fake.requests[0])[0].endswith("responde este pedido: hacer algo")

    @pytest.mark.parametrize("text", ["/plan", "planificá esto", "el /plan de ayer", "/planear algo"])
    def test_anything_that_is_not_the_command_reaches_the_model_untouched(self, client, model, sid, text):
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, text)

        assert user_messages(fake.requests[0]) == [text]

    def test_a_planned_answer_with_its_todowrite_reaches_the_screen_once_and_closes(self, client, model, sid):
        model.use([calls_tool("todowrite", TODOS, "call_plan"), says("[[TIPO: planificable]]Plan listo.")])

        events = ask(client, sid, "/plan algo")

        assert shown_text(events) == "Plan listo." and types(events)[-1] == "done"

    def test_a_planned_answer_that_only_promises_is_retried_instead_of_accepted(self, client, model, sid):
        """Bug real 2026-09-21: el modelo decia «ahora cargo el plan» sin ejecutar nada y el turno se daba por terminado. El loop corrige y reintenta
        (hasta `_MAX_EMPTY_PROMISE_RETRIES`); aca el modelo obedece a la primera correccion."""
        fake = model.use(
            [
                says("[[TIPO: planificable]]Ahora cargo el plan."),
                calls_tool("todowrite", TODOS, "call_fix"),
                says("Plan cargado."),
            ]
        )

        events = ask(client, sid, "/plan algo")

        assert len(fake.requests) == 3  # promesa vacia -> correccion -> tool real -> cierre
        assert "system-note" in types(events)  # la persona ve que Vision se corrigio
        assert types(events)[-1] == "done" and "error" not in types(events)


# ---------------------------------------------------------------------------------------------------------------------
# 8) El ciclo de vida del turno: lo que otras partes de Vision leen despues (agentes, editar, borrar, cola)
# ---------------------------------------------------------------------------------------------------------------------
class TestTheTurnLifecycle:
    def test_a_finished_turn_is_marked_done_so_the_agent_scheduler_can_close_its_run(self, client, model, sid):
        """`scheduler/agents.py::_sweep_finished_runs` deja una ejecucion de agente «running» para siempre si el turno no pasa a `done`."""
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", message_id="t-done")

        assert main.get_session_store().get_or_create(sid).turns["t-done"].status == "done"

    def test_the_turn_remembers_where_its_user_message_sits_in_the_history(self, client, model, sid):
        """Editar, borrar y ↻ ubican el mensaje por este indice: [0]=system, [1]=1ra pregunta, [2]=su respuesta, [3]=2da pregunta."""
        model.use([says("[[TIPO: conversacional]]uno"), says("[[TIPO: conversacional]]dos")])

        ask(client, sid, "primera", message_id="t1")
        ask(client, sid, "segunda", message_id="t2")

        session = main.get_session_store().get_or_create(sid)
        assert session.turns["t1"].messages_start_index == 1
        assert session.turns["t2"].messages_start_index == 3
        assert session.messages[1]["content"] == "primera" and session.messages[3]["content"] == "segunda"

    @pytest.mark.timeout(15)
    def test_the_stream_closes_even_if_the_turn_ends_without_a_terminal_event(self, client, sid, monkeypatch):
        """Un turno que termina sin `done`/`error`/`stopped` igual tiene que cerrar el stream: `__turn_done__` es la red de seguridad.
        Sin ella el navegador queda en «Vision está trabajando…» para siempre."""

        async def _ends_silently(*args, **kwargs):
            yield {"type": "text-delta", "data": {"text": "a medias"}}

        monkeypatch.setattr(main, "run_turn", _ends_silently)

        events = ask(client, sid, "hola")

        assert shown_text(events) == "a medias"
        assert not main.get_session_store().get_or_create(sid).is_busy

    def test_a_finished_turn_releases_its_history_snapshot(self, client, model, sid):
        """`turn_baseline` es una copia de TODO el historial que solo sirve mientras el turno corre: al terminar se suelta (si no, cada sesion
        retiene una copia extra de su conversacion)."""
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola")

        session = main.get_session_store().get_or_create(sid)
        assert session.turn_baseline is None and session.active_turn_id is None

    @pytest.mark.timeout(20)
    def test_a_failure_while_discarding_a_deleted_turn_never_leaves_the_stream_hanging_or_the_session_busy(self, client, sid, monkeypatch):
        """Borrar un mensaje con el turno ya arrancado lo descarta al terminar. Si ese descarte fallara, el cierre del turno tiene que seguir
        (avisar `__turn_done__`, arrancar el siguiente): una excepcion aca dejaba el navegador en «Vision está trabajando…» para siempre."""

        async def _turn_the_user_deleted(session, *args, **kwargs):
            session.discard_turn_ids.add(session.active_turn_id)  # el usuario borro este mensaje mientras corria
            yield {"type": "text-delta", "data": {"text": "respuesta a un mensaje ya borrado"}}
            yield {"type": "done", "data": {}}

        def _discard_that_breaks(*args, **kwargs):
            raise RuntimeError("fallo al descartar")

        monkeypatch.setattr(main, "run_turn", _turn_the_user_deleted)
        monkeypatch.setattr(main, "_discard_finished_turn", _discard_that_breaks)

        events = ask(client, sid, "mensaje que se va a borrar")

        assert types(events)[-1] == "done"  # el stream cerro solo
        session = main.get_session_store().get_or_create(sid)
        assert not session.is_busy
        # Lo que de verdad se pierde si el cierre se corta a mitad: el turno sigue figurando activo y el siguiente de la cola no arranca.
        assert session.active_turn_id is None and session.turn_baseline is None


# ---------------------------------------------------------------------------------------------------------------------
# 9) Preparacion del turno: catalogo de modelos y compactacion (nunca cortan el chat)
# ---------------------------------------------------------------------------------------------------------------------
class TestTurnPreparation:
    def test_a_failed_compaction_never_cuts_the_turn(self, client, model, sid, monkeypatch, caplog):
        """Bug real 2026-09-25: con el tope de consumo alcanzado, compactar lanzaba 429 ANTES de llegar al modelo y cada mensaje terminaba en
        «problema interno». Compactar es mejor esfuerzo: se avisa en el log y el turno sigue."""
        import app.session.compaction as compaction_module

        async def _boom(*args, **kwargs):
            raise RuntimeError("429 tope de consumo")

        monkeypatch.setattr(compaction_module, "maybe_compact", _boom)
        model.use([says("[[TIPO: conversacional]]Sigo igual.")])

        with caplog.at_level("WARNING"):
            events = ask(client, sid, "hola")

        assert shown_text(events) == "Sigo igual." and types(events)[-1] == "done" and "error" not in types(events)
        assert "se sigue sin compactar" in caplog.text

    def test_a_compaction_tells_the_person_what_happened_before_the_answer(self, client, model, sid, monkeypatch):
        import app.session.compaction as compaction_module

        async def _compacted(*args, **kwargs):
            return True

        monkeypatch.setattr(compaction_module, "maybe_compact", _compacted)
        model.use([says("[[TIPO: conversacional]]Listo.")])

        events = ask(client, sid, "hola")

        notes = [e for e in events if e["type"] == "system-note"]
        assert len(notes) == 1 and "Resumí la parte más vieja" in notes[0]["data"]["text"]
        assert types(events).index("system-note") < types(events).index("text-delta")

    def test_without_compaction_there_is_no_summary_note(self, client, model, sid):
        model.use([says("[[TIPO: conversacional]]Listo.")])

        assert "system-note" not in types(ask(client, sid, "hola"))

    def test_compaction_receives_the_session_history_the_window_and_the_active_model(self, client, model, sid, monkeypatch):
        import app.session.compaction as compaction_module

        seen = {}

        async def _spy(session_id, messages, context_window, model_id):
            seen.update(session_id=session_id, context_window=context_window, model_id=model_id, roles=[m["role"] for m in messages])
            return False

        monkeypatch.setattr(compaction_module, "maybe_compact", _spy)
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", model=CLAUDE)

        assert seen == {"session_id": sid, "context_window": 200_000, "model_id": CLAUDE, "roles": ["system", "user"]}

    def test_the_model_catalog_is_fetched_when_the_cache_is_empty_and_its_params_apply(self, client, model, sid, monkeypatch):
        calls = []

        def _list_models():
            calls.append(1)
            main._MODELS_CACHE["by_id"] = {GEMINI: {"id": GEMINI, "valid_args": ["reasoning_effort"]}}

        monkeypatch.setitem(main._MODELS_CACHE, "by_id", None)
        monkeypatch.setattr(main, "list_models", _list_models)
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", reasoning_effort="low")

        assert calls == [1] and fake.requests[0]["reasoning_effort"] == "low"

    def test_a_failing_catalog_never_breaks_the_chat_and_sends_no_optional_parameters(self, client, model, sid, monkeypatch):
        def _list_models():
            raise ConnectionError("El proveedor de IA /models_metadata no responde")

        monkeypatch.setitem(main._MODELS_CACHE, "by_id", None)
        monkeypatch.setattr(main, "list_models", _list_models)
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        events = ask(client, sid, "hola", reasoning_effort="high", temperature=0.5)

        assert shown_text(events) == "ok" and types(events)[-1] == "done"
        assert "reasoning_effort" not in fake.requests[0] and "temperature" not in fake.requests[0]

    def test_a_warm_catalog_is_not_fetched_again(self, client, model, sid, monkeypatch):
        calls = []
        monkeypatch.setitem(main._MODELS_CACHE, "by_id", {})
        monkeypatch.setattr(main, "list_models", lambda: calls.append(1))
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola")

        assert calls == []

    def test_compaction_uses_the_context_window_of_the_model_in_use(self, client, model, sid, monkeypatch):
        """Cada modelo tiene su ventana: compactar con la de otro modelo recorta de mas (o no recorta y el modelo rechaza el pedido por largo)."""
        import app.session.compaction as compaction_module

        seen = []

        async def _spy(session_id, messages, context_window, model_id):
            seen.append((model_id, context_window))
            return False

        monkeypatch.setattr(main, "resolve_context_window", lambda m: {CLAUDE: 123_456, GEMINI: 999_999}.get(m, 1))
        monkeypatch.setattr(compaction_module, "maybe_compact", _spy)
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", model=CLAUDE)

        assert seen == [(CLAUDE, 123_456)]

    def test_a_recovered_session_without_system_prompt_gets_it_first_not_in_the_middle(self, client, model, sid):
        """Tras reiniciar Vision el historial vuelve de SQLite SIN el system prompt (nunca se persiste): tiene que quedar al principio."""
        session = main.get_session_store().get_or_create(sid)
        session.messages = [{"role": "user", "content": "pregunta de ayer"}, {"role": "assistant", "content": "respuesta de ayer"}]
        fake = model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "pregunta de hoy")

        roles = [m["role"] for m in fake.requests[0]["messages"]]
        assert roles == ["system", "user", "assistant", "user"]

    def test_the_session_remembers_which_model_is_answering_for_the_tools_that_need_it(self, client, model, sid):
        """La tool `read` decide si puede leer un PDF segun el modelo activo de la sesion."""
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "hola", model=CLAUDE)

        assert main.get_session_store().get_or_create(sid).active_model == CLAUDE

    def test_a_message_deleted_before_its_turn_starts_is_stopped_without_calling_the_model(self, client, model, sid):
        """Borrar un mensaje justo cuando sale de la cola: el Detener es de ESTE turno y no se pierde (si no, se respondia entero)."""
        session = main.get_session_store().get_or_create(sid)
        session.discard_turn_ids.add("m-borrado")
        fake = model.use([says("[[TIPO: conversacional]]no deberia llegar")])

        events = ask(client, sid, "mensaje que el usuario ya borro", message_id="m-borrado")

        assert fake.requests == []
        stopped = [e for e in events if e["type"] == "stopped"]
        assert len(stopped) == 1 and stopped[0]["data"]["reason"] == "message_deleted"
        assert "text-delta" not in types(events)

    def test_a_pending_edit_from_before_does_not_leak_into_the_next_turn(self, client, model, sid):
        """`edit_pending_text` de un turno anterior no puede reescribir el mensaje del turno siguiente."""
        session = main.get_session_store().get_or_create(sid)
        session.edit_pending_text = "texto de una edicion vieja"
        model.use([says("[[TIPO: conversacional]]ok")])

        ask(client, sid, "pregunta nueva")

        assert session.edit_pending_text is None
        assert [m["content"] for m in session.messages if m["role"] == "user"] == ["pregunta nueva"]

    def test_after_compaction_the_turn_snapshot_is_rebuilt_without_this_turns_own_message(self, client, model, sid, monkeypatch):
        """Si el usuario borra este mensaje, se descarta solo lo suyo: la foto de «lo de antes» tiene que ser la lista YA compactada (resumen
        incluido) menos el mensaje del turno. Sin esto, borrar se llevaba el resumen de toda la conversacion."""
        import app.session.compaction as compaction_module

        resumen = {"role": "user", "content": "[resumen de la conversacion]"}

        async def _compact(session_id, messages, context_window, model_id):
            messages[:] = [messages[0], resumen, messages[-1]]  # system, resumen, mensaje de este turno
            return True

        snapshots = []

        def _watch():
            snapshots.append(list(main.get_session_store().get_or_create(sid).turn_baseline or []))
            return says("[[TIPO: conversacional]]ok")

        monkeypatch.setattr(compaction_module, "maybe_compact", _compact)
        model.use([_watch])

        ask(client, sid, "pregunta larga")

        assert [m["content"] for m in snapshots[0] if m["role"] != "system"] == ["[resumen de la conversacion]"]
