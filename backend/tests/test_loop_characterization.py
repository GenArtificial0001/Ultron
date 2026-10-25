"""BL-024 fase 1: red de CARACTERIZACION de `app/llm/loop.py::_run_turn_impl` (1070 lineas, la funcion mas larga del repo).

Estos tests describen lo que el bucle HACE hoy, no lo que "deberia": son la red que permite partir la funcion mas adelante sin cambiar su comportamiento.
Cubren los tramos que ninguna otra prueba ejecutaba (medido: 36 sentencias sin cubrir): editar el mensaje mientras el agente trabaja, el stop real entre pasos,
los eventos de navegador al terminar el turno, el autonombre por adjunto y la clave de tiempos de una skill.

Cliente de chat FALSO (mismo patron que `test_empty_promise_retry.py`): un guion de respuestas por paso. Una respuesta puede ser una FUNCION: se ejecuta cuando el bucle
pide ese paso, y asi el test simula "el usuario edita / aprieta Detener justo en este momento" sin hilos ni `sleep`. Sin red, sin modelo real, sin persistencia (session_id vacio)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.llm import loop as loop_module
from app.session.events import SessionState
from app.tools import get_registry
from app.tools.base import Tool


# ---------------------------------------------------------------------------------------------------------------------
# Cliente falso
# ---------------------------------------------------------------------------------------------------------------------
class _Fn:
    def __init__(self, name: str = "", arguments: str = ""):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, index: int, id_: str, name: str, arguments: str):
        self.index, self.id, self.function = index, id_, _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []


class _Choice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.delta, self.finish_reason = _Delta(content, tool_calls), finish_reason


class _Chunk:
    def __init__(self, choice: _Choice):
        self.choices = [choice]


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)

    def close(self):  # pragma: no cover
        pass


def text(s: str):
    return [_Chunk(_Choice(content=s)), _Chunk(_Choice(finish_reason="stop"))]


def tool_call(name: str, arguments: str | dict, call_id: str = "call_1"):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return [_Chunk(_Choice(tool_calls=[_TC(0, call_id, name, args)])), _Chunk(_Choice(finish_reason="tool_calls"))]


class _Completions:
    def __init__(self, script: list):
        self.script, self.calls, self.requests = list(script), 0, []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        chunks = (
            step() if callable(step) else step
        )  # una funcion corre AL PEDIR ese paso (simula un evento del usuario)
        return _Stream(chunks)


class _Client:
    def __init__(self, script):
        self.completions = _Completions(script)
        self.chat = type("Chat", (), {"completions": self.completions})()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(loop_module, "get_settings", lambda: type("S", (), {"llm_model": "test-model"})())

    async def _no_compact(*a, **k):
        return False

    import app.main as main_module
    import app.session.compaction as compaction

    monkeypatch.setattr(compaction, "maybe_compact", _no_compact)
    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setattr(main_module, "list_models", lambda: main_module._MODELS_CACHE.__setitem__("by_id", {}))


@pytest.fixture
def hook_tool():
    """Registra una tool `hook` cuyo `execute` corre un callback: asi el test pide Detener/editar DURANTE la ejecucion de una tool, es decir
    despues del streaming y ANTES del siguiente paso (el chequeo de loop.py:1100). Se saca del registro al terminar."""
    registry = get_registry()
    state = {"callback": lambda session: None}

    def execute(args, session):
        state["callback"](session)
        return "hook-ok"

    registry.register(Tool(name="hook", description="tool de prueba", parameters={"type": "object", "properties": {}}, execute=execute, thread_safe=False))
    yield state
    registry._tools.pop("hook", None)


def hook_call():
    return tool_call("hook", {})


def run(
    script, user_message="hola", session: SessionState | None = None, **kwargs
) -> tuple[list[dict[str, Any]], SessionState, _Client]:
    client = _Client(script)
    loop_module.get_client = lambda force_refresh=False, **_k: client
    session = session or SessionState(session_id="")

    async def collect():
        out = []
        async for ev in loop_module.run_turn(session, user_message, "test-session-id", **kwargs):
            out.append(ev)
        return out

    return asyncio.run(collect()), session, client


def types(events) -> list[str]:
    return [e["type"] for e in events]


def last_user_text(request: dict) -> str:
    users = [m for m in request["messages"] if m.get("role") == "user"]
    c = users[-1]["content"]
    return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if isinstance(b, dict))


# ---------------------------------------------------------------------------------------------------------------------
# A) Turno normal: el orden de los eventos
# ---------------------------------------------------------------------------------------------------------------------
class TestNormalTurn:
    def test_a_plain_answer_ends_with_done_and_leaves_the_text_in_the_history(self):
        events, session, client = run([text("Hola, soy ULTRON.")])

        assert types(events)[-1] == "done"
        assert any(
            e["type"] == "text-delta" and "Hola" in e["data"].get("text", "") for e in events
        ) or "Hola, soy ULTRON." in str(events)
        assert client.completions.calls == 1
        assert session.messages[-1]["role"] == "assistant" and "Hola, soy ULTRON." in str(
            session.messages[-1]["content"]
        )

    def test_one_tool_call_runs_the_tool_then_asks_the_model_again(self):
        script = [
            tool_call("todowrite", {"todos": [{"content": "paso 1", "status": "in_progress", "priority": "high"}]}),
            text("Listo."),
        ]

        events, session, client = run(script)

        t = types(events)
        assert t.index("tool-call") < t.index("tool-result") < t.index("done")
        assert client.completions.calls == 2, "el modelo se vuelve a llamar con el resultado de la herramienta"
        roles = [m["role"] for m in session.messages]
        assert roles.index("tool") > roles.index("assistant"), "el resultado de la tool va despues de la peticion"

    def test_the_user_message_is_in_the_history_before_the_answer(self):
        _, session, _ = run([text("ok")], user_message="mi pregunta")

        users = [m for m in session.messages if m.get("role") == "user"]
        assert users and "mi pregunta" in str(users[-1]["content"])


# ---------------------------------------------------------------------------------------------------------------------
# B) Editar el mensaje MIENTRAS el agente trabaja (loop.py 1101-1122 y 1441-1449, sin cubrir hasta hoy)
# ---------------------------------------------------------------------------------------------------------------------
class TestEditWhileWorking:
    """El usuario edita su mensaje mientras el agente ya ejecuto una herramienta: el bucle NO termina ni reinicia, continua con el texto nuevo."""

    @staticmethod
    def _arm(hook_tool, new_text="mejor: dame el resumen"):
        def cb(session):
            session.edit_pending_text = new_text     # la UI marca stop + texto pendiente (PATCH /api/chat/{id}/message)
            session.stop_requested = True
        hook_tool["callback"] = cb

    def test_an_edit_during_a_tool_continues_the_same_turn_with_the_new_text(self, hook_tool):
        self._arm(hook_tool)

        events, _, client = run([hook_call(), text("Ok, resumen.")], user_message="dame el detalle")

        t = types(events)
        assert "message-edited" in t, t
        assert next(e for e in events if e["type"] == "message-edited")["data"]["text"] == "mejor: dame el resumen"
        assert t.index("message-edited") < t.index("done"), "el turno SIGUE despues de la edicion"
        assert "stopped" not in t and client.completions.calls == 2

    def test_after_an_edit_the_model_sees_the_new_text_and_its_own_previous_tool_result(self, hook_tool):
        self._arm(hook_tool, "texto nuevo")

        _, _, client = run([hook_call(), text("fin")], user_message="texto viejo")

        second = client.completions.requests[1]
        roles = [m["role"] for m in second["messages"]]
        assert last_user_text(second) == "texto nuevo"
        assert "tool" in roles, "el modelo conserva el progreso ya hecho (no se reinicia el turno)"
        assert roles[-1] == "user" and roles.index("tool") < len(roles) - 1, "el mensaje editado queda AL FINAL, despues del resultado de la tool"
        assert any(m.get("role") == "tool" and "hook-ok" in str(m.get("content")) for m in second["messages"])

    def test_the_edit_clears_the_pending_flags(self, hook_tool):
        self._arm(hook_tool, "nuevo")

        _, session, _ = run([hook_call(), text("fin")])

        assert session.edit_pending_text is None and session.stop_requested is False

    def test_the_old_user_text_is_gone_from_the_history_the_model_gets(self, hook_tool):
        self._arm(hook_tool, "texto nuevo")

        _, _, client = run([hook_call(), text("fin")], user_message="texto viejo")

        second_users = [m for m in client.completions.requests[1]["messages"] if m.get("role") == "user"]
        assert len(second_users) == 1 and "viejo" not in str(second_users[0]["content"])


# ---------------------------------------------------------------------------------------------------------------------
# C) Stop real
# ---------------------------------------------------------------------------------------------------------------------
class TestRealStop:
    def test_stop_during_a_tool_ends_the_turn_with_stopped_and_not_done(self, hook_tool):
        hook_tool["callback"] = lambda session: setattr(session, "stop_requested", True)

        events, _, client = run([hook_call(), text("no deberia llegar")])

        t = types(events)
        assert t[-1] == "stopped" and "done" not in t and "message-edited" not in t
        assert next(e for e in events if e["type"] == "stopped")["data"] == {"reason": "user_requested"}
        assert client.completions.calls == 1, "tras el stop no se vuelve a llamar al modelo"

    def test_the_history_is_consistent_after_a_stop_between_steps(self, hook_tool):
        """Cada tool-call del asistente tiene su resultado: un historial a medias haria fallar la siguiente llamada al modelo."""
        hook_tool["callback"] = lambda session: setattr(session, "stop_requested", True)

        _, session, _ = run([hook_call()])

        called = {tc["id"] for m in session.messages if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])}
        answered = {m["tool_call_id"] for m in session.messages if m.get("role") == "tool"}
        assert called and called == answered

    def test_a_stop_while_the_model_is_still_answering_cuts_before_any_tool_runs(self):
        """Tres puntos de chequeo en el bucle: este es el del STREAMING. La tool-call a medio llegar NO se ejecuta ni se guarda: historial [system, user]."""
        session = SessionState(session_id="")

        def step1():
            session.stop_requested = True
            return tool_call("todowrite", {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]})

        events, session, client = run([step1, text("fin")], session=session)

        assert types(events)[-1] == "stopped" and "tool-call" not in types(events)
        assert [m["role"] for m in session.messages] == ["system", "user"] and client.completions.calls == 1

    def test_stop_requested_is_reset_at_the_start_of_every_turn(self):
        session = SessionState(session_id="")
        session.stop_requested = True      # un Detener viejo, de un turno anterior

        events, _, _ = run([text("hola")], session=session)

        assert types(events)[-1] == "done", "un stop viejo no mata el turno nuevo"


# ---------------------------------------------------------------------------------------------------------------------
# D) Autonombre por adjunto (loop.py 877-879)
# ---------------------------------------------------------------------------------------------------------------------
class TestAutonameByAttachment:
    def test_without_text_the_session_is_named_after_the_first_attachment(self, monkeypatch):
        named: list[tuple[str, str]] = []
        monkeypatch.setattr(
            loop_module.persistence, "maybe_autoname_session", lambda sid, title: named.append((sid, title))
        )

        run(
            [text("ok")],
            user_message="",
            attachments=[
                {
                    "path": "attachment/stock.csv",
                    "name": "stock.csv",
                    "size": 8,
                    "mime": "text/csv",
                    "kind": "csv",
                    "inplace": False,
                }
            ],
        )

        assert named and "stock.csv" in named[-1][1]

    def test_with_text_the_session_is_named_after_the_text_not_the_attachment(self, monkeypatch):
        named: list[tuple[str, str]] = []
        monkeypatch.setattr(
            loop_module.persistence, "maybe_autoname_session", lambda sid, title: named.append((sid, title))
        )

        run(
            [text("ok")],
            user_message="analiza el stock de enero",
            attachments=[
                {
                    "path": "attachment/stock.csv",
                    "name": "stock.csv",
                    "size": 8,
                    "mime": "text/csv",
                    "kind": "csv",
                    "inplace": False,
                }
            ],
        )

        assert named and "analiza el stock" in named[-1][1] and "stock.csv" not in named[-1][1]


# ---------------------------------------------------------------------------------------------------------------------
# E) Clave de tiempos de una skill (loop.py 1626-1631)
# ---------------------------------------------------------------------------------------------------------------------
class TestSkillProgressKey:
    """loop.py:1626-1631: para la tool `skill` la barra de progreso usa la clave `skill:<nombre>` (el nombre sale de los argumentos). La clave viaja en el
    evento `progress-estimate` (campos `operation` y `label`); NO se pasa a `record_duration` desde el bucle (eso lo hace `tools/base.py` con el nombre de la tool)."""

    @staticmethod
    def _estimate(events):
        return next(e for e in events if e["type"] == "progress-estimate" and e["data"].get("id") == "call_1")["data"]

    def test_a_skill_load_uses_its_own_name_as_the_progress_key(self):
        events, _, _ = run([tool_call("skill", {"name": "skill-interna"}), text("ok")])

        est = self._estimate(events)
        assert est["operation"] == "skill:skill-interna" and "skill-interna" in est["label"]

    def test_a_skill_without_a_name_falls_back_to_the_tool_name(self):
        events, _, _ = run([tool_call("skill", {}), text("ok")])

        assert self._estimate(events)["operation"] == "skill"

    def test_a_skill_with_an_empty_name_falls_back_to_the_tool_name(self):
        events, _, _ = run([tool_call("skill", {"name": ""}), text("ok")])

        assert self._estimate(events)["operation"] == "skill"

    @pytest.mark.parametrize("arguments", ["no es json {", "[]", "null", "123", '"texto"'])
    def test_odd_skill_arguments_never_break_the_turn_and_fall_back_to_the_tool_name(self, arguments):
        """JSON invalido -> JSONDecodeError; JSON valido que NO es un objeto (lista, null, numero, texto) -> AttributeError en `.get`. Los dos estan atrapados."""
        events, _, _ = run([tool_call("skill", arguments), text("ok")])

        assert types(events)[-1] == "done"
        assert self._estimate(events)["operation"] == "skill"

    def test_other_tools_keep_their_own_name_as_the_key(self):
        events, _, _ = run([tool_call("todowrite", {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]}), text("ok")])

        assert self._estimate(events)["operation"] == "todowrite"


class TestEditDuringStreaming:
    """Segundo punto de chequeo (loop.py:1441): la edicion llega mientras el modelo todavia esta respondiendo. La respuesta a medias se descarta y el turno sigue."""

    def test_an_edit_while_the_model_is_answering_discards_the_partial_answer_and_continues(self):
        session = SessionState(session_id="")

        def step1():
            session.edit_pending_text = "texto nuevo"
            session.stop_requested = True
            return tool_call("todowrite", {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]})

        events, session, client = run([step1, text("Respuesta final.")], session=session, user_message="texto viejo")

        t = types(events)
        assert "message-edited" in t and t[-1] == "done" and "stopped" not in t and "tool-call" not in t[: t.index("message-edited")]
        assert client.completions.calls == 2
        assert last_user_text(client.completions.requests[1]) == "texto nuevo"
        assert session.edit_pending_text is None and session.stop_requested is False

    def test_the_partial_tool_call_of_the_cut_step_is_not_kept_in_the_history(self):
        session = SessionState(session_id="")

        def step1():
            session.edit_pending_text = "nuevo"
            session.stop_requested = True
            return tool_call("todowrite", {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]})

        _, session, _ = run([step1, text("fin")], session=session)

        assert not any(m.get("tool_calls") for m in session.messages if m.get("role") == "assistant")


class TestBrowserEvents:
    """loop.py:726-734 y 700-715: al terminar el turno se avisa si apareció o cambió la página de la vista previa. Se simula la huella (nombre, mtime) en vez de crear archivos."""

    @staticmethod
    def _snapshots(monkeypatch, before, after):
        seq = [before, after]
        monkeypatch.setattr(loop_module, "_browser_index_snapshot", lambda sid: seq.pop(0) if len(seq) > 1 else seq[0])

    def test_a_new_page_emits_page_created_before_the_terminal_event(self, monkeypatch):
        self._snapshots(monkeypatch, (None, None), ("index.html", 5.0))

        events, _, _ = run([text("ok")])

        t = types(events)
        assert t.index("browser-page-created") < t.index("done")
        assert next(e for e in events if e["type"] == "browser-page-created")["data"]["file"] == "index.html"

    def test_a_modified_page_emits_page_updated(self, monkeypatch):
        self._snapshots(monkeypatch, ("index.html", 5.0), ("index.html", 9.0))

        events, _, _ = run([text("ok")])

        assert "browser-page-updated" in types(events) and "browser-page-created" not in types(events)

    def test_an_untouched_page_emits_nothing(self, monkeypatch):
        self._snapshots(monkeypatch, ("index.html", 5.0), ("index.html", 5.0))

        events, _, _ = run([text("ok")])

        assert not any(t.startswith("browser-page") for t in types(events))

    def test_no_page_at_all_emits_nothing(self, monkeypatch):
        self._snapshots(monkeypatch, (None, None), (None, None))

        events, _, _ = run([text("ok")])

        assert not any(t.startswith("browser-page") for t in types(events))

    def test_the_event_is_emitted_only_once_even_with_several_terminal_events(self, monkeypatch):
        self._snapshots(monkeypatch, (None, None), ("index.html", 5.0))

        events, _, _ = run([text("ok")])

        assert types(events).count("browser-page-created") == 1

    def test_the_message_id_of_the_turn_travels_with_the_event(self, monkeypatch):
        self._snapshots(monkeypatch, (None, None), ("index.html", 5.0))

        events, _, _ = run([text("ok")], user_message_id="mi-turno")

        assert next(e for e in events if e["type"] == "browser-page-created")["data"]["message_id"] == "mi-turno"


# ---------------------------------------------------------------------------------------------------------------------
# F) Limites
# ---------------------------------------------------------------------------------------------------------------------
class TestLimits:
    def test_the_loop_cannot_run_forever(self):
        """Un modelo que SIEMPRE pide una herramienta termina en el limite de pasos, con un aviso, no en un bucle infinito."""
        script = [tool_call("todowrite", {"todos": [{"content": "x", "status": "in_progress", "priority": "high"}]})]

        events, _, client = run(script)

        assert client.completions.calls <= loop_module.MAX_STEPS + 1
        assert types(events)[-1] in {"done", "error"}
