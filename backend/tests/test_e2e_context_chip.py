"""E2E del chip de contexto y del aviso de conversacion larga en un Chromium REAL (spec aviso-contexto-largo, REQ-008/009).

Caso real (2026-10-23): una conversacion de 2.971.848 caracteres llego al limite del modelo SIN que Vision avisara (estimaba 74 %). Aca se mide,
contra el backend real con `/api/context` simulado por `page.route` (determinista: el porcentaje lo fija el test), que el chip este siempre
visible, cambie de estado con el nivel, se actualice al terminar un turno / cambiar de sesion / compactar, no muestre el % de otra sesion, y que
el aviso salga UNA vez al cruzar 75 %, no se repita y se rearme al bajar de 60 %.
LIMITE: se prueba el comportamiento y el texto, no que a la persona le resulte claro ni como se ve a ojo."""

from __future__ import annotations

import json

import pytest

from e2e_support import launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

CHIP = "#context-chip"


@pytest.fixture(scope="module")
def base_url():
    with serve_backend(None) as url:
        yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("ctxchip"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.pct = 10
        a.calls = []

        def handler(route):
            a.calls.append(route.request.url)
            pct = a.pct
            level = "critical" if pct >= 90 else "high" if pct >= 75 else "warn" if pct >= 60 else "ok"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "tokens": pct * 1000,
                        "context_window": 100_000,
                        "pct_used": pct,
                        "estimated": True,
                        "level": level,
                    }
                ),
            )

        a.page.route("**/api/context/**", handler)
        a.open()
        yield a


def chip_text(app) -> str:
    return app.page.inner_text(CHIP).strip()


def chip_state(app) -> str:
    return app.page.get_attribute(CHIP, "data-state")


def wait_chip(app, pct: int) -> None:
    app.page.wait_for_function("(p) => document.getElementById('context-chip').textContent.includes(p + '%')", arg=pct)


def trigger_refresh(app) -> None:
    """Cambia de sesion: es el camino real que vuelve a medir el contexto (`activateSession` -> `refreshContextUsage`)."""
    old = app.page.evaluate("() => document.getElementById('meta-session-id').textContent")
    app.page.evaluate("document.getElementById('acc-session').open = true")
    app.page.click("#session-new-btn")
    app.page.wait_for_function("(o) => document.getElementById('meta-session-id').textContent !== o", arg=old)


def touch_context(app) -> None:
    """Vuelve a medir el contexto por el camino REAL de la app: abrir el acordeon «Contexto» dispara `refreshContextUsage()`; se cierra y se
    reabre para forzar una lectura nueva sin cambiar de sesion."""
    app.page.evaluate("document.getElementById('acc-context').open = false")
    app.page.evaluate("document.getElementById('acc-context').open = true")


def pick_model(app, model_id: str) -> None:
    """Fija el modelo elegido como lo restaura la app al abrir (`vision.selectedModel` de localStorage) y recarga."""
    app.page.evaluate("(m) => localStorage.setItem('vision.selectedModel', m)", model_id)
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()


def run_compact(app) -> None:
    """`/compact` por la UI real: escribir el comando, Enter y «Confirmar»."""
    app.page.fill("#chat-input", "/compact")
    app.page.wait_for_selector("#slash-menu:not(.hidden)")
    app.page.press("#chat-input", "Enter")
    app.page.wait_for_selector("#command-modal-body .acc-action-btn:not(.secondary)")
    app.page.click("#command-modal-body .acc-action-btn:not(.secondary)")


def notes_with(app, fragment: str) -> int:
    return sum(1 for n in app.notes() if fragment in n)


WARNING = "llegando al límite"


class TestTheChip:
    def test_it_exists_and_is_visible_with_the_percentage(self, app):
        wait_chip(app, 10)
        assert app.page.is_visible(CHIP)
        assert "CONTEXTO" in chip_text(app)

    @pytest.mark.parametrize(
        "pct, state",
        [
            (0, "ok"),
            (59, "ok"),
            (60, "warn"),
            (74, "warn"),
            (75, "high"),
            (89, "high"),
            (90, "critical"),
            (100, "critical"),
        ],
    )
    def test_its_state_follows_the_level(self, app, pct, state):
        app.pct = pct
        trigger_refresh(app)
        wait_chip(app, pct)
        assert chip_state(app) == state

    def test_it_has_a_title_with_the_detail_and_says_it_is_an_estimate(self, app):
        app.pct = 42
        trigger_refresh(app)
        wait_chip(app, 42)
        title = app.page.get_attribute(CHIP, "title")
        assert "42.000" in title and "100.000" in title and "estimad" in title.lower()

    def test_it_does_not_keep_the_previous_sessions_percentage_when_it_cannot_be_read(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        app.page.unroute("**/api/context/**")
        app.page.route("**/api/context/**", lambda r: r.fulfill(status=503, body="{}", content_type="application/json"))
        trigger_refresh(app)
        app.page.wait_for_function("() => !document.getElementById('context-chip').textContent.includes('80%')")
        assert chip_state(app) == "unknown"
        assert "—" in chip_text(app)

    def test_it_asks_for_the_context_of_the_selected_model(self, app):
        pick_model(app, "modelo-elegido-xyz")
        app.calls.clear()
        touch_context(app)
        app.page.wait_for_function("() => true")
        app.page.wait_for_timeout(500)
        assert any("model=modelo-elegido-xyz" in u for u in app.calls), app.calls

    def test_it_is_not_clickable_and_blocks_nothing(self, app):
        wait_chip(app, 10)
        assert app.page.evaluate("document.getElementById('context-chip').tagName") in ("DIV", "SPAN")
        assert app.page.is_enabled("#send-btn") or app.page.get_attribute("#send-btn", "data-state") in ("idle", None)


class TestLateAnswers:
    def test_a_late_answer_from_the_previous_session_is_not_painted_on_the_new_one(self, app):
        """Cierra el mutante «sin la guarda de sesion»: la medicion de la sesion A llega DESPUES de cambiar a la B y no se pinta."""
        app.page.unroute("**/api/context/**")
        held = []

        def hold_first(route):
            # La primera lectura (sesion A) queda retenida; las siguientes (sesion B) responden 12 %.
            if not held:
                held.append(route)
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"tokens": 12_000, "context_window": 100_000, "pct_used": 12, "estimated": True, "level": "ok"}),
            )

        app.page.route("**/api/context/**", hold_first)
        touch_context(app)  # sesion A: queda retenida
        app.page.wait_for_function("() => true")
        app.page.wait_for_timeout(300)
        assert held, "la lectura de la sesion A no salio"
        trigger_refresh(app)  # cambia a la sesion B (responde 12 %)
        wait_chip(app, 12)

        held[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"tokens": 95_000, "context_window": 100_000, "pct_used": 95, "estimated": True, "level": "critical"}),
        )
        app.page.wait_for_timeout(800)
        assert "12%" in chip_text(app) and "95%" not in chip_text(app)
        assert chip_state(app) == "ok"
        assert notes_with(app, WARNING) == 0


class TestTheWarning:
    def test_it_shows_once_when_crossing_75_percent(self, app):
        app.pct = 76
        trigger_refresh(app)
        wait_chip(app, 76)
        app.wait_note(WARNING)
        note = next(n for n in app.notes() if WARNING in n)
        assert "/compact" in note and "/clear" in note

    def test_it_does_not_show_below_75(self, app):
        app.pct = 74
        trigger_refresh(app)
        wait_chip(app, 74)
        app.page.wait_for_timeout(500)
        assert notes_with(app, WARNING) == 0

    def test_it_does_not_repeat_in_the_same_session_while_it_stays_high(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        app.wait_note(WARNING)
        sid = app.page.evaluate("() => document.getElementById('meta-session-id').textContent")
        for pct in (82, 88, 95):
            app.pct = pct
            touch_context(app)
            wait_chip(app, pct)
        assert app.page.evaluate("() => document.getElementById('meta-session-id').textContent") == sid
        assert notes_with(app, WARNING) == 1

    def test_it_rearms_after_dropping_below_60(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        app.wait_note(WARNING)
        app.pct = 30
        touch_context(app)
        wait_chip(app, 30)
        app.pct = 85
        touch_context(app)
        wait_chip(app, 85)
        app.page.wait_for_function(
            "(f) => [...document.querySelectorAll('.msg.system-note')].filter(e => e.textContent.includes(f)).length === 2",
            arg=WARNING,
        )

    def test_dropping_between_60_and_75_does_not_rearm(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        app.wait_note(WARNING)
        app.pct = 65
        touch_context(app)
        wait_chip(app, 65)
        app.pct = 80
        touch_context(app)
        wait_chip(app, 80)
        app.page.wait_for_timeout(500)
        assert notes_with(app, WARNING) == 1

    def test_a_new_session_is_evaluated_again(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        app.wait_note(WARNING)
        trigger_refresh(app)  # sesion nueva, tambien al 80 %
        app.page.wait_for_function(
            "() => [...document.querySelectorAll('.msg.system-note')].some(e => e.textContent.includes('llegando al límite'))"
        )


class TestAfterCompact:
    def test_compacting_refreshes_the_chip_and_sends_the_selected_model(self, app):
        app.pct = 80
        trigger_refresh(app)
        wait_chip(app, 80)
        sent = []

        def compact(route):
            sent.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json", body='{"compacted": true}')

        app.page.route("**/api/commands/compact/**", compact)
        pick_model(app, "modelo-elegido-xyz")
        app.pct = 20
        run_compact(app)
        wait_chip(app, 20)
        assert sent and "modelo-elegido-xyz" in sent[0]

    def test_a_failed_compact_shows_the_friendly_text(self, app):
        app.page.route(
            "**/api/commands/compact/**",
            lambda r: r.fulfill(
                status=429,
                content_type="application/json",
                body=json.dumps(
                    {"detail": "No pude resumir la conversación. Llegaste al tope de consumo del proveedor de IA por un rato."}
                ),
            ),
        )
        run_compact(app)
        app.wait_note("tope de consumo")
