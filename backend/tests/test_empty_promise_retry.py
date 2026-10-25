"""BUG REAL reportado por el usuario (2026-09-21, sesion "no ejecute la
busqueda" con NGAVS): el modelo respondia con texto tipo "Ahora si cargo el
plan..." / "lanzo la busqueda en Microsoft Graph..." SIN emitir ninguna
tool-call real -- ni siquiera `todowrite`, pese a que el propio system
prompt exige que sea la PRIMERA tool-call de un pedido `planificable`. El
turno se aceptaba como `done` normal, y el usuario terminaba viendo una
cadena de 10+ mensajes disculpandose y prometiendo ejecutar sin que nada
forzara la ejecucion real (ver conversacion completa citada por el usuario).

Este test reproduce el bug con un cliente del proveedor de IA FALSO que, en su primer
paso, devuelve exactamente ese patron (texto de promesa, tool_calls vacio,
finish_reason='stop') y verifica que `run_turn`:
1. NO acepta ese paso como turno terminado (no debe yieldear "done" todavia).
2. Inyecta una correccion (mensaje 'system') y reintenta automaticamente en
   el MISMO turno -- el cliente falso, en su segundo paso, emite la
   tool-call real (`todowrite`) y ahi si el turno debe seguir su curso
   normal.
3. Si el modelo IGNORA la correccion y vuelve a prometer sin ejecutar mas
   de `_MAX_EMPTY_PROMISE_RETRIES` veces, el turno debe cortar avisando al
   usuario en vez de reintentar para siempre.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm import loop as loop_module
from app.session.events import SessionState


class _FakeChoice:
    def __init__(self, content: str | None, tool_calls=None, finish_reason=None):
        self.delta = _FakeDelta(content, tool_calls)
        self.finish_reason = finish_reason


class _FakeDelta:
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeToolCallFunction:
    def __init__(self, name: str = "", arguments: str = ""):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, index: int, id_: str, name: str, arguments: str):
        self.index = index
        self.id = id_
        self.function = _FakeToolCallFunction(name, arguments)


class _FakeChunk:
    def __init__(self, choice: _FakeChoice):
        self.choices = [choice]


class _FakeStream:
    """Itera una lista fija de chunks ya armada -- simula
    `client.chat.completions.create(stream=True)`."""

    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:  # pragma: no cover - no se usa en este test
        pass


def _text_chunk(text: str) -> _FakeChunk:
    return _FakeChunk(_FakeChoice(content=text))


def _stop_chunk() -> _FakeChunk:
    return _FakeChunk(_FakeChoice(content=None, finish_reason="stop"))


def _tool_call_chunk() -> list[_FakeChunk]:
    """Simula una tool-call de `todowrite` completa en un solo fragmento
    (en la practica llega particionada, pero para este test alcanza con
    un unico chunk: `tool_calls_acc` se arma igual)."""
    todos_arg = json.dumps(
        {
            "todos": [
                {
                    "content": "Buscar en Graph",
                    "status": "in_progress",
                    "priority": "high",
                }
            ]
        }
    )
    call = _FakeToolCall(0, "call_1", "todowrite", todos_arg)
    return [
        _FakeChunk(_FakeChoice(content=None, tool_calls=[call])),
        _FakeChunk(
            _FakeChoice(content=None, tool_calls=[], finish_reason="tool_calls")
        ),
    ]


class _FakeCompletions:
    """Devuelve, en orden, una lista de "respuestas" (cada una una lista
    de chunks) -- una por cada llamada a `.create()`. Permite simular
    varios pasos consecutivos del loop de tool-calling."""

    def __init__(self, responses: list[list[_FakeChunk]]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def create(self, **kwargs):
        idx = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return _FakeStream(self._responses[idx])


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, responses: list[list[_FakeChunk]]) -> None:
        self.chat = _FakeChat(_FakeCompletions(responses))


@pytest.fixture(autouse=True)
def _patch_loop_dependencies(monkeypatch):
    """Evita tocar red/el proveedor real para todo lo que `run_turn` consulta
    aparte del cliente de chat (valid_args del modelo, context_window)."""
    monkeypatch.setattr(
        loop_module,
        "get_settings",
        lambda: type("S", (), {"llm_model": "test-model", "vision_company_name": ""})(),
    )

    async def _fake_maybe_compact(*args, **kwargs):
        return False

    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _fake_maybe_compact)

    import app.main as main_module

    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)

    def _fake_list_models():
        main_module._MODELS_CACHE["by_id"] = {}

    monkeypatch.setattr(main_module, "list_models", _fake_list_models)


def _make_session() -> SessionState:
    return SessionState(session_id="")  # sin session_id: persistencia es no-op


def test_empty_promise_triggers_automatic_retry(monkeypatch):
    """Primer paso: solo texto de promesa, sin tool_calls -> Vision debe
    reintentar automaticamente (no debe emitir 'done' todavia). Segundo
    paso: el modelo ya ejecuta `todowrite` de verdad -> el turno sigue su
    curso normal (tool-call real ejecutada, no un corte con aviso)."""
    empty_promise_chunks = [
        _text_chunk(
            "[[TIPO: planificable]]Ahí mismo cargo el plan paso a paso en tus tareas "
            "y ejecuto la búsqueda en Microsoft Graph para traerte información sobre NGAVS."
        ),
        _stop_chunk(),
    ]
    fake_client = _FakeClient([empty_promise_chunks, _tool_call_chunk()])
    monkeypatch.setattr(
        loop_module, "get_client", lambda force_refresh=False, **_k: fake_client
    )

    session = _make_session()
    events = asyncio.run(_collect_events(session, "buscame info de NGAVS en la red"))

    event_types = [e["type"] for e in events]
    # Debe haber un aviso de correccion automatica (system-note) ANTES de
    # cualquier "done" -- nunca debe cerrar el turno con la promesa vacia.
    assert "system-note" in event_types
    note_idx = event_types.index("system-note")
    assert "vuelvo a pedir automáticamente" in events[note_idx]["data"]["text"].lower()

    # La tool-call real (todowrite) debe haberse ejecutado en el segundo
    # paso, tras la correccion -- confirma que el reintento fue efectivo,
    # no solo un mensaje cosmetico.
    tool_call_events = [e for e in events if e["type"] == "tool-call"]
    assert any(e["data"]["name"] == "todowrite" for e in tool_call_events)

    # El historial real debe contener el mensaje de correccion inyectado
    # como 'system' (nunca como si el usuario lo hubiera escrito).
    correction_messages = [
        m
        for m in session.messages
        if m.get("role") == "system"
        and "Corrección automática" in str(m.get("content", ""))
    ]
    assert len(correction_messages) == 1


async def _collect_events(session: SessionState, user_message: str) -> list[dict]:
    events = []
    async for event in loop_module.run_turn(session, user_message, "test-session-id"):
        events.append(event)
    return events


def test_repeated_empty_promise_gives_up_after_max_retries(monkeypatch):
    """Si el modelo IGNORA la correccion y sigue prometiendo sin ejecutar
    nada mas alla del limite de reintentos, el turno debe cortar avisando
    explicitamente en vez de reintentar para siempre (bug real: la
    conversacion original tenia MAS DE 10 promesas vacias consecutivas)."""
    empty_promise_chunks = [
        _text_chunk("[[TIPO: planificable]]Ahora sí, ejecuto la búsqueda ya mismo."),
        _stop_chunk(),
    ]
    # Todas las respuestas son la misma promesa vacia -- el modelo nunca
    # se corrige, sin importar cuantas veces se lo pida.
    responses = [empty_promise_chunks] * (loop_module._MAX_EMPTY_PROMISE_RETRIES + 2)
    fake_client = _FakeClient(responses)
    monkeypatch.setattr(
        loop_module, "get_client", lambda force_refresh=False, **_k: fake_client
    )

    session = _make_session()
    events = asyncio.run(_collect_events(session, "buscame info de NGAVS en la red"))

    event_types = [e["type"] for e in events]
    # Debe terminar en "done" (no colgarse ni loopear infinito), pero con
    # un system-note final explicito avisando que hizo falta intervencion.
    assert event_types[-1] == "done"
    final_notes = [
        e
        for e in events
        if e["type"] == "system-note"
        and "dijo varias veces" in e["data"]["text"].lower()
    ]
    assert len(final_notes) == 1

    # No debe haber reintentado mas de _MAX_EMPTY_PROMISE_RETRIES + 1 veces
    # en total (los reintentos automaticos + el intento final que se rinde).
    assert (
        fake_client.chat.completions.call_count
        == loop_module._MAX_EMPTY_PROMISE_RETRIES + 1
    )


def test_normal_turn_without_tools_is_unaffected(monkeypatch):
    """No debe romper el caso feliz mas comun: un pedido `analitica_simple`/
    `conversacional` que el modelo responde con texto y SIN tool_calls no
    debe activar el guardrail (el guardrail solo aplica a `planificable`)."""
    chunks = [
        _text_chunk("[[TIPO: conversacional]]"),
        _text_chunk("¡Hola! Todo bien, ¿en qué te ayudo hoy?"),
        _stop_chunk(),
    ]
    fake_client = _FakeClient([chunks])
    monkeypatch.setattr(
        loop_module, "get_client", lambda force_refresh=False, **_k: fake_client
    )

    session = _make_session()
    events = asyncio.run(_collect_events(session, "hola"))

    event_types = [e["type"] for e in events]
    assert "done" in event_types
    assert "system-note" not in event_types
    assert fake_client.chat.completions.call_count == 1
