"""Salto automatico de modelos (spec `salto-automatico-modelos`, APPROVED 2026-09-25).

Unit: `llm/model_fallback.py` (quien elige el modelo de cada paso y cuando se
pausa uno) y `llm/history_compat.py` (historial aceptable para el modelo destino).
Integracion: el loop completo con un el proveedor de IA falso que responde distinto segun
el modelo pedido, y los endpoints de Configuracion. Cada test cita su REQ.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from app import main
from app.llm import loop as loop_module
from app.llm import model_fallback as mf
from app.llm.history_compat import history_for_model
from app.session.events import SessionState

_REQUEST = SimpleNamespace(method="POST", url="https://proveedor.test/v1/chat/completions")
OPUS, GPT, GEMINI, DEEPSEEK, NOT_SECRET = "claude-opus-5-5", "gpt-6-sol", "gemini-3.1-pro", "deepseekv41-flash", "modelo-no-apto"
SPEND = "Error code: 429 - Too much spend: model {m} is limited to $50 per 600 seconds"
# Texto EXACTO del 429 de la prueba real del usuario (backend/logs/app.log, 2026-09-25
# 15:37:47): tope del USUARIO, sin nombrar modelo, valido para todos los modelos pagos.
USER_SPEND = (
    "Error code: 429 - {'error': {'code': 'rate_limit_exceeded', 'message': \"Too many requests: you've reached the $3 per 600 seconds spend limit. This short-term guardrail resets automatically within a few minutes. On-prem models are free and not spend-limited — consider switching to one to keep working.\", 'param': '', 'type': 'rate_limit_error'}}"
)


def _catalog() -> dict[str, dict]:
    """Subconjunto del catalogo REAL del proveedor de IA (consultado el 2026-09-25):
    cada alias apunta a la misma entrada que su modelo."""
    entries = [
        {"id": OPUS, "aliases": ["claude-opus-latest"], "valid_args": ["temperature"], "secret_data_compliant": True, "hosted_on_prem": False},
        {"id": GPT, "aliases": ["openai-flagship-model", "gpt-6"], "valid_args": ["temperature", "verbosity"], "secret_data_compliant": True, "hosted_on_prem": False},
        {"id": GEMINI, "aliases": ["gemini-pro-latest"], "valid_args": ["temperature"], "secret_data_compliant": True, "hosted_on_prem": False},
        {"id": DEEPSEEK, "aliases": ["deepseek-latest"], "valid_args": ["temperature"], "secret_data_compliant": True, "hosted_on_prem": True},
        {"id": NOT_SECRET, "aliases": [], "valid_args": [], "secret_data_compliant": False, "hosted_on_prem": False},
    ]
    catalog = {}
    for entry in entries:
        catalog[entry["id"]] = entry
        for alias in entry["aliases"]:
            catalog[alias] = entry
    return catalog


def status_error(cls, status: int, text: str):
    return cls(text, response=SimpleNamespace(status_code=status, headers={}, request=_REQUEST), body=None)


def spend_429(model: str = OPUS):
    return status_error(openai.RateLimitError, 429, SPEND.format(m=model))


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(mf, "PAUSES", mf.ModelPauses(clock=fake))
    return fake


def picker(order=(OPUS, GPT, GEMINI, DEEPSEEK), selected=None, pauses=None, catalog=None):
    settings = mf.FallbackSettings(enabled=True, order=tuple(order), pause_minutes=10)
    return mf.TurnModelPicker(settings, selected, "claude-sonnet-latest", _catalog() if catalog is None else catalog, pauses)


# ---------------------------------------------------------------------------
# Unit: eleccion del modelo
# ---------------------------------------------------------------------------
pytestmark_unit = pytest.mark.unit


@pytestmark_unit
def test_aliases_resolve_to_the_real_model_and_the_first_is_home(clock):
    p = picker(order=("claude-opus-latest", "openai-flagship-model"))
    assert p.home == OPUS
    assert p.choose(task_started=False).model == OPUS


@pytestmark_unit
def test_a_model_chosen_by_hand_goes_first_and_is_not_repeated(clock):
    p = picker(order=("claude-opus-latest", "gpt-6"), selected=GPT)
    assert p.home == GPT
    p.register_failure(GPT, spend_429(GPT))
    assert p.choose(task_started=False).model == OPUS  # "gpt-6" es el mismo modelo: no se repite


@pytestmark_unit
@pytest.mark.parametrize(
    ("exc", "jumps"),
    [
        (spend_429(), True),  # REQ-002: limite de gasto
        (status_error(openai.InternalServerError, 503, "Error code: 503 - down"), True),  # REQ-002: caida
        (status_error(openai.InternalServerError, 504, "Error code: 504 - gateway"), True),  # 504 = caida
        (status_error(openai.BadRequestError, 400, "Error code: 400 - function call parts"), False),  # REQ-008
        (openai.APITimeoutError(request=_REQUEST), False),  # REQ-008: demora sin codigo
        # Cualquier 429 es un limite (decision "limite, caida"; la prueba real del
        # 2026-09-25 mostro un 429 que no se reconocia como tope y no saltaba).
        (status_error(openai.RateLimitError, 429, "Error code: 429 - slow down"), True),
        (status_error(openai.RateLimitError, 429, USER_SPEND), True),
        (status_error(openai.AuthenticationError, 401, "Error code: 401"), False),
    ],
    ids=["gasto-429", "caida-503", "caida-504", "rechazo-400", "timeout", "rate-429", "tope-usuario-real", "sesion-401"],
)
def test_only_limits_and_outages_trigger_the_jump(clock, exc, jumps):
    p = picker()
    assert p.register_failure(OPUS, exc) is jumps
    assert (mf.PAUSES.paused_until(OPUS) is not None) is jumps


@pytestmark_unit
def test_failure_pauses_the_model_and_the_next_choice_explains_it(clock):
    p = picker()
    assert p.register_failure(OPUS, spend_429())
    choice = p.choose(task_started=False)
    back_at = mf.hhmm(clock.now + 600)
    assert choice.model == GPT
    assert choice.notice == (
        f"{OPUS} llegó a su límite de uso por un rato. Sigo con {GPT} y vuelvo a probar {OPUS} a las {back_at}."
    )


@pytestmark_unit
def test_outage_notice_says_the_model_is_not_answering(clock):
    p = picker()
    p.register_failure(OPUS, status_error(openai.InternalServerError, 503, "Error code: 503"))
    assert f"{OPUS} no está respondiendo." in p.choose(task_started=False).notice


@pytestmark_unit
def test_pause_expires_and_the_model_comes_back_once_announced(clock):
    p = picker()
    p.register_failure(OPUS, spend_429())
    p.choose(task_started=False)
    clock.now += 5 * 60
    p2 = picker()  # otro turno, 5 minutos despues: sigue en pausa (REQ-003)
    assert p2.choose(task_started=False) == mf.Choice(model=GPT)
    clock.now += 6 * 60
    p3 = picker()  # 11 minutos despues: vuelve el primero, con aviso UNA vez
    assert p3.choose(task_started=False) == mf.Choice(model=OPUS, notice=f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo.")
    assert picker().choose(task_started=False) == mf.Choice(model=OPUS)


@pytestmark_unit
def test_all_paused_gives_one_clear_error_with_the_earliest_release(clock):
    p = picker(order=(OPUS, GPT))
    mf.PAUSES.pause(OPUS, 600)
    mf.PAUSES.pause(GPT, 120)
    choice = p.choose(task_started=False)
    assert choice.model is None
    assert choice.error.code == "MODELO-TODOS-EN-PAUSA"
    assert f"El primero en liberarse es {GPT}, a las {mf.hhmm(clock.now + 120)}." in choice.error.message


@pytestmark_unit
def test_models_missing_from_the_catalog_are_skipped(clock):
    p = picker(order=("modelo-dado-de-baja", GPT))
    assert p.choose(task_started=False).model == GPT


@pytestmark_unit
def test_without_catalog_names_are_tried_as_they_are(clock):
    p = picker(order=("claude-opus-latest",), catalog={})
    assert p.choose(task_started=False).model == "claude-opus-latest"


@pytestmark_unit
def test_empty_or_fully_missing_list_falls_back_to_home(clock):
    p = picker(order=("no-existe-1", "no-existe-2"))
    assert p.choose(task_started=False).model == "no-existe-1"


@pytestmark_unit
def test_gemini_is_skipped_mid_task_started_by_another_model(clock):
    p = picker(order=(OPUS, GEMINI, DEEPSEEK))
    p.mark_used(OPUS)
    p.register_failure(OPUS, spend_429())
    assert p.choose(task_started=True).model == DEEPSEEK  # REQ-009


@pytestmark_unit
def test_only_gemini_left_mid_task_stops_and_asks_for_a_new_message(clock):
    p = picker(order=(OPUS, GEMINI))
    p.mark_used(OPUS)
    p.register_failure(OPUS, spend_429())
    choice = p.choose(task_started=True)
    assert choice.model is None and choice.error.code == "MODELO-SALTO-PENDIENTE"
    # Mensaje nuevo: Gemini ya puede tomarlo desde el principio.
    assert picker(order=(OPUS, GEMINI)).choose(task_started=False).model == GEMINI


@pytestmark_unit
def test_gemini_continuing_its_own_task_is_fine(clock):
    p = picker(order=(GEMINI, DEEPSEEK))
    p.mark_used(GEMINI)
    assert p.choose(task_started=True).model == GEMINI


@pytestmark_unit
def test_jump_to_a_model_not_approved_for_secret_data_is_announced(clock):
    p = picker(order=(OPUS, NOT_SECRET))
    p.register_failure(OPUS, spend_429())
    notice = p.choose(task_started=False).notice
    assert notice.endswith(f"Atención: {NOT_SECRET} no está aprobado para datos secretos de tu empresa.")  # REQ-007


@pytestmark_unit
def test_status_items_show_alias_target_pause_and_missing(clock):
    mf.PAUSES.pause(GPT, 600)
    settings = mf.FallbackSettings(enabled=True, order=("claude-opus-latest", GPT, "dado-de-baja"), pause_minutes=10)
    items = mf.status_items(settings, _catalog())
    assert items[0] == {
        "model": "claude-opus-latest",
        "label": "claude-opus-latest",  # spec varios-proveedores: nombre legible (sin prefijo, igual)
        "resolved": OPUS,
        "missing": False,
        "paused_until": None,
        "secret_data_compliant": True,
    }
    assert items[1]["paused_until"] == mf.hhmm(clock.now + 600)
    assert items[2]["missing"] is True and items[2]["resolved"] is None


@pytestmark_unit
def test_catalog_choices_offer_latest_aliases_first():
    choices = mf.catalog_choices(_catalog())
    aliases = [c["value"] for c in choices if c["value"] != c["resolved"]]
    assert aliases == ["claude-opus-latest", "deepseek-latest", "gemini-pro-latest", "openai-flagship-model"]
    assert {"value": "claude-opus-latest", "label": f"claude-opus-latest → hoy {OPUS}", "resolved": OPUS} in choices
    assert "gpt-6" not in [c["value"] for c in choices]  # alias comun, no "latest": no se ofrece aparte


@pytestmark_unit
def test_normalize_order_drops_blanks_and_repeats_and_caps_length():
    assert mf.normalize_order([" a ", "", None, "a", "b"]) == ["a", "b"]
    assert len(mf.normalize_order([f"m{i}" for i in range(50)])) == mf.MAX_ORDER_LENGTH


@pytestmark_unit
def test_default_list_in_config_matches_the_module_default():
    from app.project_context import ProjectConfig

    assert tuple(ProjectConfig().model_fallback_order) == mf.DEFAULT_ORDER
    assert ProjectConfig().model_fallback_enabled is False  # REQ-006: apagado hasta que el usuario lo active


# ---------------------------------------------------------------------------
# Unit: historial compatible con el modelo destino (REQ-005)
# ---------------------------------------------------------------------------
GEMINI_ID = "call_1707955200_0__thought__" + "Ab+/9=" * 850


def _gemini_history() -> list[dict]:
    return [
        {"role": "system", "content": "p"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": GEMINI_ID, "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "call_1707955200_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": GEMINI_ID, "content": "r1"},
        {"role": "tool", "tool_call_id": "call_1707955200_1", "content": "r2"},
        {"role": "user", "content": "u2"},
        # Gemini repite el id en otro turno (caso real 2026-09-24).
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1707955200_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1707955200_1", "content": "r3"},
    ]


@pytestmark_unit
def test_history_for_claude_gets_safe_unique_ids_paired_with_their_results():
    original = _gemini_history()
    snapshot = json.dumps(original)
    sent = history_for_model(original, OPUS)

    calls = [c["id"] for m in sent if m.get("tool_calls") for c in m["tool_calls"]]
    results = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
    assert all(len(i) <= 40 and i.replace("_", "").replace("-", "").isalnum() for i in calls)
    assert len(set(calls)) == len(calls)  # sin ids repetidos
    assert results == calls  # cada respuesta sigue apuntando a SU llamada
    assert json.dumps(original) == snapshot  # session.messages no se toca


@pytestmark_unit
def test_history_for_gemini_keeps_its_signatures_untouched():
    original = _gemini_history()
    assert history_for_model(original, GEMINI) is original


@pytestmark_unit
def test_history_without_problems_is_returned_as_is():
    clean = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_ok_1", "type": "function"}]},
             {"role": "tool", "tool_call_id": "call_ok_1", "content": "r"}]
    assert history_for_model(clean, GPT) is clean


@pytestmark_unit
@pytest.mark.parametrize("assistant", [{"content": None}, {}], ids=["content-none", "sin-content"])
def test_tool_call_messages_without_text_get_empty_content_except_for_gemini(assistant):
    # Medido contra el proveedor real (2026-09-25): Claude responde 500 "'content'" con
    # content None/ausente en un assistant con tool-calls; con "" responde 200.
    history = [{"role": "assistant", **assistant, "tool_calls": [{"id": "call_ok_1", "type": "function"}]},
               {"role": "tool", "tool_call_id": "call_ok_1", "content": "r"}]
    assert history_for_model(history, OPUS)[0]["content"] == ""
    assert history_for_model(history, GEMINI) is history
    assert "content" not in history[0] or history[0]["content"] is None  # el original no se toca


# ---------------------------------------------------------------------------
# Integracion: el loop completo
# ---------------------------------------------------------------------------
class _Chunk:
    def __init__(self, *, content=None, tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]


def text_reply(text: str) -> list:
    return [_Chunk(content=f"[[TIPO: conversacional]]{text}"), _Chunk(finish_reason="stop")]


def tool_reply() -> list:
    args = json.dumps({"todos": [{"content": "Paso", "status": "in_progress", "priority": "high"}]})
    call = SimpleNamespace(index=0, id=f"call_{uuid.uuid4().hex[:8]}", function=SimpleNamespace(name="todowrite", arguments=args))
    return [_Chunk(content="[[TIPO: planificable]]"), _Chunk(tool_calls=[call]), _Chunk(finish_reason="tool_calls")]


class RoutingClient:
    """el proveedor de IA falso: cada modelo tiene una cola de respuestas (lista de chunks)
    o excepciones; registra cada llamada (modelo, kwargs)."""

    def __init__(self, script: dict[str, list]):
        self.script = {m: list(v) for m, v in script.items()}
        self.calls: list[tuple[str, dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        model = kwargs["model"]
        self.calls.append((model, kwargs))
        queue = self.script.get(model) or [text_reply(f"respuesta de {model}")]
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, BaseException):
            raise step
        return iter(step)

    @property
    def models(self) -> list[str]:
        return [m for m, _ in self.calls]


@pytest.fixture
def fallback_env(monkeypatch, clock):
    """Loop sin red: catalogo fijo, salto activo con la lista pedida."""
    monkeypatch.setattr(loop_module, "get_settings", lambda: SimpleNamespace(llm_model="claude-sonnet-latest", vision_company_name=""))

    async def _no_compact(*args, **kwargs):
        return False

    import app.session.compaction as compaction_module

    monkeypatch.setattr(compaction_module, "maybe_compact", _no_compact)
    monkeypatch.setattr(main, "resolve_context_window", lambda model: 200_000)
    monkeypatch.setitem(main._MODELS_CACHE, "by_id", _catalog())
    monkeypatch.setattr(main, "list_models", lambda: None)

    def configure(order=(OPUS, GPT, GEMINI, DEEPSEEK), enabled=True, script=None):
        settings = mf.FallbackSettings(enabled=enabled, order=tuple(order), pause_minutes=10)
        monkeypatch.setattr(mf, "load_settings", lambda: settings)
        client = RoutingClient(script or {})
        monkeypatch.setattr(loop_module, "get_client", lambda force_refresh=False, **_k: client)
        return client

    return configure


def run_turn(session=None, message="hola", model=None, generation_params=None) -> list[dict]:
    session = session or SessionState(session_id="")

    async def _collect():
        return [e async for e in loop_module.run_turn(session, message, "sesion-test", model=model, generation_params=generation_params)]

    return asyncio.run(_collect())


def switches(events):
    return [e["data"] for e in events if e["type"] == "model-switch"]


def text_of(events):
    return "".join(e["data"]["text"] for e in events if e["type"] == "text-delta")


@pytest.mark.integration
def test_limit_on_the_first_model_is_answered_by_the_next_in_the_same_turn(fallback_env, clock):
    client = fallback_env(script={OPUS: [spend_429()]})
    events = run_turn()

    assert client.models == [OPUS, GPT]  # REQ-002: mismo pedido, siguiente modelo
    assert text_of(events) == f"respuesta de {GPT}"
    assert events[-1]["type"] == "done"
    sw = switches(events)
    assert sw[0] == {"model": OPUS, "home": OPUS, "text": None, "resume_at": None}  # arranque: el chip sabe quien responde
    assert sw[1]["model"] == GPT and sw[1]["text"].startswith(f"{OPUS} llegó a su límite de uso por un rato.")


@pytest.mark.integration
def test_paused_model_is_not_called_again_until_the_pause_ends(fallback_env, clock):
    client = fallback_env(script={OPUS: [spend_429()]})
    run_turn()
    clock.now += 5 * 60
    run_turn()  # REQ-003: 5 minutos despues va directo al segundo
    assert client.models == [OPUS, GPT, GPT]
    clock.now += 6 * 60
    client.script[OPUS] = [text_reply("volvi")]
    events = run_turn()  # 11 minutos despues: vuelve a probar el primero
    assert client.models[-1] == OPUS and text_of(events) == "volvi"
    assert switches(events)[0]["text"] == f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo."


@pytest.mark.integration
def test_every_model_failing_ends_with_one_clear_message(fallback_env, clock):
    client = fallback_env(order=(OPUS, GPT), script={OPUS: [spend_429()], GPT: [spend_429(GPT)]})
    events = run_turn()
    assert client.models == [OPUS, GPT]  # sin bucle: cada uno una vez
    assert events[-1]["type"] == "error" and events[-1]["data"]["code"] == "MODELO-TODOS-EN-PAUSA"  # REQ-004


@pytest.mark.integration
def test_disabled_fallback_behaves_like_before(fallback_env, clock):
    client = fallback_env(enabled=False, script={"claude-sonnet-latest": [spend_429("claude-sonnet-latest")]})
    events = run_turn()
    assert client.models == ["claude-sonnet-latest"]  # REQ-006
    assert events[-1]["data"]["code"] == "MODELO-LIMITE"
    assert switches(events) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (status_error(openai.BadRequestError, 400, "Error code: 400 - Unsupported parameter"), "MODELO-RECHAZO"),
        (openai.APITimeoutError(request=_REQUEST), "MODELO-DEMORA"),
    ],
    ids=["400", "timeout"],
)
def test_rejections_and_timeouts_do_not_jump(fallback_env, clock, exc, code):
    client = fallback_env(script={OPUS: [exc]})
    events = run_turn()
    assert client.models == [OPUS]  # REQ-008
    assert events[-1]["data"]["code"] == code


@pytest.mark.integration
def test_failure_after_the_answer_started_does_not_switch_mid_answer(fallback_env, clock):
    def half_then_fail():
        yield _Chunk(content="[[TIPO: conversacional]]Empiezo...")
        raise status_error(openai.InternalServerError, 503, "Error code: 503 - cortado")

    client = fallback_env(script={OPUS: [half_then_fail()]})
    events = run_turn()
    assert client.models == [OPUS]
    assert events[-1]["type"] == "error" and events[-1]["data"]["code"] == "MODELO-CAIDO"


@pytest.mark.integration
def test_a_proveedor_cut_mid_answer_retries_the_same_model_without_jumping(fallback_env, clock):
    """spec fix-corte-proveedor REQ-002: un corte de la conexion a mitad de la respuesta (caso real 2026-10-14) no es un limite ni una
    caida del modelo: el paso se repite con el MISMO modelo, sin pausarlo ni avisar un salto."""

    def cut_mid_answer():
        yield _Chunk(content="[[TIPO: conversacional]]")
        raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")

    client = fallback_env(script={OPUS: [cut_mid_answer(), text_reply("ahora sí")]})
    events = run_turn()

    assert client.models == [OPUS, OPUS]
    assert text_of(events) == "ahora sí" and events[-1]["type"] == "done"
    assert [s["text"] for s in switches(events)] == [None], "solo el aviso de arranque: no hubo salto"
    assert mf.PAUSES.paused_until(OPUS) is None, "el modelo no queda en pausa por un corte"


@pytest.mark.integration
def test_mid_task_jump_skips_gemini_and_keeps_going(fallback_env, clock):
    client = fallback_env(order=(OPUS, GEMINI, DEEPSEEK), script={OPUS: [tool_reply(), spend_429()]})
    events = run_turn(message="planifica algo")
    assert client.models == [OPUS, OPUS, DEEPSEEK]  # REQ-009: Gemini no toma una tarea ajena
    assert events[-1]["type"] == "done"


@pytest.mark.integration
def test_mid_task_with_only_gemini_left_stops_with_an_explanation(fallback_env, clock):
    client = fallback_env(order=(OPUS, GEMINI), script={OPUS: [tool_reply(), spend_429()]})
    events = run_turn(message="planifica algo")
    assert client.models == [OPUS, OPUS]
    assert events[-1]["data"]["code"] == "MODELO-SALTO-PENDIENTE"
    events = run_turn(message="continuá")  # mensaje nuevo: ahora si Gemini
    assert client.models[-1] == GEMINI and events[-1]["type"] == "done"


@pytest.mark.integration
def test_history_sent_to_claude_is_sanitized_but_session_keeps_the_original(fallback_env, clock):
    client = fallback_env(order=(OPUS,))
    session = SessionState(session_id="")
    session.messages = _gemini_history()
    run_turn(session=session)
    sent = client.calls[0][1]["messages"]
    assert GEMINI_ID not in json.dumps(sent)  # REQ-005: Claude no recibe la firma
    assert session.messages[2]["tool_calls"][0]["id"] == GEMINI_ID  # la sesion la conserva


@pytest.mark.integration
def test_generation_params_are_refiltered_for_the_new_model(fallback_env, clock):
    client = fallback_env(script={OPUS: [spend_429()]})
    run_turn(generation_params={"verbosity": "low"})
    assert "verbosity" not in client.calls[0][1]  # claude no lo acepta
    assert client.calls[1][1]["verbosity"] == "low"  # gpt si


@pytest.mark.integration
def test_model_chosen_by_hand_jumps_along_the_list_and_is_marked(fallback_env, clock):
    client = fallback_env(order=(GPT, DEEPSEEK), script={OPUS: [spend_429()]})
    events = run_turn(model=OPUS)
    assert client.models == [OPUS, GPT]
    assert switches(events)[-1]["home"] == OPUS and switches(events)[-1]["model"] == GPT


# ---------------------------------------------------------------------------
# Integracion: Configuracion
# ---------------------------------------------------------------------------
@pytest.fixture
def api(monkeypatch, clock):
    monkeypatch.setitem(main._MODELS_CACHE, "by_id", _catalog())
    monkeypatch.setattr(main, "list_models", lambda: None)
    return TestClient(main.app)


@pytest.mark.integration
def test_config_defaults_to_disabled_with_an_empty_list(api):
    """spec varios-proveedores REQ-005: la lista arranca vacia (la arma el usuario)."""
    data = api.get("/api/config/model-fallback").json()
    assert data["enabled"] is False and data["pause_minutes"] == 10
    assert data["order"] == list(mf.DEFAULT_ORDER) == []
    assert data["items"] == []


@pytest.mark.integration
def test_config_marks_models_the_provider_no_longer_offers(api):
    """Un modelo de la lista que el proveedor ya no publica se marca."""
    from app import project_context as pc

    pc.set_model_fallback(order=["claude-opus-5-5", "claude-sonnet-5"])
    data = api.get("/api/config/model-fallback").json()
    items = {i["model"]: i for i in data["items"]}
    assert items["claude-opus-5-5"]["resolved"] == OPUS  # esta en el catalogo de prueba
    assert items["claude-sonnet-5"]["missing"] is True  # no esta: se saltea
    assert data["choices"][0]["value"] == "claude-opus-latest"
    assert api.get("/api/status").json()["model_fallback_enabled"] is False


@pytest.mark.integration
def test_saving_the_list_persists_it_cleaned_and_enables_auto(api):
    resp = api.post(
        "/api/config/model-fallback",
        json={"enabled": True, "order": [GPT, " ", GPT, "claude-opus-latest"], "pause_minutes": 15},
    )
    assert resp.status_code == 200
    data = api.get("/api/config/model-fallback").json()
    assert data["enabled"] is True and data["order"] == [GPT, "claude-opus-latest"] and data["pause_minutes"] == 15
    assert api.get("/api/status").json()["model_fallback_enabled"] is True


@pytest.mark.integration
def test_config_shows_which_model_is_paused(api):
    from app import project_context as pc

    pc.set_model_fallback(order=["claude-opus-5-5"])
    mf.PAUSES.pause(OPUS, 600)
    items = api.get("/api/config/model-fallback").json()["items"]
    assert next(i for i in items if i["model"] == "claude-opus-5-5")["paused_until"] is not None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"enabled": True, "order": []}, "Agregá al menos un modelo"),
        ({"pause_minutes": 0}, "entre 1 y 120"),
        ({"pause_minutes": 500}, "entre 1 y 120"),
    ],
    ids=["lista-vacia", "pausa-0", "pausa-500"],
)
def test_invalid_config_is_rejected_in_plain_language(api, body, fragment):
    resp = api.post("/api/config/model-fallback", json=body)
    assert resp.status_code == 400 and fragment in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Prueba real del usuario 2026-09-25: tope de consumo POR USUARIO + compactacion
# ---------------------------------------------------------------------------
USER_SPEND_EXC = lambda: status_error(openai.RateLimitError, 429, USER_SPEND)  # noqa: E731


@pytestmark_unit
def test_user_level_spend_limit_pauses_every_paid_model_and_goes_to_a_free_one(clock):
    p = picker(order=(OPUS, GPT, GEMINI, DEEPSEEK))
    assert p.register_failure(OPUS, USER_SPEND_EXC())
    choice = p.choose(task_started=False)
    assert choice.model == DEEPSEEK  # GPT y Gemini tambien son pagos: ni se prueban
    assert choice.notice.startswith(f"{OPUS} llegó al tope de consumo del servicio de inteligencia artificial.")
    assert all(mf.PAUSES.paused_until(m) for m in (OPUS, GPT, GEMINI))
    assert mf.PAUSES.paused_until(DEEPSEEK) is None


@pytestmark_unit
def test_user_level_limit_without_a_free_model_ends_in_the_all_paused_message(clock):
    p = picker(order=(OPUS, GPT))
    p.register_failure(OPUS, USER_SPEND_EXC())
    assert p.choose(task_started=False).error.code == "MODELO-TODOS-EN-PAUSA"


@pytestmark_unit
def test_user_level_limit_without_catalog_only_pauses_the_failed_model(clock):
    p = picker(order=(OPUS, GPT), catalog={})
    p.register_failure(OPUS, USER_SPEND_EXC())
    assert p.choose(task_started=False).model == GPT


@pytestmark_unit
def test_first_available_skips_paused_models_without_side_effects(clock):
    p = picker()
    mf.PAUSES.pause(OPUS, 600)
    assert p.first_available() == GPT
    assert p.choose(task_started=False) == mf.Choice(model=GPT)  # sin aviso consumido ni generado


@pytest.mark.integration
def test_real_user_level_429_jumps_straight_to_the_free_model(fallback_env, clock):
    client = fallback_env(order=(OPUS, GPT, GEMINI, DEEPSEEK), script={OPUS: [USER_SPEND_EXC()]})
    events = run_turn()
    assert client.models == [OPUS, DEEPSEEK]
    assert text_of(events) == f"respuesta de {DEEPSEEK}"
    assert "tope de consumo del servicio" in switches(events)[1]["text"]


def _failing_compaction(monkeypatch, exc_factory, calls: list):
    import app.session.compaction as compaction_module

    async def _boom(session_id, messages, context_window, model):
        calls.append(model)
        raise exc_factory()

    monkeypatch.setattr(compaction_module, "maybe_compact", _boom)


@pytest.mark.integration
def test_compaction_hitting_the_limit_no_longer_kills_the_turn(fallback_env, monkeypatch, clock):
    """Bug real 2026-09-25 15:38: con la conversacion larga, compactar llamaba al
    modelo en su tope -> 429 -> "Vision tuvo un problema interno" en cada mensaje."""
    client = fallback_env(order=(OPUS, GPT, GEMINI, DEEPSEEK))
    compact_calls: list[str] = []
    _failing_compaction(monkeypatch, USER_SPEND_EXC, compact_calls)
    events = run_turn()
    assert compact_calls == [OPUS]
    assert events[-1]["type"] == "done"
    assert client.models == [DEEPSEEK]  # la falla de compactar ya pauso los pagos
    assert "tope de consumo del servicio" in switches(events)[0]["text"]


@pytest.mark.integration
def test_compaction_uses_the_first_available_model(fallback_env, monkeypatch, clock):
    fallback_env(order=(OPUS, GPT))
    mf.PAUSES.pause(OPUS, 600)
    import app.session.compaction as compaction_module

    seen: list[str] = []

    async def _spy(session_id, messages, context_window, model):
        seen.append(model)
        return False

    monkeypatch.setattr(compaction_module, "maybe_compact", _spy)
    run_turn()
    assert seen == [GPT]


@pytest.mark.integration
def test_compaction_failure_without_fallback_does_not_kill_the_turn(fallback_env, monkeypatch, clock):
    client = fallback_env(enabled=False)
    calls: list[str] = []
    _failing_compaction(monkeypatch, lambda: RuntimeError("resumen caido"), calls)
    events = run_turn()
    assert events[-1]["type"] == "done"
    assert client.models == ["claude-sonnet-latest"]


# ---------------------------------------------------------------------------
# REQ-010 (prueba real del usuario 2026-09-26): la vuelta se ve al vencer la
# pausa -- la pausa vencia a las 10:58 y el chip y el aviso recien cambiaban al
# escribir de nuevo (11:02), porque las pausas solo se miraban al arrancar un turno.
# ---------------------------------------------------------------------------
def _settings(order=(OPUS, GPT, GEMINI, DEEPSEEK)):
    return mf.FallbackSettings(enabled=True, order=tuple(order), pause_minutes=10)


def check(selected=None, order=(OPUS, GPT, GEMINI, DEEPSEEK)):
    return mf.check_now(_settings(order), selected, "claude-sonnet-latest", _catalog())


@pytestmark_unit
def test_during_the_pause_check_reports_the_jump_and_when_it_ends(clock):
    mf.PAUSES.pause(OPUS, 600)
    assert check() == {"model": GPT, "home": OPUS, "text": None, "resume_at": clock.now + 600}


@pytestmark_unit
def test_when_the_pause_ends_check_announces_the_return_once(clock):
    mf.PAUSES.pause(OPUS, 600)
    clock.now += 601  # nadie escribio nada
    assert check() == {"model": OPUS, "home": OPUS, "text": f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo.", "resume_at": None}
    assert check()["text"] is None  # una sola vez
    assert picker().choose(task_started=False) == mf.Choice(model=OPUS)  # el proximo turno no lo repite


@pytestmark_unit
def test_a_turn_that_returns_first_leaves_nothing_to_announce_later(clock):
    mf.PAUSES.pause(OPUS, 600)
    clock.now += 601
    assert picker().choose(task_started=False).notice == f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo."
    assert check()["text"] is None


@pytestmark_unit
def test_resume_at_is_the_earliest_pause_of_the_list(clock):
    mf.PAUSES.pause(GEMINI, 900)
    mf.PAUSES.pause(OPUS, 600)
    assert check()["resume_at"] == clock.now + 600  # el primero que vuelve
    assert picker().resume_at() == clock.now + 600
    assert picker(order=(OPUS, GPT), catalog={}).resume_at() == clock.now + 600


@pytestmark_unit
def test_resume_at_ignores_models_missing_from_the_catalog_and_is_none_without_pauses(clock):
    assert check()["resume_at"] is None
    mf.PAUSES.pause("modelo-que-ya-no-existe", 600)
    assert picker(order=(OPUS, "modelo-que-ya-no-existe")).resume_at() is None


@pytestmark_unit
def test_with_everything_paused_check_has_no_model_but_says_when_the_first_returns(clock):
    mf.PAUSES.pause(OPUS, 600)
    mf.PAUSES.pause(GPT, 120)
    assert check(order=(OPUS, GPT)) == {"model": None, "home": OPUS, "text": None, "resume_at": clock.now + 120}


@pytestmark_unit
def test_a_model_chosen_by_hand_comes_back_to_the_chip_when_its_pause_ends(clock):
    mf.PAUSES.pause(GPT, 600)
    assert check(selected=GPT)["model"] == OPUS  # sigue con el primero de la lista
    clock.now += 601
    data = check(selected=GPT)
    assert (data["model"], data["home"], data["text"]) == (GPT, GPT, f"Ya pasó la pausa de {GPT}: vuelvo a usarlo.")


@pytest.mark.integration
def test_the_jump_event_tells_the_frontend_when_to_check_again(fallback_env, clock):
    fallback_env(script={OPUS: [spend_429()]})
    sw = switches(run_turn())
    assert sw[0]["resume_at"] is None  # arranque, sin pausas
    assert sw[1]["model"] == GPT and sw[1]["resume_at"] == clock.now + 600  # REQ-010


@pytest.mark.integration
def test_check_endpoint_follows_the_pause_and_announces_the_return_once(api, monkeypatch, clock):
    monkeypatch.setattr(mf, "load_settings", lambda: _settings())
    mf.PAUSES.pause(OPUS, 600)
    assert api.post("/api/model-fallback/check", json={}).json() == {
        "enabled": True, "model": GPT, "home": OPUS, "text": None, "resume_at": clock.now + 600,
    }
    clock.now += 601
    data = api.post("/api/model-fallback/check", json={"model": None}).json()
    assert data["model"] == OPUS and data["text"] == f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo."
    assert api.post("/api/model-fallback/check", json={}).json()["text"] is None


@pytest.mark.integration
def test_check_endpoint_with_the_jump_off_does_nothing(api, monkeypatch, clock):
    monkeypatch.setattr(mf, "load_settings", lambda: mf.FallbackSettings(enabled=False))
    mf.PAUSES.pause(OPUS, 600)
    clock.now += 601
    assert api.post("/api/model-fallback/check", json={}).json() == {
        "enabled": False, "model": None, "home": None, "text": None, "resume_at": None,
    }
    # No consumio el aviso: el proximo turno (si se vuelve a activar) lo da igual.
    assert picker().choose(task_started=False).notice == f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo."


@pytest.mark.integration
def test_check_endpoint_never_calls_the_provider(api, monkeypatch, clock):
    monkeypatch.setattr(mf, "load_settings", lambda: _settings(order=("claude-opus-latest", "deepseek-latest")))
    monkeypatch.setitem(main._MODELS_CACHE, "by_id", None)

    def _boom():
        raise AssertionError("no debe pedir el catalogo al proveedor de IA")

    monkeypatch.setattr(main, "list_models", _boom)
    data = api.post("/api/model-fallback/check", json={}).json()
    assert data["model"] == "claude-opus-latest"  # sin catalogo, el nombre tal cual
