"""IA local (spec ia-local): Ollama y LM Studio como proveedores de ULTRON.

Cero servicios reales: un servidor HTTP falso en 127.0.0.1 imita la API compatible con OpenAI de Ollama (`/v1/models`,
`/v1/chat/completions`) y su `/api/ps`. Ningun test llama a un Ollama o LM Studio de verdad (REQ-008): conftest apaga la busqueda
(`LOCAL_AI=off`) y cada test la prende apuntando al servidor falso.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest

from app.llm import local_ai
from app.llm import providers as llm_providers

MODELS = [{"id": "qwen3:4b", "object": "model"}, {"id": "nomic-embed-text", "object": "model"}, {"id": "gemma3:1b", "object": "model"}]


class FakeOllama:
    """Servidor falso: cuenta los pedidos por ruta y responde como Ollama."""

    def __init__(self, context_length: int | None = 4096):
        self.hits: dict[str, int] = {}
        self.chat_bodies: list[dict] = []
        self.context_length = context_length
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

            def do_GET(self):
                fake.hits[self.path] = fake.hits.get(self.path, 0) + 1
                if self.path == "/v1/models":
                    self._send(200, {"object": "list", "data": MODELS})
                elif self.path == "/api/ps":
                    loaded = [{"name": "qwen3:4b", "model": "qwen3:4b", "context_length": fake.context_length}] if fake.context_length else []
                    self._send(200, {"models": loaded})
                else:
                    self._send(404, {"error": "no existe"})

            def do_POST(self):
                fake.hits[self.path] = fake.hits.get(self.path, 0) + 1
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.chat_bodies.append(body)
                if body.get("model") == "gemma3:1b" and body.get("tools"):
                    self._send(400, {"error": {"message": "registry.ollama.ai/library/gemma3:1b does not support tools", "type": "api_error"}})
                    return
                self._send(200, {
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hola desde la IA local"}, "finish_reason": "stop"}],
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

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
    # Sin motores de fabrica: cada test dice donde esta cada uno (nunca un Ollama real de la PC).
    monkeypatch.setenv("OLLAMA_BASE_URL", _closed_port_url())
    monkeypatch.setenv("LMSTUDIO_BASE_URL", _closed_port_url())
    local_ai.clear_caches()
    llm_providers.clear_caches()
    yield
    local_ai.clear_caches()
    llm_providers.clear_caches()


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setenv("OLLAMA_BASE_URL", fake.url)
    yield fake
    fake.close()


# REQ-001
def test_a_running_ollama_shows_up_by_itself_with_its_chat_models(ollama):
    providers = llm_providers.active_providers()
    assert "ollama" in providers
    p = providers["ollama"]
    assert p.label == "Ollama (local)" and p.local and p.base_url == ollama.url and not p.free
    assert llm_providers.provider_models(p) == ["qwen3:4b", "gemma3:1b"], "los de embeddings no son de chat"


def test_closing_the_engine_makes_it_disappear_after_its_pause(ollama, monkeypatch):
    assert "ollama" in llm_providers.active_providers()
    ollama.close()
    now = [time.monotonic() + local_ai.ALIVE_TTL + 1]
    monkeypatch.setattr(local_ai.time, "monotonic", lambda: now[0])
    assert "ollama" not in llm_providers.active_providers()


def test_local_ai_off_never_looks(ollama, monkeypatch):
    monkeypatch.setenv("LOCAL_AI", "off")
    assert "ollama" not in llm_providers.active_providers()
    assert ollama.hits.get("/v1/models", 0) == 0


# REQ-002
def test_a_stopped_engine_costs_at_most_the_probe_and_then_is_remembered():
    start = time.monotonic()
    assert llm_providers.active_providers().get("ollama") is None
    assert time.monotonic() - start < 2 * local_ai.PROBE_TIMEOUT + 1
    start = time.monotonic()
    llm_providers.active_providers()
    assert time.monotonic() - start < 0.5, "el apagado se recuerda DEAD_TTL: no se vuelve a esperar"


def test_a_live_engine_is_not_asked_again_on_every_call(ollama):
    for _ in range(5):
        llm_providers.active_providers()
    assert ollama.hits.get("/v1/models", 0) == 1


# REQ-003
def test_chat_goes_to_the_local_engine_with_a_placeholder_key(ollama):
    client = llm_providers.client_for("ollama/qwen3:4b")
    resp = client.chat.completions.create(model=llm_providers.api_model("ollama/qwen3:4b"), messages=[{"role": "user", "content": "hola"}])
    assert resp.choices[0].message.content == "hola desde la IA local"
    assert ollama.chat_bodies[-1]["model"] == "qwen3:4b"


def test_a_model_without_tools_gets_the_plain_explanation(ollama):
    from app.friendly_errors import describe_model_error

    client = llm_providers.client_for("ollama/gemma3:1b")
    with pytest.raises(openai.BadRequestError) as info:
        client.chat.completions.create(
            model="gemma3:1b", messages=[{"role": "user", "content": "hola"}],
            tools=[{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {}}}}],
        )
    friendly = describe_model_error(info.value, "ollama/gemma3:1b")
    assert friendly.code == "MODELO-SIN-HERRAMIENTAS"
    assert "herramientas" in friendly.message and "Reenviar" in friendly.message


# REQ-004
def test_a_stopped_local_engine_is_a_fall_that_jumps_to_the_next_model():
    """REQ-004: motor local apagado = caida de ESE modelo (se pausa y el turno sigue con el siguiente de la lista)."""
    import httpx

    from app.llm import model_fallback

    settings = model_fallback.FallbackSettings(enabled=True, order=["ollama/qwen3:4b", "gemini/gemini-flash-latest"], pause_minutes=5)
    picker = model_fallback.TurnModelPicker(settings, "ollama/qwen3:4b", "ollama/qwen3:4b", {}, pauses=model_fallback.ModelPauses())
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"))
    assert picker.register_failure("ollama/qwen3:4b", exc) is True
    assert picker.choose(task_started=False).model == "gemini/gemini-flash-latest"


def test_a_connection_error_of_a_cloud_model_keeps_its_usual_behaviour():
    import httpx

    from app.llm import model_fallback

    settings = model_fallback.FallbackSettings(enabled=True, order=["anthropic/claude-sonnet-5", "gemini/gemini-flash-latest"], pause_minutes=5)
    picker = model_fallback.TurnModelPicker(settings, "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", {}, pauses=model_fallback.ModelPauses())
    exc = openai.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/chat/completions"))
    assert picker.register_failure("anthropic/claude-sonnet-5", exc) is False


# REQ-005
def test_local_engines_never_ask_for_the_free_consent(ollama):
    assert not llm_providers.is_free("ollama", "qwen3:4b")


def test_an_engine_on_another_pc_is_flagged(monkeypatch):
    assert local_ai.is_on_this_pc("http://127.0.0.1:11434/v1") and local_ai.is_on_this_pc("http://localhost:1234/v1")
    assert not local_ai.is_on_this_pc("http://192.168.0.20:11434/v1")


# REQ-006
def test_the_first_answer_of_a_local_model_gets_a_long_wait(ollama):
    client = llm_providers.client_for("ollama/qwen3:4b")
    timeout = client.timeout
    read = getattr(timeout, "read", timeout)
    assert read is None or read >= 180


# REQ-007
def test_without_local_engines_the_cloud_providers_are_exactly_as_before(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "clave-de-prueba")
    providers = llm_providers.active_providers()
    assert list(providers) == ["gemini"]


# REQ-009 / seguridad
@pytest.mark.parametrize(
    "raw", ["ftp://127.0.0.1:11434", "http://usuario:clave@127.0.0.1:11434/v1", "no-es-una-url", "http://127.0.0.1:99999/v1", ""]
)
def test_invalid_addresses_are_refused(raw):
    assert local_ai.normalize_url(raw) is None


def test_an_invalid_configured_address_falls_back_to_the_factory_one(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://usuario:clave@127.0.0.1:11434/v1")
    assert local_ai.base_url(local_ai.BY_ID["ollama"], llm_providers.env_value) == "http://127.0.0.1:11434/v1"


def test_the_probe_never_goes_through_the_company_proxy(ollama, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy-que-no-existe.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-que-no-existe.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    local_ai.clear_caches()
    assert local_ai.is_alive(ollama.url) is True


# REQ-011
def test_the_window_of_a_loaded_model_comes_from_the_engine(ollama):
    from app import main

    assert local_ai.context_length("ollama", ollama.url, "qwen3:4b") == 4096
    entry = main._model_entry("ollama/qwen3:4b", llm_providers.active_providers()["ollama"])
    assert entry["context_window"] == 4096 and entry["local"] is True


def test_a_model_that_is_not_loaded_has_an_unknown_window(monkeypatch):
    fake = FakeOllama(context_length=None)
    try:
        assert local_ai.context_length("ollama", fake.url, "qwen3:4b") is None
    finally:
        fake.close()
