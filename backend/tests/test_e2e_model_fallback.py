"""E2E del salto automatico de modelos en un Chromium REAL contra el backend real
(spec `salto-automatico-modelos`, APPROVED 2026-09-25).

Lo que ve el usuario: la lista de prioridad en Configuracion > Modelo e
inteligencia (REQ-001), la opcion "Automático" del selector, y el salto en el
chat -- el aviso y el chip con el modelo que responde de verdad (REQ-002/003).
El proveedor de IA es un fake que responde distinto segun el modelo pedido y
publica su lista de modelos por `GET /models`, como Anthropic u OpenAI (spec
vision-generico REQ-002: sin alias).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import openai
import pytest

from app import main
from app.llm import loop as loop_module
from app.llm import model_fallback as mf
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

OPUS, GPT, GEMINI, DEEPSEEK = "claude-opus-5-5", "gpt-6-sol", "gemini-3.1-pro", "deepseekv41-flash"
ROSTER = [OPUS, GPT, GEMINI, DEEPSEEK]


class FakeProvider:
    """Catalogo + chat que falla o responde segun `failing` (modelo -> excepcion)."""

    def __init__(self):
        self.failing: dict[str, BaseException] = {}
        self.called: list[str] = []
        self.audio = SimpleNamespace(transcriptions=FakeWhisper())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(id=m) for m in ROSTER])

    def _create(self, **kwargs):
        model = kwargs["model"]
        self.called.append(model)
        if model in self.failing:
            raise self.failing[model]
        delta = SimpleNamespace(content=f"[[TIPO: conversacional]]respuesta de {model}", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=stop, finish_reason="stop")]),
            ]
        )


def spend_429(model: str) -> openai.RateLimitError:
    text = f"Error code: 429 - Too much spend: model {model} is limited to $50 per 600 seconds"
    return openai.RateLimitError(text, response=SimpleNamespace(status_code=429, headers={}, request=None), body=None)


@pytest.fixture(scope="module")
def proveedor():
    return FakeProvider()


@pytest.fixture(scope="module")
def base_url(proveedor):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        # Otro modulo e2e pudo dejar cacheado un catalogo distinto (TTL 5 min).
        mp.setattr(main, "_MODELS_CACHE", {"data": None, "fetched_at": 0.0})
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url, proveedor):
    proveedor.failing.clear()
    proveedor.called.clear()
    mf.PAUSES.clear()
    with open_app(browser, base_url) as a:
        yield a
    mf.PAUSES.clear()


def enable_fallback(app, order=(OPUS, GPT)) -> None:
    resp = app.page.request.post(
        app.base_url + "/api/config/model-fallback",
        data=json.dumps({"enabled": True, "order": list(order)}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.ok, resp.text()


def open_model_settings(app) -> None:
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="model"]')
    app.page.wait_for_selector("#cfg-fallback-list .fallback-item")


def fallback_rows(app) -> list[dict]:
    return app.page.eval_on_selector_all(
        "#cfg-fallback-list .fallback-item",
        "els => els.map(e => ({model: e.dataset.model, info: e.querySelector('.fallback-info').textContent}))",
    )


def chip(app) -> str:
    return app.page.text_content("#meta-model .meta-val")


def send(app, text: str = "hola") -> None:
    # El boton solo envia en reposo (un turno anterior puede estar cerrandose).
    app.page.wait_for_function("() => document.querySelector('#send-btn').dataset.state === 'idle'")
    app.page.fill("#chat-input", text)
    app.page.click("#send-btn")


def wait_chip(app, text: str) -> None:
    # El chip se completa cuando responde /api/status (asincrono al cargar).
    app.page.wait_for_function(
        "t => document.querySelector('#meta-model .meta-val').textContent === t", arg=text
    )


# ---------------------------------------------------------------------------
# REQ-001: la lista de prioridad en Configuracion
# ---------------------------------------------------------------------------
def test_default_list_is_the_claude_family(app):
    """spec vision-generico: la lista precargada es la familia de Claude; un
    modelo que el proveedor no publica se marca en vez de romper."""
    app.open()
    open_model_settings(app)
    rows = fallback_rows(app)
    assert [r["model"] for r in rows] == list(mf.DEFAULT_ORDER)
    assert rows[0]["info"] == "disponible"  # claude-opus-5-5 esta en el catalogo falso
    assert rows[1]["info"] == "disponible"  # el modelo por defecto siempre esta en la lista
    assert rows[2]["info"] == "el proveedor ya no lo ofrece: se saltea"  # Haiku: el falso no lo publica
    assert app.page.is_checked("#cfg-fallback-enabled") is False  # REQ-006: apagado por defecto


def test_building_the_list_and_saving_turns_on_auto(app):
    app.open()
    open_model_settings(app)
    # quitar el ultimo, subir el segundo, agregar un modelo puntual
    app.page.locator("#cfg-fallback-list .fallback-item").nth(2).locator("button[title='Quitar de la lista']").click()
    app.page.locator("#cfg-fallback-list .fallback-item").nth(1).locator("button[title='Subir']").click()
    app.page.select_option("#cfg-fallback-add-select", DEEPSEEK)
    app.page.click("#cfg-fallback-add")
    app.page.check("#cfg-fallback-enabled")
    app.page.fill("#cfg-fallback-pause", "15")
    app.page.click("#cfg-fallback-save")

    app.page.wait_for_function("() => document.querySelector('#cfg-fallback-status').textContent === 'Guardado ✓'")
    expected = ["claude-sonnet-5", "claude-opus-5-5", DEEPSEEK]
    assert [r["model"] for r in fallback_rows(app)] == expected
    assert chip(app) == "Automático"
    saved = app.page.request.get(app.base_url + "/api/config/model-fallback").json()
    assert saved["enabled"] is True and saved["order"] == expected and saved["pause_minutes"] == 15


def test_enabling_with_an_empty_list_is_explained(app):
    app.open()
    open_model_settings(app)
    for _ in range(len(mf.DEFAULT_ORDER)):
        app.page.locator("#cfg-fallback-list .fallback-item").first.locator("button[title='Quitar de la lista']").click()
    app.page.check("#cfg-fallback-enabled")
    app.page.click("#cfg-fallback-save")
    app.page.wait_for_function("() => document.querySelector('#cfg-fallback-status').classList.contains('error')")
    assert app.page.text_content("#cfg-fallback-status").startswith("Agregá al menos un modelo")


# ---------------------------------------------------------------------------
# REQ-002/003: el salto visto desde el chat
# ---------------------------------------------------------------------------
def test_limit_jumps_to_the_next_model_with_a_notice_and_the_chip_follows(app, proveedor):
    enable_fallback(app)
    proveedor.failing[OPUS] = spend_429(OPUS)
    app.open()
    wait_chip(app, "Automático")

    send(app)
    app.wait_note(f"{OPUS} llegó a su límite de uso por un rato. Sigo con {GPT}")
    app.page.wait_for_function(
        "t => [...document.querySelectorAll('.msg.assistant .msg-text')].some(e => e.textContent.includes(t))",
        arg=f"respuesta de {GPT}",
    )
    assert proveedor.called == [OPUS, GPT]
    assert chip(app) == f"Automático · {GPT}"

    # El siguiente mensaje va directo al segundo, sin volver a golpear al primero ni repetir el aviso.
    notes_before = len(app.notes())
    send(app, "otra")
    app.page.wait_for_function(
        "t => [...document.querySelectorAll('.msg.assistant .msg-text')].filter(e => e.textContent.includes(t)).length >= 2",
        arg=f"respuesta de {GPT}",
    )
    assert proveedor.called == [OPUS, GPT, GPT]
    assert len(app.notes()) == notes_before


def test_selector_offers_auto_only_when_the_jump_is_on(app):
    app.open()
    app.page.click("#meta-model")
    app.page.wait_for_selector("#model-dropdown .model-option")
    assert app.page.locator("#model-dropdown [data-model='auto']").count() == 0
    app.page.keyboard.press("Escape")

    enable_fallback(app)
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    wait_chip(app, "Automático")
    app.page.click("#meta-model")
    app.page.wait_for_selector("#model-dropdown [data-model='auto']")
    assert app.page.locator("#model-dropdown .model-option").first.get_attribute("data-model") == "auto"


# ---------------------------------------------------------------------------
# REQ-010 (prueba real del usuario 2026-09-26): la vuelta se ve sola al vencer
# la pausa. La pausa vencia a las 10:58, pero el chip y el aviso "Ya pasó la
# pausa de..." recien cambiaban al escribir de nuevo (11:02). Las pausas duran
# unos segundos en estos tests: se prueba que la vuelta se VEA, no cuanto dura.
# ---------------------------------------------------------------------------
@pytest.fixture
def pause_for(monkeypatch):
    original = mf.ModelPauses.pause

    def set_seconds(seconds: float) -> None:
        monkeypatch.setattr(mf.ModelPauses, "pause", lambda self, model, _s: original(self, model, seconds))

    return set_seconds


def jump_to_gpt(app, proveedor) -> None:
    enable_fallback(app)
    proveedor.failing[OPUS] = spend_429(OPUS)
    app.open()
    wait_chip(app, "Automático")
    send(app)
    app.wait_note(f"{OPUS} llegó a su límite de uso por un rato. Sigo con {GPT}")
    proveedor.failing.clear()


def test_when_the_pause_ends_the_chip_and_the_notice_come_back_by_themselves(app, proveedor, pause_for):
    pause_for(5)
    jump_to_gpt(app, proveedor)
    assert chip(app) == f"Automático · {GPT}"

    app.wait_note(f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo.")  # sin escribir nada
    wait_chip(app, f"Automático · {OPUS}")
    assert proveedor.called == [OPUS, GPT]  # mostrar la vuelta no llama a ningun modelo

    notes = len(app.notes())
    send(app, "otra")
    app.page.wait_for_function(
        "t => [...document.querySelectorAll('.msg.assistant .msg-text')].some(e => e.textContent.includes(t))",
        arg=f"respuesta de {OPUS}",
    )
    assert proveedor.called == [OPUS, GPT, OPUS]
    assert len(app.notes()) == notes  # el aviso de vuelta no se repite


def test_reloading_during_the_pause_keeps_the_jump_on_the_chip_and_still_comes_back(app, proveedor, pause_for):
    pause_for(9)
    jump_to_gpt(app, proveedor)
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    wait_chip(app, f"Automático · {GPT}")  # la pagina nueva sabe que sigue el salto
    app.wait_note(f"Ya pasó la pausa de {OPUS}: vuelvo a usarlo.")
    wait_chip(app, f"Automático · {OPUS}")


def test_the_settings_list_shows_the_model_available_again_when_the_pause_ends(app, proveedor, pause_for):
    pause_for(6)
    jump_to_gpt(app, proveedor)
    open_model_settings(app)
    first = fallback_rows(app)[0]
    assert first["model"] == OPUS and "en pausa hasta" in first["info"]
    app.page.wait_for_function(
        "t => document.querySelector('#cfg-fallback-list .fallback-item .fallback-info').textContent === t",
        arg="disponible",
    )
