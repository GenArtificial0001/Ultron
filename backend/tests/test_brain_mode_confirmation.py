"""Modo Cerebro (spec asistente-contextual-voz REQ-006/009/011): una tool
peligrosa (bash/write/edit) pedida en un turno DE VOZ no se ejecuta hasta que
el usuario la confirme hablando. Es la barrera de seguridad de la voz continua.

Se corre `run_turn` de verdad con un LLM FAKE que emite chunks guionados y un
registry con una tool `bash` ESPIA: asi se verifica lo que importa -- si la tool
se ejecuto o no -- sin tocar el shell, la red ni el proveedor de IA."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.llm import loop
from app.session.events import SessionState
from app.tools.base import Tool, ToolRegistry


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tc(index, cid, name, args):
    return SimpleNamespace(index=index, id=cid, function=SimpleNamespace(name=name, arguments=args))


def tool_call_turn(name, args='{"command": "borrar todo"}', cid="call_1"):
    return [_chunk(tool_calls=[_tc(0, cid, name, args)], finish="tool_calls")]


def text_turn(text):
    return [_chunk(content=text, finish="stop")]


class FakeStream:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __iter__(self):
        return self._it

    def close(self):
        pass


class FakeLLM:
    """Cada llamada a `chat.completions.create` consume el siguiente guion y
    registra los mensajes que recibio (para inspeccionar los prompts)."""

    def __init__(self, scripts):
        self.scripts, self.requests = list(scripts), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append([dict(m) for m in kwargs["messages"]])
        return FakeStream(self.scripts.pop(0))


class Harness:
    def __init__(self, monkeypatch, scripts, dangerous=True):
        self.executed: list[dict] = []
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="bash",
                description="espia",
                parameters={"type": "object", "properties": {}},
                execute=lambda args, session=None: self.executed.append(args) or "EJECUTADO",
                dangerous=dangerous,
            )
        )
        registry.register(
            Tool(
                name="read",
                description="espia",
                parameters={"type": "object", "properties": {}},
                execute=lambda args, session=None: "LEIDO",
            )
        )
        self.llm = FakeLLM(scripts)
        monkeypatch.setattr(loop, "get_registry", lambda: registry)
        monkeypatch.setattr(loop, "get_client", lambda *a, **k: self.llm)
        monkeypatch.setattr("app.tools.base.record_duration", lambda *a: None)
        monkeypatch.setattr("app.tools.base._audit", lambda *a: None)
        self.session = SessionState(session_id="voz-1")

    def turn(self, text, voice_confirm=True, images=None):
        async def go():
            return [
                e
                async for e in loop.run_turn(
                    self.session, text, "voz-1", voice_confirm=voice_confirm, images=images
                )
            ]

        return asyncio.run(go())


def results(events):
    return [e["data"]["result"] for e in events if e["type"] == "tool-result"]


def user_prompt(harness, request_index=0):
    """Contenido del ultimo mensaje 'user' que vio el LLM en esa llamada."""
    users = [m for m in harness.llm.requests[request_index] if m["role"] == "user"]
    content = users[-1]["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)


# ---- REQ-009: la tool peligrosa se intercepta -------------------------------
def test_dangerous_tool_by_voice_is_not_executed_and_asks_confirmation(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("¿Estás seguro?")])
    events = h.turn("borrá todo")
    assert h.executed == []  # LO IMPORTANTE: el shell nunca corrio
    assert "[Confirmación requerida, Modo Cerebro]" in results(events)[0]
    assert "Todavía NO se ejecutó" in results(events)[0]
    assert h.session.voice_pending_confirmation == {
        "name": "bash",
        "arguments": '{"command": "borrar todo"}',
    }
    assert any(e["type"] == "done" for e in events)


def test_same_tool_by_text_chat_executes_without_confirmation(monkeypatch):
    """Sin voice_confirm (chat de texto) el comportamiento NO cambia."""
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("hecho")])
    events = h.turn("borrá todo", voice_confirm=False)
    assert h.executed == [{"command": "borrar todo"}]
    assert results(events) == ["EJECUTADO"]
    assert h.session.voice_pending_confirmation is None


def test_non_dangerous_tool_by_voice_runs_normally(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("read", "{}"), text_turn("leido")])
    events = h.turn("leé el archivo")
    assert results(events) == ["LEIDO"] and h.session.voice_pending_confirmation is None


def test_tool_not_marked_dangerous_is_not_intercepted(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("ok")], dangerous=False)
    h.turn("corré esto")
    assert h.executed == [{"command": "borrar todo"}]


# ---- REQ-009: resolucion de la confirmacion en el turno siguiente ------------
def test_confirmation_turn_lets_the_retried_tool_run(monkeypatch):
    h = Harness(
        monkeypatch,
        [
            tool_call_turn("bash"),
            text_turn("¿Estás seguro?"),  # turno 1: se frena
            tool_call_turn("bash", cid="call_2"),
            text_turn("listo"),  # turno 2: el LLM reintenta tras el "si"
        ],
    )
    h.turn("borrá todo")
    assert h.executed == []
    events2 = h.turn("sí, dale")
    assert h.executed == [{"command": "borrar todo"}]  # ahora SI se ejecuta
    assert results(events2) == ["EJECUTADO"]
    assert h.session.voice_pending_confirmation is None  # el pendiente no sobrevive


def test_pending_confirmation_is_injected_into_the_next_prompt(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("¿seguro?"), text_turn("cancelado")])
    h.turn("borrá todo")
    h.turn("no, mejor no")
    prompt = user_prompt(h, request_index=2)
    assert "[Confirmación pendiente, Modo Cerebro]" in prompt
    assert "'bash'" in prompt and "borrar todo" in prompt
    assert "no, mejor no" in prompt


def test_negative_answer_leaves_nothing_pending_and_nothing_executed(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("¿seguro?"), text_turn("no lo hago")])
    h.turn("borrá todo")
    h.turn("no")  # el LLM NO reintenta la tool
    assert h.executed == [] and h.session.voice_pending_confirmation is None


def test_pending_never_survives_more_than_one_turn(monkeypatch):
    """Si el usuario cambia de tema en vez de responder, la tool no queda colgada."""
    h = Harness(
        monkeypatch,
        [
            tool_call_turn("bash"),
            text_turn("¿seguro?"),
            text_turn("son las 3"),  # turno 2: cambio de tema
            tool_call_turn("bash", cid="call_9"),
            text_turn("¿seguro?"),  # turno 3: pedido NUEVO de bash
        ],
    )
    h.turn("borrá todo")
    h.turn("qué hora es")
    assert h.session.voice_pending_confirmation is None
    h.turn("ahora sí borrá")
    assert h.executed == []  # turno 3 vuelve a frenarse, no hereda el bypass


def test_bypass_applies_to_a_single_turn_only(monkeypatch):
    h = Harness(
        monkeypatch,
        [
            tool_call_turn("bash"),
            text_turn("¿seguro?"),
            tool_call_turn("bash", cid="c2"),
            text_turn("hecho"),
            tool_call_turn("bash", cid="c3"),
            text_turn("¿seguro otra vez?"),
        ],
    )
    h.turn("borrá todo")
    h.turn("sí")
    assert len(h.executed) == 1
    h.turn("y borrá otra cosa")
    assert len(h.executed) == 1  # el bypass NO se filtro al turno siguiente


def test_text_turn_does_not_consume_a_pending_voice_confirmation(monkeypatch):
    h = Harness(monkeypatch, [tool_call_turn("bash"), text_turn("¿seguro?"), text_turn("hola")])
    h.turn("borrá todo")
    h.turn("hola", voice_confirm=False)  # un mensaje de TEXTO no es la respuesta hablada
    assert h.session.voice_pending_confirmation is not None
    assert "Confirmación pendiente" not in user_prompt(h, 2)


def test_parallel_dangerous_calls_are_all_paused_together(monkeypatch):
    three_calls = [
        _chunk(
            tool_calls=[
                _tc(0, "a", "bash", '{"command":"uno"}'),
                _tc(1, "b", "bash", '{"command":"dos"}'),
                _tc(2, "c", "read", "{}"),
            ],
            finish="tool_calls",
        )
    ]
    h = Harness(monkeypatch, [three_calls, text_turn("¿seguro?")])
    events = h.turn("hacé todo")
    assert h.executed == []  # ninguna de las dos peligrosas corrio
    out = results(events)
    assert out.count("LEIDO") == 1  # la no peligrosa si siguio su curso
    assert sum("Confirmación requerida" in r for r in out) == 2


# ---- REQ-011: instruccion de transcripcion ambigua ----------------------------
def test_voice_turn_gets_ambiguity_instruction(monkeypatch):
    h = Harness(monkeypatch, [text_turn("ok")])
    h.turn("eh, cómo es eso de la cosa")
    prompt = user_prompt(h)
    assert "[Modo Cerebro]" in prompt and "transcripción de voz" in prompt
    assert "eh, cómo es eso de la cosa" in prompt


def test_text_turn_never_gets_the_voice_instruction(monkeypatch):
    h = Harness(monkeypatch, [text_turn("ok")])
    h.turn("hola", voice_confirm=False)
    assert "Modo Cerebro" not in user_prompt(h)


def test_instruction_is_appended_to_multimodal_content(monkeypatch):
    """Con imagenes el ultimo mensaje user es una LISTA de bloques: la instruccion
    se agrega como un bloque de texto mas, sin romper el formato."""
    h = Harness(monkeypatch, [text_turn("ok")])
    h.turn("qué ves", images=["data:image/png;base64,AAA"])
    content = [m for m in h.llm.requests[0] if m["role"] == "user"][-1]["content"]
    assert isinstance(content, list)
    assert any(b.get("type") == "image_url" for b in content)
    assert "[Modo Cerebro]" in content[-1]["text"]


def test_session_state_defaults_are_safe():
    assert SessionState(session_id="x").voice_pending_confirmation is None
