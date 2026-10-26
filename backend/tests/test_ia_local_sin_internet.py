"""IA local sin internet (caso real 2026-10-26, spec ia-local REQ-011).

El usuario abrio ULTRON sin internet, con Ollama y `qwen3:4b` en su PC, y ULTRON no respondio nada. Dos causas encadenadas:
1. Ollama corria con su ventana de fabrica (4.096 tokens) y recorto el pedido de ULTRON (11.078 tokens, casi todo
   instrucciones y herramientas) sin avisar: qwen3 «penso» 3 minutos y termino sin escribir nada. El chat quedo en blanco.
2. Despues, la compactacion del historial mandaba el resumen al proveedor PRINCIPAL (en la nube) con el nombre
   `ollama/qwen3:4b`: sin internet fallaba, el salto automatico le echaba la culpa a Ollama y lo pausaba 10 minutos.

Contrato que se prueba contra un Ollama FALSO por HTTP en 127.0.0.1 (el camino real: proveedor, cliente y red local; nunca un
motor de verdad, REQ-008):
- la compactacion y la lectura de PDF usan el cliente del proveedor del modelo, con el nombre que ese proveedor entiende;
- antes de mandar el pedido, ULTRON carga el modelo si hace falta, lee su ventana y, si no entra, lo dice en el chat con el
  arreglo (sin mandarlo y sin pausar el modelo, que no esta caido);
- con contexto suficiente, el turno anda igual que siempre;
- si el modelo termina sin escribir nada, el chat lo explica en lugar de quedar en blanco.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.llm import local_ai, model_fallback
from app.llm import loop as loop_module
from app.llm import providers as llm_providers
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import SessionState

SID = "sesion-ia-local-sin-internet"
MODEL = "ollama/qwen3:4b"


class FakeOllama:
    """Ollama falso: `/v1/models`, `/v1/chat/completions` (con y sin streaming), `/api/ps` y `/api/generate` (cargar).

    `window`: la ventana con la que «carga» el modelo. Arranca SIN el modelo cargado (como Ollama recien abierto): `/api/ps`
    no lo lista hasta que llega un `/api/generate` o un chat."""

    def __init__(self, window: int = 4096, answer: str = "hola desde la IA local"):
        self.window = window
        self.answer = answer
        self.loaded = False
        self.hits: dict[str, int] = {}
        self.chat_bodies: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, model: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                deltas = [{"role": "assistant", "content": fake.answer}] if fake.answer else [{"role": "assistant"}]
                for delta in deltas:
                    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                end = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())

            def do_GET(self):
                fake.hits[self.path] = fake.hits.get(self.path, 0) + 1
                if self.path == "/v1/models":
                    self._send(200, {"object": "list", "data": [{"id": "qwen3:4b", "object": "model"}]})
                elif self.path == "/api/ps":
                    loaded = [{"name": "qwen3:4b", "model": "qwen3:4b", "context_length": fake.window}] if fake.loaded else []
                    self._send(200, {"models": loaded})
                else:
                    self._send(404, {"error": "no existe"})

            def do_POST(self):
                fake.hits[self.path] = fake.hits.get(self.path, 0) + 1
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/generate":
                    fake.loaded = True
                    self._send(200, {"model": body.get("model"), "done": True, "done_reason": "load"})
                    return
                fake.chat_bodies.append(body)
                fake.loaded = True
                if body.get("stream"):
                    self._stream(body.get("model"))
                    return
                self._send(200, {
                    "id": "c1", "object": "chat.completion", "created": 0, "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": fake.answer}, "finish_reason": "stop"}],
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("LOCAL_AI", "on")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", _closed_port_url())
    local_ai.clear_caches()
    llm_providers.clear_caches()
    yield
    local_ai.clear_caches()
    llm_providers.clear_caches()


def _ollama(monkeypatch, **kwargs) -> FakeOllama:
    fake = FakeOllama(**kwargs)
    monkeypatch.setenv("OLLAMA_BASE_URL", fake.url)
    return fake


@pytest.fixture
def ollama_4096(monkeypatch):
    fake = _ollama(monkeypatch, window=4096)
    yield fake
    fake.close()


@pytest.fixture
def ollama_big(monkeypatch):
    fake = _ollama(monkeypatch, window=200_000)
    yield fake
    fake.close()


# ---------------------------------------------------------------------------------------------------------------------
# Causa 2: la compactacion y la lectura de PDF van al proveedor DEL MODELO
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def cloud_principal_offline(monkeypatch):
    """Como en la PC del usuario: el proveedor principal esta en la nube y no hay internet. Pedirle el cliente SIN modelo
    devuelve el de la nube, que no conecta."""
    import openai

    from app.llm import client as client_module

    real_get_client = client_module.get_client

    class OfflineCloud:
        def __init__(self):
            self.chat = type("C", (), {"completions": type("X", (), {"create": staticmethod(self._create)})()})()

        @staticmethod
        def _create(**_kwargs):
            import httpx

            raise openai.APIConnectionError(request=httpx.Request("POST", "https://nube.invalid/v1/chat/completions"))

    def get_client(force_refresh=False, model=None):
        return OfflineCloud() if model is None else real_get_client(force_refresh=force_refresh, model=model)

    monkeypatch.setattr(client_module, "get_client", get_client)


@pytest.fixture
def ollama_big_offline(ollama_big, cloud_principal_offline):
    return ollama_big


def test_compaction_summary_goes_to_the_local_engine_with_its_own_model_name(ollama_big_offline):
    ollama_big = ollama_big_offline
    from app.session import compaction

    summary = asyncio.run(compaction._summarize("usuario: hola\nasistente: hola", MODEL))
    assert summary == "hola desde la IA local"
    assert [b["model"] for b in ollama_big.chat_bodies] == ["qwen3:4b"], "el nombre sin el prefijo de ULTRON"


def test_pdf_reading_goes_to_the_local_engine_with_its_own_model_name(ollama_big_offline):
    ollama_big = ollama_big_offline
    from app import pdf_vision

    text = pdf_vision.complete_with_proveedor(MODEL, [{"role": "user", "content": "transcribí"}])
    assert text == "hola desde la IA local"
    assert [b["model"] for b in ollama_big.chat_bodies] == ["qwen3:4b"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011: la ventana del modelo cargado, antes de mandar el pedido
# ---------------------------------------------------------------------------------------------------------------------
def test_an_unloaded_ollama_model_is_loaded_to_learn_its_window(ollama_4096):
    assert local_ai.context_length("ollama", ollama_4096.url, "qwen3:4b") is None, "recien abierto: no esta cargado"
    assert local_ai.loaded_context_length("ollama", ollama_4096.url, "qwen3:4b") == 4096
    assert ollama_4096.hits.get("/api/generate") == 1
    assert ollama_4096.chat_bodies == [], "cargar no genera nada"


def test_a_loaded_model_is_not_loaded_again(ollama_4096):
    local_ai.loaded_context_length("ollama", ollama_4096.url, "qwen3:4b")
    local_ai.loaded_context_length("ollama", ollama_4096.url, "qwen3:4b")
    assert ollama_4096.hits.get("/api/generate") == 1


def test_a_request_bigger_than_the_window_gets_the_plain_explanation(ollama_4096):
    big = [{"role": "system", "content": "x" * 33_000}, {"role": "user", "content": "hola"}]
    problem = loop_module._local_context_problem(MODEL, big, None)
    assert problem is not None and problem.code == "MODELO-LOCAL-CONTEXTO-CORTO"
    assert "4.096" in problem.message and "11.001" in problem.message
    assert "OLLAMA_CONTEXT_LENGTH" in problem.message and "README" in problem.message


def test_the_tools_count_as_part_of_the_request(ollama_4096):
    small = [{"role": "user", "content": "hola"}]
    tools = [{"type": "function", "function": {"name": "t", "description": "d" * 12_000, "parameters": {}}}]
    assert loop_module._local_context_problem(MODEL, small, None) is None
    assert loop_module._local_context_problem(MODEL, small, tools) is not None


def test_a_request_that_fits_has_no_problem(ollama_big):
    big = [{"role": "system", "content": "x" * 33_000}, {"role": "user", "content": "hola"}]
    assert loop_module._local_context_problem(MODEL, big, None) is None


def test_cloud_models_are_never_checked(ollama_4096):
    big = [{"role": "system", "content": "x" * 33_000}]
    assert loop_module._local_context_problem("gemini/gemini-flash-latest", big, None) is None
    assert ollama_4096.hits.get("/api/ps", 0) == 0


# ---------------------------------------------------------------------------------------------------------------------
# El turno completo, como lo vivio el usuario (sin internet: ningun proveedor en la nube con clave)
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def turn_env(monkeypatch, tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261026073604")
    import app.main as main_module

    monkeypatch.setattr(main_module, "list_models", lambda: main_module._MODELS_CACHE.__setitem__("by_id", {}))
    monkeypatch.setattr(main_module, "resolve_context_window", lambda model: 200_000)
    return db.db_path_for(str(root))


def _run_turn(model: str = MODEL, fallback: bool = False):
    settings = model_fallback.FallbackSettings(enabled=fallback, order=[model], pause_minutes=10)
    session = SessionState(session_id=SID)

    async def collect():
        return [ev async for ev in loop_module.run_turn(session, "hola", SID, model=model)]

    original = model_fallback.load_settings
    model_fallback.load_settings = lambda: settings
    try:
        events = asyncio.run(collect())
    finally:
        model_fallback.load_settings = original
    assert writer.flush()
    return events


@pytest.mark.integration
@pytest.mark.parametrize("fallback", [False, True])
def test_with_a_short_window_the_turn_explains_and_never_sends_the_cut_request(turn_env, ollama_4096, fallback):
    events = _run_turn(fallback=fallback)
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1 and errors[0]["data"]["code"] == "MODELO-LOCAL-CONTEXTO-CORTO"
    assert "OLLAMA_CONTEXT_LENGTH" in errors[0]["data"]["message"]
    assert ollama_4096.chat_bodies == [], "un pedido que el motor recortaria no se manda"
    assert model_fallback.ModelPauses().paused_until(MODEL) is None, "el modelo no esta caido: no se pausa"


@pytest.mark.integration
def test_with_enough_context_the_local_model_answers_as_always(turn_env, ollama_big):
    events = _run_turn()
    text = "".join(e["data"].get("delta", "") for e in events if e["type"] in ("text-delta", "text"))
    assert "hola desde la IA local" in text or any("hola desde la IA local" in json.dumps(e) for e in events)
    assert not [e for e in events if e["type"] == "error"]
    assert [b["model"] for b in ollama_big.chat_bodies] == ["qwen3:4b"]


@pytest.mark.integration
def test_an_empty_answer_is_explained_instead_of_a_blank_chat(turn_env, monkeypatch):
    fake = _ollama(monkeypatch, window=200_000, answer="")
    try:
        events = _run_turn()
    finally:
        fake.close()
    notes = [e["data"]["text"] for e in events if e["type"] == "system-note"]
    assert any("terminó sin escribir ninguna respuesta" in n and "OLLAMA_CONTEXT_LENGTH" in n for n in notes), notes
    assert events[-1]["type"] == "done"


@pytest.mark.integration
def test_with_a_short_window_compaction_is_not_even_tried(turn_env, ollama_4096, monkeypatch):
    """Caso real: se intentaba resumir la conversacion para que entrara en 4.096 tokens, cuando solo las instrucciones y
    las herramientas ya no entraban. Compactar no lo arregla: el aviso va antes."""
    import app.session.compaction as compaction

    calls = []

    async def spy(*args, **_kwargs):
        calls.append(args)
        return False

    monkeypatch.setattr(compaction, "maybe_compact", spy)
    events = _run_turn()
    assert [e["data"]["code"] for e in events if e["type"] == "error"] == ["MODELO-LOCAL-CONTEXTO-CORTO"]
    assert calls == []


@pytest.mark.integration
def test_a_long_conversation_that_no_longer_fits_is_also_explained(turn_env, monkeypatch):
    """Las instrucciones y las herramientas entran, pero la conversacion acumulada no (y no se pudo compactar): tambien se
    avisa en vez de mandarla recortada."""
    import app.session.compaction as compaction

    async def no_compact(*_a, **_k):
        return False

    monkeypatch.setattr(compaction, "maybe_compact", no_compact)
    fake = _ollama(monkeypatch, window=60_000)
    session = SessionState(session_id=SID)
    session.messages = [
        {"role": "user", "content": "contame todo"},
        {"role": "assistant", "content": "x" * 200_000},
    ]
    settings = model_fallback.FallbackSettings(enabled=False, order=[MODEL], pause_minutes=10)
    monkeypatch.setattr(model_fallback, "load_settings", lambda: settings)

    async def collect():
        return [ev async for ev in loop_module.run_turn(session, "hola", SID, model=MODEL)]

    try:
        events = asyncio.run(collect())
        assert writer.flush()
    finally:
        fake.close()
    assert [e["data"]["code"] for e in events if e["type"] == "error"] == ["MODELO-LOCAL-CONTEXTO-CORTO"]
    assert fake.chat_bodies == []
