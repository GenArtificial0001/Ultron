"""spec navegador-integrado-ventana REQ-002 (CERRADO): el evento
`browser-page-created` sale SOLO del turno que crea la pagina de entrada de
la carpeta de vista previa (`backend/browser/<session_id>/`).

Se prueba a nivel de TURNO completo (`run_turn` con un el proveedor de IA falso que pide
tool-calls reales), no a nivel de `_write_execute`: la spec exige detectar la
pagina nueva sin importar la tool que la creo (`write`, `edit` o `bash`, ej.
plotly `fig.write_html`) -- la implementacion real (`_browser_index_snapshot`
+ comparacion antes/despues en el wrapper `run_turn`, ver `llm/loop.py`) emite
via `session.push_event` en el `finally` del turno, asi que el test recolecta
tanto lo yieldeado por el loop como lo empujado fuera de banda.

Los casos negativos son guardianes de NO REGRESION: una implementacion que
emitiera en cualquier `write` (data.json, el export de la spec
dashboards-autonomos-sesion, reescribir una pagina existente) seria el mismo
bug de auto-apertura que se corrigio."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.browser_workspace import get_browser_dir
from app.llm import loop as loop_module
from app.session.events import SessionState

EVENT = "browser-page-created"


@dataclass
class _RecordingSession(SessionState):
    """SessionState que ademas anota los eventos fuera de banda (`push_event`),
    que sin suscriptores SSE se descartarian."""

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
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tool_call_response(name: str, args: dict) -> list:
    call = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    return [_chunk(tool_calls=[call]), _chunk(finish="tool_calls")]


_FINAL_ANSWER = [
    _chunk(content="[[TIPO: conversacional]]Listo, quedó la vista previa."),
    _chunk(finish="stop"),
]


class _FakeClient:
    """Paso 1: la tool-call pedida. Paso 2 (y cualquier llamada extra): texto final."""

    def __init__(self, first_response: list) -> None:
        self._responses = [first_response, _FINAL_ANSWER]
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return _Stream(self._responses[idx])


@pytest.fixture(autouse=True)
def _patch_loop_dependencies(monkeypatch):
    """Mismo aislamiento que test_empty_promise_retry: nada de red ni del
    roster real de modelos (valid_args, context window, compactacion)."""
    monkeypatch.setattr(
        loop_module,
        "get_settings",
        lambda: type("S", (), {"llm_model": "test-model"})(),
    )

    async def _no_compact(*args, **kwargs):
        return False

    import app.main as main_module
    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _no_compact)
    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)

    def _fake_list_models():
        main_module._MODELS_CACHE["by_id"] = {}

    monkeypatch.setattr(main_module, "list_models", _fake_list_models)


def _run_turn_with(
    monkeypatch, first_response: list, session_id: str
) -> list[tuple[str, dict]]:
    """Corre un turno completo y devuelve TODOS los eventos: los que yieldea el
    loop y los que se empujan fuera de banda."""
    client = _FakeClient(first_response)
    monkeypatch.setattr(loop_module, "get_client", lambda force_refresh=False, **_k: client)
    session = _RecordingSession(session_id=session_id)

    async def collect():
        return [
            e
            async for e in loop_module.run_turn(
                session, "armame el tablero", session_id
            )
        ]

    yielded = asyncio.run(collect())
    assert client.calls >= 2, "el turno no llego a ejecutar la tool-call pedida"
    return [(e.get("type"), e.get("data") or {}) for e in yielded] + session.pushed


def _created(events) -> list[dict]:
    return [data for kind, data in events if kind == EVENT]


def _new_session_id() -> str:
    return f"preview-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Positivos (pendientes de implementacion)
# ---------------------------------------------------------------------------
def test_creating_the_preview_page_in_a_turn_emits_browser_page_created(monkeypatch):
    # REQ-002 CERRADO: run_turn compara una huella de find_index_file antes/
    # despues del turno completo y empuja 'browser-page-created' via
    # session.push_event si antes no existia y ahora si.
    sid = _new_session_id()
    page = get_browser_dir(sid) / "index.html"
    write = _tool_call_response(
        "write", {"path": str(page), "content": "<html><body>Tablero</body></html>"}
    )

    events = _run_turn_with(monkeypatch, write, sid)

    assert page.exists()
    created = _created(events)
    assert len(created) == 1, events  # una sola vez por turno
    assert created[0].get("file") == "index.html"


def test_a_page_generated_with_bash_also_emits_it(monkeypatch):
    # REQ-002 CERRADO: la deteccion es a nivel de TURNO, no de tool, asi
    # que dispara igual si la pagina la genero un script de bash.
    sid = _new_session_id()
    page = get_browser_dir(sid) / "index.html"
    if " " in sys.executable or " " in str(page):
        pytest.skip("rutas con espacios: cmd /c necesitaria otro quoteo")
    get_browser_dir(sid).mkdir(parents=True, exist_ok=True)
    code = f"open(r'{page}', 'w', encoding='utf-8').write('<html><body>plotly</body></html>')"
    bash_call = _tool_call_response(
        "bash", {"command": f'{sys.executable} -c "{code}"', "timeout": 60}
    )

    events = _run_turn_with(monkeypatch, bash_call, sid)

    assert page.exists(), "el comando de prueba no genero la pagina"
    assert [d.get("file") for d in _created(events)] == ["index.html"]


# ---------------------------------------------------------------------------
# Negativos: guardianes de NO REGRESION (pasan hoy)
# ---------------------------------------------------------------------------
def test_writing_a_data_file_in_the_preview_does_not_emit_it(monkeypatch):
    sid = _new_session_id()
    data_file = get_browser_dir(sid) / "data.json"
    write = _tool_call_response(
        "write", {"path": str(data_file), "content": '{"stock": 12}'}
    )

    events = _run_turn_with(monkeypatch, write, sid)

    assert data_file.exists()
    assert _created(events) == []


def test_writing_to_the_session_export_folder_does_not_emit_it(monkeypatch, tmp_path):
    # El export de la spec dashboards-autonomos-sesion vive en
    # <carpeta-sesion>/browser/, que NO es la carpeta de vista previa.
    sid = _new_session_id()
    export = tmp_path / "proyecto" / "browser" / "index.html"
    write = _tool_call_response(
        "write", {"path": str(export), "content": "<html><body>Export</body></html>"}
    )

    events = _run_turn_with(monkeypatch, write, sid)

    assert export.exists()
    assert _created(events) == []


def test_rewriting_a_page_that_already_existed_does_not_emit_created(monkeypatch):
    # "Pagina nueva" = no existia al empezar el turno. Reescribirla es una
    # actualizacion (REQ-007 la avisa aparte, sin abrir el panel solo).
    sid = _new_session_id()
    page = get_browser_dir(sid) / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<html><body>v1</body></html>", encoding="utf-8")
    write = _tool_call_response(
        "write", {"path": str(page), "content": "<html><body>v2</body></html>"}
    )

    events = _run_turn_with(monkeypatch, write, sid)

    assert Path(page).read_text(encoding="utf-8").endswith("v2</body></html>")
    assert _created(events) == []


# ---------------------------------------------------------------------------
# REQ-007: 'browser-page-updated' cuando la pagina YA existia y cambio.
# ---------------------------------------------------------------------------
def test_rewriting_an_existing_page_emits_updated_not_created(monkeypatch):
    sid = _new_session_id()
    page = get_browser_dir(sid) / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<html><body>v1</body></html>", encoding="utf-8")
    time.sleep(0.02)  # asegura un mtime distinto al de arriba (resolucion del FS)
    write = _tool_call_response(
        "write", {"path": str(page), "content": "<html><body>v2</body></html>"}
    )

    events = _run_turn_with(monkeypatch, write, sid)

    updated = [data for kind, data in events if kind == "browser-page-updated"]
    assert len(updated) == 1, events
    assert updated[0].get("file") == "index.html"
    assert _created(events) == []  # nunca los dos eventos a la vez
