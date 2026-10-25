"""E2E de Confluence (spec conexiones-con-nombre Fase B: REQ-006/010) en un Chromium REAL contra el backend real.

Se verifica lo que ve el usuario: la rama «Confluence» de Configuracion > Conexiones con sus secciones y la carga perezosa de hijos, y el arbol seleccionable
que aparece en el chat cuando el modelo usa las tools. Lo mas importante: TODO texto del wiki es dato no confiable. Un titulo con HTML o un `open_url` con
`javascript:` NO puede ejecutarse ni volverse un link; el token y el correo no llegan a la pagina. El proveedor de IA y Confluence son falsos."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app import connections as conns
from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
TOKEN = "TOKEN-E2E-CONFLUENCE-4a8e"
EMAIL = "persona.e2e@empresa.com"
XSS_TITLE = '<img src=x onerror="window.__xss=1">Procedimiento'
SLOW_S = 6  # un turno lento dura lo suficiente para hacer clic mientras Vision esta ocupada
SLOW_MARK = "DEMORA-A-PROPOSITO"  # palabra clave que hace al proveedor falso tardar SLOW_S (no es una palabra del idioma: no se confunde con una pregunta normal)


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class Provider:
    """Primer paso: pide confluence_get_page_tree; con el resultado, contesta texto."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        if SLOW_MARK in str(last_user.get("content", "")):
            time.sleep(SLOW_S)
            return iter([_chunk("[[TIPO: conversacional]]Respuesta lenta."), _chunk(finish="stop")])
        if any(m.get("role") == "tool" for m in messages):
            return iter([_chunk("[[TIPO: conversacional]]Listo, ahi esta el arbol."), _chunk(finish="stop")])
        call = SimpleNamespace(
            index=0,
            id="call_cf",
            function=SimpleNamespace(
                name="confluence_get_page_tree", arguments=json.dumps({"connection": "la empresa", "section": "Calidad"})
            ),
        )
        return iter([_chunk(tool_calls=[call]), _chunk(finish="tool_calls")])


class FakeConfluence:
    children_calls: list = []
    delay_s = 0  # demora de get_page_tree, para tener un turno de otra sesion todavia en curso

    def __init__(self, settings):
        self.settings = settings

    def test_connection(self):
        return True

    def get_page_tree(self, ref, max_depth=1, max_nodes=300):
        if FakeConfluence.delay_s:
            time.sleep(FakeConfluence.delay_s)
        return {
            "id": ref,
            "title": "Calidad raiz",
            "space_key": "QOS",
            "url": "https://empresa.atlassian.net/wiki/spaces/QOS/pages/42",
            "truncated": False,
            "children": [
                {
                    "id": "100",
                    "title": XSS_TITLE,
                    "space_key": "QOS",
                    "url": "javascript:window.__xss=2",
                    "children": [],
                },
                {
                    "id": "101",
                    "title": "Hija con nietas",
                    "space_key": "QOS",
                    "url": "https://empresa.atlassian.net/wiki/spaces/QOS/pages/101",
                    "children": [
                        {
                            "id": "102",
                            "title": "Nieta",
                            "space_key": "QOS",
                            "url": "https://empresa.atlassian.net/wiki/spaces/QOS/pages/102",
                            "children": [],
                        }
                    ],
                },
            ],
        }

    def list_child_pages(self, page_id, limit=250):
        FakeConfluence.children_calls.append((self.settings.confluence_base_url, page_id))
        return {
            "42": [{"id": "100", "title": XSS_TITLE}, {"id": "101", "title": "Hija"}],
            "101": [{"id": "102", "title": "Nieta"}],
        }.get(page_id, [])


@pytest.fixture(scope="module")
def connections_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("cf") / "connections.json"
    path.write_text(
        json.dumps(
            {
                "connections": [
                    {
                        "type": "confluence",
                        "name": "la empresa",
                        "fields": {
                            "confluence_base_url": "https://empresa.atlassian.net/wiki",
                            "confluence_space_key": "QOS",
                            "sections": [
                                {"name": "MP&L Procedures", "page_id": "611718870"},
                                {"name": "Calidad", "page_id": "42"},
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def base_url(connections_file):
    import app.connectors.confluence_connector as cc
    import app.main as main_module
    from app.config import Settings, get_settings
    from app.tools import confluence as tools_module

    provider = Provider()
    with pytest.MonkeyPatch.context() as mp:
        personal = Settings(_env_file=None, confluence_email=EMAIL, confluence_api_token=TOKEN)
        mp.setattr(conns, "CONNECTIONS_FILE", connections_file)
        mp.setattr(conns, "get_settings", lambda: personal)
        conns.reset_for_tests()
        mp.setattr(cc, "ConfluenceConnector", FakeConfluence)
        mp.setattr(main_module, "ConfluenceConnector", FakeConfluence)
        mp.setattr(tools_module, "ConfluenceConnector", FakeConfluence)
        mp.setattr(tools_module, "get_settings", lambda: personal)
        mp.setattr(loop_module, "get_client", lambda *a, **k: provider)
        _ = get_settings
        with serve_backend(provider) as url:
            yield url
    conns.reset_for_tests()


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture()
def app(browser, base_url):
    with open_app(browser, base_url) as a:
        a.open()
        yield a


def open_connections_tab(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_selector(".conn-status-row .conn-status-label", timeout=15_000)


def test_the_connections_tab_has_a_confluence_branch_with_its_sections(app):
    open_connections_tab(app)
    app.page.click("#conn-cf-toggle")
    app.page.wait_for_selector("#conn-cf-list > li:has-text('Confluence - la empresa')", timeout=15_000)

    app.page.click("#conn-cf-list > li:has-text('Confluence - la empresa')")

    app.page.wait_for_selector("#conn-cf-list li:has-text('MP&L Procedures')", timeout=10_000)
    assert "Calidad" in app.page.inner_text("#conn-cf-list")


def test_opening_a_section_loads_its_children_lazily(app):
    open_connections_tab(app)
    app.page.click("#conn-cf-toggle")
    app.page.click("#conn-cf-list > li:has-text('Confluence - la empresa')")
    app.page.wait_for_selector("#conn-cf-list li:text-is('📚 Calidad')", timeout=10_000)

    app.page.click("#conn-cf-list li:text-is('📚 Calidad')")

    app.page.wait_for_selector("#conn-cf-list li:text-is('📄 Hija')", timeout=10_000)


def test_a_wiki_title_with_html_is_shown_as_text_and_never_executes(app):
    open_connections_tab(app)
    app.page.click("#conn-cf-toggle")
    app.page.click("#conn-cf-list > li:has-text('Confluence - la empresa')")
    app.page.wait_for_selector("#conn-cf-list li:text-is('📚 Calidad')", timeout=10_000)
    app.page.click("#conn-cf-list li:text-is('📚 Calidad')")
    app.page.wait_for_selector("#conn-cf-list li:has-text('onerror')", timeout=10_000)

    text = app.page.inner_text("#conn-cf-list")

    assert "<img src=x" in text, "el HTML del titulo se ve como TEXTO"
    assert app.page.evaluate("window.__xss") is None, "el onerror del titulo NO se ejecuto"
    assert app.page.query_selector("#conn-cf-list img") is None, "no se creo ningun <img> a partir del titulo"


def test_no_credential_reaches_the_page(app):
    open_connections_tab(app)
    app.page.click("#conn-cf-toggle")
    app.page.wait_for_selector("#conn-cf-list > li:has-text('Confluence - la empresa')", timeout=15_000)

    html = app.page.content()

    assert TOKEN not in html and EMAIL not in html


def _ask_for_the_tree(app):
    app.page.fill("#chat-input", "mostrame el arbol de Calidad")
    app.page.click("#send-btn")
    app.page.wait_for_selector(".cf-chat-block", timeout=30_000)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=30_000)


def test_the_chat_shows_a_selectable_confluence_tree_with_open_links(app):
    _ask_for_the_tree(app)

    titles = app.page.eval_on_selector_all(".cf-chat-block .cf-chat-title", "els => els.map(e => e.textContent)")
    links = app.page.eval_on_selector_all(
        ".cf-chat-block a.cf-chat-open", "els => els.map(e => e.getAttribute('href'))"
    )

    assert "Calidad raiz" in titles and "Hija con nietas" in titles and "Nieta" in titles
    assert "https://empresa.atlassian.net/wiki/spaces/QOS/pages/42" in links


def test_a_javascript_url_from_the_wiki_never_becomes_a_link(app):
    _ask_for_the_tree(app)

    hrefs = app.page.eval_on_selector_all(".cf-chat-block a", "els => els.map(e => e.getAttribute('href'))")

    assert not [h for h in hrefs if h and h.lower().startswith("javascript:")]
    assert app.page.evaluate("window.__xss") is None


def test_a_title_with_html_in_the_chat_tree_is_text_only(app):
    _ask_for_the_tree(app)

    assert app.page.query_selector(".cf-chat-block img") is None
    assert app.page.evaluate("window.__xss") is None
    assert "<img src=x" in app.page.inner_text(".cf-chat-block")


def test_a_branch_can_be_collapsed_and_expanded(app):
    _ask_for_the_tree(app)
    nieta = app.page.locator(".cf-chat-block .cf-chat-title:text-is('Nieta')")
    assert nieta.is_visible()

    app.page.locator(
        ".cf-chat-block .cf-chat-row:has(.cf-chat-title:text-is('Hija con nietas')) .cf-chat-toggle"
    ).click()
    assert not nieta.is_visible()

    app.page.locator(
        ".cf-chat-block .cf-chat-row:has(.cf-chat-title:text-is('Hija con nietas')) .cf-chat-toggle"
    ).click()
    assert nieta.is_visible()


def test_clicking_a_page_asks_vision_to_open_it(app):
    _ask_for_the_tree(app)
    sent = []
    app.page.on(
        "request", lambda r: sent.append(r.post_data) if r.url.endswith("/api/chat") and r.method == "POST" else None
    )

    app.page.locator(".cf-chat-block .cf-chat-title:text-is('Hija con nietas')").click()
    app.page.wait_for_function(
        "[...document.querySelectorAll('.msg.user')].some(m => m.innerText.includes('Hija con nietas'))", timeout=15_000
    )

    assert any("Hija con nietas" in (body or "") and "101" in (body or "") for body in sent)


def test_a_click_while_vision_is_busy_does_not_queue_anything_behind_the_users_back(app):
    """Mientras Vision responde, el clic en una pagina del arbol NO manda un mensaje a escondidas. Se usa un turno realmente lento del proveedor falso."""
    _ask_for_the_tree(app)
    sent = []
    app.page.on("request", lambda r: sent.append(r.post_data) if r.url.endswith("/api/chat") and r.method == "POST" else None)
    app.page.fill("#chat-input", f"pregunta {SLOW_MARK} para ocupar a Vision")
    app.page.click("#send-btn")
    app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
    users_before = app.page.eval_on_selector_all(".msg.user", "els => els.length")
    posts_before = len(sent)

    app.page.locator(".cf-chat-block .cf-chat-title:text-is('Nieta')").click()
    app.page.wait_for_timeout(800)

    assert app.page.eval_on_selector_all(".msg.user", "els => els.length") == users_before, "no aparecio un mensaje nuevo del usuario"
    assert len(sent) == posts_before, "y no salio ningun POST /api/chat a escondidas"
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=40_000)


def test_expanding_a_page_asks_for_the_children_of_THAT_connection(app, connections_file):
    """El pedido de hijos lleva el nombre de la conexion: con varias conexiones de Confluence, nunca se piden a la equivocada."""
    FakeConfluence.children_calls.clear()
    open_connections_tab(app)
    requests = []
    app.page.on("request", lambda r: requests.append(r.url) if "/api/confluence/children" in r.url else None)
    app.page.click("#conn-cf-toggle")
    app.page.click("#conn-cf-list > li:has-text('Confluence - la empresa')")
    app.page.wait_for_selector("#conn-cf-list li:text-is('📚 Calidad')", timeout=10_000)
    app.page.click("#conn-cf-list li:text-is('📚 Calidad')")
    app.page.wait_for_selector("#conn-cf-list li:text-is('📄 Hija')", timeout=10_000)

    assert requests and all("connection=la empresa" in u for u in requests), requests
    assert any("id=42" in u for u in requests)


def test_a_tree_from_another_session_is_not_painted_in_the_current_chat(app):
    """El arbol de un turno que sigue corriendo en OTRA sesion no puede aparecer en la conversacion que se esta mirando.

    Se lanza un turno cuya herramienta tarda, se cambia a una sesion nueva mientras corre, y se espera a que el evento `confluence-tree` del turno viejo llegue:
    la sesion nueva debe seguir limpia."""
    FakeConfluence.delay_s = 4
    try:
        app.page.fill("#chat-input", "mostrame el arbol de Calidad")
        app.page.click("#send-btn")
        app.page.wait_for_selector("#send-btn[data-state=active]", timeout=10_000)
        app.page.evaluate("document.getElementById('acc-session').open = true")  # el boton vive dentro de un acordeon plegado
        app.page.click("#session-new-btn")  # cambia a una sesion nueva MIENTRAS el turno viejo sigue en curso
        app.page.wait_for_selector(".msg.user", state="detached", timeout=10_000)

        app.page.wait_for_timeout((FakeConfluence.delay_s + 3) * 1000)  # el evento del turno viejo ya tuvo tiempo de llegar

        assert app.page.query_selector(".cf-chat-block") is None, "el arbol del turno de otra sesion se colo en esta conversacion"
    finally:
        FakeConfluence.delay_s = 0


def test_every_open_link_opens_without_giving_the_wiki_access_to_this_page(app):
    """`target=_blank` sin `rel=noopener` deja que la pagina abierta controle `window.opener`: los links de Confluence llevan noopener noreferrer."""
    _ask_for_the_tree(app)

    rels = app.page.eval_on_selector_all(".cf-chat-block a", "els => els.map(e => e.getAttribute('rel'))")

    assert rels and all(r and "noopener" in r and "noreferrer" in r for r in rels)


def test_after_the_connections_file_changes_the_confluence_branch_is_rebuilt(app, connections_file):
    """Tras cambiar connections.json, la rama de Confluence refleja la conexion nueva aunque la relectura del archivo llegue TARDE.

    Fallaba a veces en la suite completa (carga alta): al volver a la pestaña, la pantalla relee `/api/connections` y recien despues invalida lo que tenia armado;
    si el usuario abria la rama antes de que esa respuesta llegara, veia la lista vieja. Aca la demora se fuerza: la PRIMERA respuesta de `/api/connections` tras
    volver a la pestaña queda retenida hasta despues del clic."""
    original = connections_file.read_text(encoding="utf-8")
    open_connections_tab(app)
    app.page.click("#conn-cf-toggle")
    app.page.wait_for_selector("#conn-cf-list > li:has-text('Confluence - la empresa')", timeout=15_000)
    data = json.loads(original)
    extra = json.loads(json.dumps(data["connections"][0]))
    extra["name"] = "Segunda"
    data["connections"].append(extra)
    connections_file.write_text(json.dumps(data), encoding="utf-8")

    held: list = []

    def hold_first(route):
        if not held:
            held.append(route)  # la primera relectura queda retenida
        else:
            route.continue_()

    app.page.route("**/api/connections", hold_first)
    try:
        app.page.click("#conn-cf-toggle")  # cierra
        app.page.click('.settings-tab[data-tab="model"]')
        app.page.click('.settings-tab[data-tab="connections"]')  # vuelve a la pestaña: la relectura queda en el aire
        app.page.wait_for_function("true")  # cede el control para que salga el pedido retenido
        app.page.click("#conn-cf-toggle")  # abre la rama ANTES de que llegue la relectura
        for route in held:
            route.continue_()  # ahora si llega
        app.page.wait_for_selector("#conn-cf-list > li:has-text('Confluence - Segunda')", timeout=15_000)
    finally:
        app.page.unroute("**/api/connections")
        connections_file.write_text(original, encoding="utf-8")


def test_the_confluence_block_has_the_standard_width_of_the_chat(app):
    """Pedido del usuario (2026-10-07): el bloque del arbol de Confluence se angostaba al ancho de su contenido, distinto del resto de los mensajes. Los mensajes del
    asistente son hijos de un contenedor flex con `align-self: flex-start`, que ajusta cada uno a su contenido: un bloque con titulos cortos quedaba mas angosto.
    Debe ocupar TODO el ancho util del chat, igual que un mensaje largo."""
    _ask_for_the_tree(app)

    measures = app.page.evaluate(
        """() => {
          const block = document.querySelector('.cf-chat-block');
          const box = document.getElementById('messages');
          const cs = getComputedStyle(box);
          const inner = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
          return { block: block.getBoundingClientRect().width, inner };
        }"""
    )

    assert abs(measures["block"] - measures["inner"]) <= 2, f"el bloque mide {measures['block']:.0f}px y el ancho util del chat es {measures['inner']:.0f}px"


def test_the_confluence_block_keeps_the_same_width_whatever_its_content(app):
    """Con un titulo muy corto y con uno largo el bloque mide lo mismo: el ancho no depende del contenido."""
    _ask_for_the_tree(app)
    first = app.page.evaluate("document.querySelector('.cf-chat-block').getBoundingClientRect().width")
    app.page.evaluate(
        """() => {
          const t = document.querySelector('.cf-chat-block .cf-chat-title');
          t.textContent = 'x'.repeat(5);
        }"""
    )
    short = app.page.evaluate("document.querySelector('.cf-chat-block').getBoundingClientRect().width")
    app.page.evaluate(
        """() => {
          const t = document.querySelector('.cf-chat-block .cf-chat-title');
          t.textContent = 'Un titulo bastante largo para empujar el contenido de la fila hacia los costados del chat';
        }"""
    )
    long = app.page.evaluate("document.querySelector('.cf-chat-block').getBoundingClientRect().width")

    assert abs(first - short) <= 1 and abs(first - long) <= 1, (first, short, long)
