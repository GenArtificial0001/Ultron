"""spec informe-tecnico-hablado REQ-012/013/015: el NIVEL llega de punta a punta -- request ->
turno -> narrador en vivo y -> instruccion que recibe el modelo. Un el proveedor de IA falso escribe una pagina
mandando los argumentos de a pedazos, como el real."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import loop as loop_module
from app.session.events import SessionState

PAGE = (
    "<!-- tablero de stock: encabezado con titulo, menu lateral y cuatro indicadores -->\n"
    "<header><h1>Stock</h1></header>\n<footer>la empresa</footer>\n"
    "<style>:root{--bg:#0b0f14} body{font-family:Inter,sans-serif;display:grid}</style>\n"
    "<script>\n// calcula el total por pieza\nfunction totalPorPieza(x){return x}\n</script>\n"
)


@dataclass
class _Session(SessionState):
    pushed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def push_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.pushed.append((event_type, dict(data)))
        super().push_event(event_type, data)


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        pass


def _chunk(content=None, tool_calls=None, finish=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls or []), finish_reason=finish)])


def _write_in_pieces(path: str, content: str, size: int = 11) -> list:
    raw = json.dumps({"path": path, "content": content})
    out = []
    for i in range(0, len(raw), size):
        piece = raw[i : i + size]
        fn = SimpleNamespace(name="write" if i == 0 else None, arguments=piece)
        out.append(_chunk(tool_calls=[SimpleNamespace(index=0, id="call_w", function=fn)]))
    out.append(_chunk(finish="tool_calls"))
    return out


class _Client:
    def __init__(self):
        self.requests: list[list[dict]] = []
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs.get("messages") or [])
        self.calls += 1
        if self.calls == 1:
            return _Stream(_write_in_pieces(str(self.target), PAGE))
        return _Stream([_chunk(content="[[TIPO: conversacional]]Listo."), _chunk(finish="stop")])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(loop_module, "get_settings", lambda: type("S", (), {"llm_model": "test-model"})())

    async def _no_compact(*a, **k):
        return False

    import app.main as main_module
    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _no_compact)
    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setattr(main_module, "list_models", lambda: main_module._MODELS_CACHE.__setitem__("by_id", {}))


def run(monkeypatch, tmp_path, **kwargs):
    client = _Client()
    client.target = tmp_path / "tablero.html"
    monkeypatch.setattr(loop_module, "get_client", lambda force_refresh=False, **_k: client)
    session = _Session(session_id="nivel-turno")

    async def collect():
        return [e async for e in loop_module.run_turn(session, "armame el tablero", "nivel-turno", **kwargs)]

    events = asyncio.run(collect())
    spoken = [e["data"]["text"] for e in events if e.get("type") == "narration"]
    return spoken, client, session


def first_user_text(client) -> str:
    for m in client.requests[0]:
        if m.get("role") == "user":
            c = m["content"]
            return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c)
    return ""


def test_detailed_level_narrates_footer_styles_and_functions(monkeypatch, tmp_path):
    spoken, _client, _s = run(monkeypatch, tmp_path, narrate=True, narrate_level="detailed")
    joined = " | ".join(spoken).lower()
    assert "pie de página" in joined and "encabezado de la página" in joined
    assert "aplico estilos" in joined and "fondo oscuro" in joined and "grilla" in joined
    assert "defino la función total por pieza" in joined and ", que calcula el total por pieza" in joined


def test_summary_level_keeps_the_report_of_before(monkeypatch, tmp_path):
    spoken, _client, _s = run(monkeypatch, tmp_path, narrate=True, narrate_level="summary")
    joined = " | ".join(spoken).lower()
    assert "escribo tablero.html" in joined
    for new in ("pie de página", "aplico estilos", "defino la función"):
        assert new not in joined


def test_without_narrate_nothing_is_said_whatever_the_level(monkeypatch, tmp_path):
    spoken, _client, _s = run(monkeypatch, tmp_path, narrate=False, narrate_level="detailed")
    assert spoken == []


def test_an_unknown_level_behaves_as_summary(monkeypatch, tmp_path):
    spoken, _client, session = run(monkeypatch, tmp_path, narrate=True, narrate_level="muy detallado")
    assert "pie de página" not in " | ".join(spoken).lower()
    assert session.narrate_level == "summary"


def test_detailed_asks_the_model_to_comment_everything_and_open_with_the_design(monkeypatch, tmp_path):
    _spoken, client, _s = run(monkeypatch, tmp_path, narrate=True, narrate_level="detailed")
    text = first_user_text(client)
    assert "[Informe técnico hablado]" in text
    assert "comentá TODO el código" in text and "comentario de cabecera" in text


def test_summary_does_not_ask_for_more_than_before(monkeypatch, tmp_path):
    _spoken, client, _s = run(monkeypatch, tmp_path, narrate=True, narrate_level="summary")
    text = first_user_text(client)
    assert "[Informe técnico hablado]" in text and "comentá TODO el código" not in text


def test_the_header_comment_is_the_first_thing_said_after_the_file_name(monkeypatch, tmp_path):
    spoken, _client, _s = run(monkeypatch, tmp_path, narrate=True, narrate_level="detailed")
    assert spoken[0] == "Escribo tablero.html"
    assert spoken[1].startswith("tablero de stock: encabezado con titulo")  # REQ-015: el diseno, primero


def test_chat_request_and_user_turn_carry_the_level():
    from app.main import ChatRequest
    from app.session.events import UserTurn

    assert ChatRequest(session_id="s", message="hola").narrate_level == "summary"
    assert ChatRequest(session_id="s", message="hola", narrate_level="detailed").narrate_level == "detailed"
    assert UserTurn(id="1", text="x").narrate_level == "summary"
