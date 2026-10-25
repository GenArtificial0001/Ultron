"""Conector de Confluence (spec conexiones-con-nombre Fase B, REQ-006/008/009/010). Sin red: `requests.request` se reemplaza por un servidor falso.

Lo que mas importa verificar, en este orden:
  1. REQ-009 SOLO LECTURA: ningun verbo distinto de GET llega a la red.
  2. ANTI-SSRF: ni una URL de otro host, ni un redirect a otro host, ni un host que solo EMPIEZA igual, reciben el token. El prototipo seguia redirects solo con
     `requests` y le habria mandado el token al host del redirect.
  3. El token y el correo (que salen del .env del usuario) no aparecen en ningun mensaje de error.
  4. Arbol con tope de profundidad y de nodos, secciones validadas, adjuntos con tope de tamano."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import connections as conns
from app.config import Settings
from app.connectors import confluence_connector as cc

TOKEN = "TOKEN-PERSONAL-8c2f1d"
EMAIL = "persona.prueba@empresa.com"
HOST = "empresa.atlassian.net"
BASE = f"https://{HOST}/wiki"


def settings(**kw) -> Settings:
    base = {"confluence_email": EMAIL, "confluence_api_token": TOKEN, "confluence_base_url": BASE, "confluence_space_key": "QOS"}
    base.update(kw)
    return Settings(_env_file=None, **base)


class Resp:
    def __init__(self, status=200, payload=None, headers=None, content=b"", text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]

    def close(self):
        pass


class FakeNet:
    """Reemplaza `requests.request`: anota cada peticion y contesta segun una funcion."""

    def __init__(self, handler):
        self.calls: list[dict] = []
        self.handler = handler

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cc._PAGE_ID_CACHE.clear()
    monkeypatch.setattr(conns, "get_settings", lambda: Settings(_env_file=None))
    yield
    cc._PAGE_ID_CACHE.clear()


def connector(monkeypatch, handler, **kw):
    net = FakeNet(handler)
    monkeypatch.setattr(cc.requests, "request", net)
    return cc.ConfluenceConnector(settings(**kw)), net


# ---------------------------------------------------------------------------------------------------------------------
# 1. REQ-009 solo lectura
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("verb", ["POST", "PUT", "DELETE", "PATCH", "post", "HEAD", "OPTIONS"])
def test_only_get_ever_reaches_the_network(monkeypatch, verb):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {}))

    with pytest.raises(cc.ConfluenceConnectionError, match="solo lectura"):
        c._request(verb, "/rest/api/space")

    assert net.calls == [], "un verbo de escritura no puede salir a la red"


def test_the_connector_has_no_method_that_writes(monkeypatch):
    public = [n for n in dir(cc.ConfluenceConnector) if not n.startswith("_")]
    writers = [n for n in public if any(w in n for w in ("create", "update", "delete", "post", "put", "write", "remove", "save"))]
    assert writers == []


def test_every_call_a_public_method_makes_is_a_get(monkeypatch):
    def handler(m, u, k):
        if "/children" in u:
            return Resp(200, {"results": [{"id": "2", "title": "Hija"}]})
        if "/child/attachment" in u and "download" not in u:
            return Resp(200, {"results": []})
        if "/content/search" in u:
            return Resp(200, {"results": []})
        if "/space" in u and "/content" not in u:
            return Resp(200, {"results": []})
        return Resp(200, {"id": "1", "title": "Raiz", "body": {"view": {"value": "<p>x</p>"}}, "space": {"key": "QOS"}, "version": {"number": 3}})

    c, net = connector(monkeypatch, handler)
    c.test_connection()
    c.list_spaces()
    c.search("algo")
    c.get_page("1")
    c.get_page_tree("1", max_depth=1)
    c.get_page_with_descendants("1", max_depth=1)
    c.list_attachments("1")

    assert net.calls and {call["method"] for call in net.calls} == {"GET"}


# ---------------------------------------------------------------------------------------------------------------------
# 2. Anti-SSRF
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/wiki/pages/123",
        f"https://{HOST}.evil.com/wiki/pages/123",
        f"https://evil-{HOST}/wiki/pages/123",
        f"https://{HOST}@evil.com/wiki/pages/123",
        f"https://user:pass@{HOST}/wiki/pages/123",
        "ftp://empresa.atlassian.net/wiki/pages/123",
        "file:///etc/passwd",
    ],
)
def test_a_url_of_another_host_is_refused_and_never_gets_the_token(monkeypatch, url):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {}))

    with pytest.raises(cc.ConfluenceConnectionError):
        c.get_page(url)

    assert net.calls == []


def test_a_redirect_to_another_host_is_cut_before_sending_the_token(monkeypatch):
    def handler(m, u, k):
        if u.startswith(f"https://{HOST}"):
            return Resp(302, headers={"Location": "https://evil.example.com/steal"})
        return Resp(200, {})

    c, net = connector(monkeypatch, handler)

    with pytest.raises(cc.ConfluenceConnectionError, match="no permitida"):
        c._request("GET", "/rest/api/space")

    hosts = [cc.urlparse(call["url"]).hostname for call in net.calls]
    assert hosts == [HOST], "el token solo pudo ir al host configurado; el salto a otro host se corto antes"


def test_a_redirect_chain_inside_the_same_host_is_followed_and_each_hop_is_checked(monkeypatch):
    def handler(m, u, k):
        if u.endswith("/rest/api/space"):
            return Resp(302, headers={"Location": "/wiki/rest/api/space?moved=1"})
        return Resp(200, {"results": []})

    c, net = connector(monkeypatch, handler)

    c._request("GET", "/rest/api/space")

    assert [call["url"] for call in net.calls] == [f"{BASE}/rest/api/space", f"https://{HOST}/wiki/rest/api/space?moved=1"]


def test_redirects_are_never_followed_by_requests_itself(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {}))

    c._request("GET", "/rest/api/space", allow_redirects=True)

    assert all(call["allow_redirects"] is False for call in net.calls), "requests no puede seguir redirects por su cuenta"


def test_an_endless_redirect_loop_gives_up(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(302, headers={"Location": f"{BASE}/rest/api/loop"}))

    with pytest.raises(cc.ConfluenceConnectionError, match="redirects"):
        c._request("GET", "/rest/api/space")

    assert len(net.calls) <= cc.MAX_REDIRECTS + 1


def test_an_attachment_url_of_another_host_is_refused(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {}))

    with pytest.raises(cc.ConfluenceConnectionError):
        c.get_attachment_by_url("https://evil.example.com/wiki/rest/api/content/1/child/attachment/att1/download")

    assert net.calls == []


def test_a_short_link_resolves_following_redirects_inside_the_host_only(monkeypatch):
    def handler(m, u, k):
        if u.endswith("/wiki/x/AbCd"):
            return Resp(302, headers={"Location": f"https://{HOST}/wiki/spaces/QOS/pages/777/Titulo"})
        return Resp(200, {})

    c, net = connector(monkeypatch, handler)

    assert c.resolve_page_id(f"https://{HOST}/wiki/x/AbCd") == "777"


def test_a_short_link_that_redirects_to_another_host_is_refused(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(302, headers={"Location": "https://evil.example.com/pages/1"}))

    with pytest.raises(cc.ConfluenceConnectionError):
        c.resolve_page_id(f"https://{HOST}/wiki/x/AbCd")

    assert [cc.urlparse(call["url"]).hostname for call in net.calls] == [HOST]


def test_the_page_id_cache_is_per_host(monkeypatch):
    """Una resolucion hecha contra un sitio no puede servir a otro sitio: la clave del cache lleva el host."""
    answers = {"uno.atlassian.net": "111", "dos.atlassian.net": "222"}

    def handler(m, u, k):
        return Resp(200, {"results": [{"id": answers[cc.urlparse(u).hostname]}]})

    net = FakeNet(handler)
    monkeypatch.setattr(cc.requests, "request", net)
    c1 = cc.ConfluenceConnector(settings(confluence_base_url="https://uno.atlassian.net/wiki"))
    c2 = cc.ConfluenceConnector(settings(confluence_base_url="https://dos.atlassian.net/wiki"))

    assert c1.resolve_page_id("Mi pagina") == "111"
    assert c2.resolve_page_id("Mi pagina") == "222"
    assert c1.resolve_page_id("Mi pagina") == "111", "y la segunda vez de c1 sale del cache sin tocar a c2"


def test_the_page_id_cache_is_bounded(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, {}))
    for i in range(cc._PAGE_ID_CACHE_MAX + 50):
        c.resolve_page_id(str(i))

    assert len(cc._PAGE_ID_CACHE) <= cc._PAGE_ID_CACHE_MAX


# ---------------------------------------------------------------------------------------------------------------------
# 3. El token no se filtra
# ---------------------------------------------------------------------------------------------------------------------
def test_missing_credentials_say_which_variables_to_set_without_echoing_anything(monkeypatch):
    net = FakeNet(lambda m, u, k: Resp(200, {}))
    monkeypatch.setattr(cc.requests, "request", net)
    c = cc.ConfluenceConnector(settings(confluence_email="", confluence_api_token=""))

    with pytest.raises(cc.ConfluenceConnectionError) as err:
        c.list_spaces()

    assert "CONFLUENCE_EMAIL" in str(err.value) and "CONFLUENCE_API_TOKEN" in str(err.value)
    assert net.calls == [], "sin credenciales no se sale a la red"


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502])
def test_http_errors_become_readable_messages_that_never_contain_the_token_or_the_body(monkeypatch, status):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(status, text=f"cuerpo con {TOKEN} y {EMAIL}"))

    with pytest.raises(cc.ConfluenceConnectionError) as err:
        c.list_spaces()

    assert TOKEN not in str(err.value) and EMAIL not in str(err.value) and "cuerpo con" not in str(err.value)


def test_a_non_json_answer_is_a_clean_error(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, None))

    with pytest.raises(cc.ConfluenceConnectionError, match="no es JSON"):
        c.list_spaces()


def test_the_token_is_sent_only_through_the_auth_tuple_never_in_the_url(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {"results": []}))

    c.list_spaces()

    assert net.calls[0]["auth"] == (EMAIL, TOKEN)
    assert all(TOKEN not in call["url"] and TOKEN not in json.dumps(call.get("params") or {}) for call in net.calls)


def test_test_connection_never_raises_and_does_not_log_the_message(monkeypatch, caplog):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(401, text=f"mal {TOKEN}"))

    with caplog.at_level("DEBUG"):
        assert c.test_connection() is False

    assert TOKEN not in caplog.text and EMAIL not in caplog.text


def test_test_connection_does_not_log_the_text_of_a_network_exception_that_carries_the_token(monkeypatch, caplog):
    """Un fallo de red de `requests` puede traer en su mensaje la URL, el proxy o las credenciales. Un `logger.warning("%s", exc)` las dejaria en el log.
    El tipo de la excepcion alcanza para diagnosticar; su texto no se registra."""

    def boom(method, url, **kwargs):
        raise RuntimeError(f"fallo conectando con {EMAIL}:{TOKEN}@{HOST}")

    monkeypatch.setattr(cc.requests, "request", boom)
    c = cc.ConfluenceConnector(settings())

    with caplog.at_level("DEBUG"):
        assert c.test_connection() is False

    assert TOKEN not in caplog.text and EMAIL not in caplog.text and "RuntimeError" in caplog.text


def test_test_connection_true_when_the_user_endpoint_answers(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, {"email": EMAIL}))

    assert c.test_connection() is True


# ---------------------------------------------------------------------------------------------------------------------
# 4. Funcionalidad: busqueda, paginas, arbol, adjuntos
# ---------------------------------------------------------------------------------------------------------------------
def test_search_escapes_quotes_and_backslashes_so_the_query_cannot_break_out_of_the_cql(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {"results": []}))

    c.search('uno" OR space = "SECRETO', space_key='X" OR 1=1 OR space = "Y')

    cql = net.calls[0]["params"]["cql"]
    assert 'text ~ "uno' + chr(92) + '" OR space = ' + chr(92) + '"SECRETO"' in cql
    assert 'space = "X' + chr(92) + '" OR 1=1 OR space = ' + chr(92) + '"Y"' in cql


def test_an_empty_search_does_not_hit_the_network(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {"results": []}))

    assert c.search("   ") == [] and net.calls == []


def test_search_results_build_their_url_from_the_configured_base_not_from_the_response(monkeypatch):
    payload = {"results": [{"id": 5, "title": "T", "space": {"key": "QOS"}, "excerpt": "<b>hola</b> mundo", "_links": {"webui": "/spaces/QOS/pages/5", "base": "https://evil.com"}}]}
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, payload))

    out = c.search_cql("type = page")

    assert out[0]["url"] == f"{BASE}/spaces/QOS/pages/5" and out[0]["excerpt"] == "hola mundo" and out[0]["space_key"] == "QOS"


def test_get_page_returns_html_and_plain_text_without_css(monkeypatch):
    html = "<style>.x{color:red}</style><h1>Titulo</h1><p>Cuerpo&nbsp;con <b>negrita</b></p>"
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, {"id": "9", "title": "P", "body": {"view": {"value": html}}, "space": {"key": "QOS"}, "version": {"number": 2}}))

    page = c.get_page("9")

    assert page["text"] == "Titulo Cuerpo con negrita" and "color" not in page["text"]
    assert page["version"] == 2 and page["url"] == f"{BASE}/spaces/QOS/pages/9"


def _tree_handler(children):
    def handler(m, u, k):
        if "/children" in u:
            pid = u.split("/pages/")[1].split("/")[0]
            return Resp(200, {"results": [{"id": cid, "title": f"P{cid}"} for cid in children.get(pid, [])]})
        pid = u.rsplit("/", 1)[1]
        return Resp(200, {"id": pid, "title": f"P{pid}", "body": {"view": {"value": ""}}, "space": {"key": "QOS"}})

    return handler


def test_the_tree_goes_down_only_max_depth_levels(monkeypatch):
    c, _ = connector(monkeypatch, _tree_handler({"1": ["2", "3"], "2": ["4"], "3": [], "4": ["5"]}))

    tree = c.get_page_tree("1", max_depth=1)

    assert [ch["id"] for ch in tree["children"]] == ["2", "3"] and all(ch["children"] == [] for ch in tree["children"])


def test_depth_zero_is_just_the_root_and_makes_no_children_requests(monkeypatch):
    """El prototipo tenia el bug `max_depth or 2`: un 0 explicito se volvia 2 y disparaba cientos de requests."""
    c, net = connector(monkeypatch, _tree_handler({"1": ["2", "3"]}))

    tree = c.get_page_tree("1", max_depth=0)

    assert tree["children"] == [] and not any("/children" in call["url"] for call in net.calls)


def test_the_tree_is_cut_at_max_nodes_and_says_so(monkeypatch):
    c, _ = connector(monkeypatch, _tree_handler({"1": [str(i) for i in range(2, 60)]}))

    tree = c.get_page_tree("1", max_depth=1, max_nodes=10)

    assert tree["truncated"] is True and 1 + len(tree["children"]) == 10


def test_depth_and_node_limits_are_clamped_against_absurd_values(monkeypatch):
    c, _ = connector(monkeypatch, _tree_handler({"1": ["2"], "2": ["3"], "3": ["4"], "4": ["5"], "5": ["6"], "6": ["7"], "7": ["8"]}))

    tree = c.get_page_tree("1", max_depth=999, max_nodes=10**9)

    depth, node = 0, tree
    while node["children"]:
        node, depth = node["children"][0], depth + 1
    assert depth == 5, "la profundidad maxima es 5 aunque se pida 999"


def test_descendants_are_capped_and_joined_as_text(monkeypatch):
    kids = {"1": [str(i) for i in range(2, 80)]}
    c, _ = connector(monkeypatch, _tree_handler(kids))

    doc = c.get_page_with_descendants("1", max_depth=1, max_pages=5)

    assert len(doc["descendants"]) == 5 and "## P2" in doc["text_full"]


def test_pagination_stops_on_a_next_link_that_never_ends(monkeypatch):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, {"results": [{"key": "A", "name": "a"}], "_links": {"next": "/wiki/rest/api/space?start=1"}}))

    out = c.list_spaces(limit=10**6)

    assert len(net.calls) <= 200 and len(out) == len(net.calls)


def test_list_spaces_returns_key_name_and_type(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, {"results": [{"key": "QOS", "name": "Calidad", "type": "global"}]}))

    assert c.list_spaces() == [{"key": "QOS", "name": "Calidad", "type": "global"}]


def test_attachments_are_listed_with_urls_built_from_the_configured_base(monkeypatch):
    payload = {"results": [{"id": "att1", "title": "f.pdf", "metadata": {"mediaType": "application/pdf"}, "extensions": {"fileSize": 99}, "version": {"number": 1}, "_links": {"download": "/download/attachments/9/f.pdf", "base": "https://evil.com"}}]}
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, payload))

    att = c.list_attachments("9")

    assert att[0]["download_url"] == f"{BASE}/download/attachments/9/f.pdf" and att[0]["size"] == 99


def test_an_attachment_is_downloaded_with_its_name_from_the_header(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, headers={"Content-Disposition": 'attachment; filename="informe final.pdf"', "Content-Type": "application/pdf"}, content=b"%PDF-1"))

    data, name, media = c.get_attachment("9", "att1")

    assert data == b"%PDF-1" and name == "informe final.pdf" and media == "application/pdf"


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "att1/../../x", "att1;rm", "", "att"])
def test_a_malformed_attachment_id_is_refused_before_any_request(monkeypatch, bad_id):
    c, net = connector(monkeypatch, lambda m, u, k: Resp(200, content=b"x"))

    with pytest.raises(cc.ConfluenceConnectionError, match="invalido"):
        c.get_attachment("9", bad_id)

    assert net.calls == []


def test_an_attachment_over_the_size_limit_is_refused_by_its_declared_size(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, headers={"Content-Length": str(cc.MAX_ATTACHMENT_BYTES + 1)}, content=b"x"))

    with pytest.raises(cc.ConfluenceConnectionError, match="pesa mas de"):
        c.get_attachment("9", "att1")


def test_an_attachment_that_lies_about_its_size_is_cut_while_reading(monkeypatch):
    monkeypatch.setattr(cc, "MAX_ATTACHMENT_BYTES", 100)
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, content=b"x" * 500))

    with pytest.raises(cc.ConfluenceConnectionError, match="pesa mas de"):
        c.get_attachment("9", "att1")


def test_an_empty_attachment_is_an_error(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, content=b""))

    with pytest.raises(cc.ConfluenceConnectionError, match="vacio"):
        c.get_attachment("9", "att1")


def test_an_attachment_by_webui_url_finds_it_by_name(monkeypatch):
    def handler(m, u, k):
        if u.endswith("/child/attachment") or "/child/attachment?" in u:
            return Resp(200, {"results": [{"id": "att7", "title": "datos.xlsx", "_links": {"download": "/download/attachments/9/datos.xlsx"}}]})
        return Resp(200, headers={"Content-Disposition": 'attachment; filename="datos.xlsx"'}, content=b"PK")

    c, _ = connector(monkeypatch, handler)

    data, name, _ = c.get_attachment_by_url(f"{BASE}/download/attachments/9/datos.xlsx")

    assert data == b"PK" and name == "datos.xlsx"


def test_an_unrecognized_attachment_url_is_refused(monkeypatch):
    c, _ = connector(monkeypatch, lambda m, u, k: Resp(200, {}))

    with pytest.raises(cc.ConfluenceConnectionError, match="no reconocida"):
        c.get_attachment_by_url(f"{BASE}/spaces/QOS/pages/9")


# ---------------------------------------------------------------------------------------------------------------------
# HTTPS obligatorio y secciones
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://empresa.atlassian.net/wiki", "ftp://empresa.atlassian.net/wiki", "empresa.atlassian.net/wiki", "https://user:pw@empresa.atlassian.net/wiki"])
def test_a_base_url_that_is_not_plain_https_is_discarded_with_a_message(url):
    fields, errors = cc.normalize_confluence_fields({"confluence_base_url": url})

    assert fields["confluence_base_url"] == "" and errors and "pw" not in " ".join(errors)


def test_http_is_accepted_only_for_localhost():
    ok, errors = cc.normalize_confluence_fields({"confluence_base_url": "http://localhost:8080/wiki"})

    assert ok["confluence_base_url"] == "http://localhost:8080/wiki" and errors == []


def test_the_default_base_url_is_used_when_the_block_has_none(monkeypatch):
    c = cc.ConfluenceConnector(Settings(_env_file=None, confluence_email=EMAIL, confluence_api_token=TOKEN))

    assert c._base == cc.DEFAULT_BASE_URL


def test_valid_sections_are_kept_and_normalized():
    raw = json.dumps([{"name": "MP&L Procedures", "page_id": "611718870"}, {"name": "Calidad", "page_id": " 42 ", "space": "QOS"}])

    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert errors == [] and cc.parse_sections(fields["sections"]) == [
        {"name": "MP&L Procedures", "page_id": "611718870", "space": ""},
        {"name": "Calidad", "page_id": "42", "space": "QOS"},
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "", "page_id": "1"},
        {"name": "x" * 61, "page_id": "1"},
        {"name": "Con {llaves}", "page_id": "1"},
        {"name": 'Con "comillas"', "page_id": "1"},
        {"name": "Con <b>", "page_id": "1"},
        {"name": "Con " + chr(92) + " barra", "page_id": "1"},
        {"name": "Linea\nnueva", "page_id": "1"},
        {"name": "Ok", "page_id": "no-numerico"},
        {"name": "Ok", "page_id": ""},
        {"name": "Ok"},
        "no es un bloque",
    ],
)
def test_an_invalid_section_is_dropped_with_a_message_and_the_good_ones_survive(bad):
    raw = json.dumps([{"name": "Buena", "page_id": "10"}, bad])

    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert [s["name"] for s in cc.parse_sections(fields["sections"])] == ["Buena"] and errors


def test_duplicate_section_names_are_dropped_case_insensitively():
    raw = json.dumps([{"name": "Calidad", "page_id": "1"}, {"name": "CALIDAD", "page_id": "2"}])

    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert [s["page_id"] for s in cc.parse_sections(fields["sections"])] == ["1"] and any("repetido" in e for e in errors)


def test_more_than_the_section_limit_are_cut():
    raw = json.dumps([{"name": f"S{i}", "page_id": str(i)} for i in range(cc.MAX_SECTIONS + 5)])

    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert len(cc.parse_sections(fields["sections"])) == cc.MAX_SECTIONS and any("tope" in e for e in errors)


@pytest.mark.parametrize("raw", ["{no es lista", '{"a": 1}', "12"])
def test_a_broken_sections_value_is_ignored_with_a_message(raw):
    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert cc.parse_sections(fields["sections"]) == [] and errors


def test_an_invalid_space_in_a_section_is_dropped_but_the_section_stays():
    raw = json.dumps([{"name": "Calidad", "page_id": "1", "space": 'X" OR 1=1'}])

    fields, errors = cc.normalize_confluence_fields({"sections": raw})

    assert cc.parse_sections(fields["sections"]) == [{"name": "Calidad", "page_id": "1", "space": ""}] and errors


def test_parse_sections_tolerates_garbage():
    assert cc.parse_sections("") == [] and cc.parse_sections("no json") == [] and cc.parse_sections('"texto"') == []
    assert cc.parse_sections(json.dumps([1, {"name": "A", "page_id": "2"}])) == [{"name": "A", "page_id": "2", "space": ""}]


def test_public_info_has_sections_and_space_and_never_credentials(monkeypatch):
    monkeypatch.setattr(conns, "get_settings", lambda: settings())
    conn = conns.Connection(
        "confluence", "la empresa", True, True,
        {"confluence_space_key": "QOS", "sections": json.dumps([{"name": "MP&L Procedures", "page_id": "611718870", "space": ""}])},
    )

    info = cc.confluence_public_info(conn)

    assert info["space"] == "QOS" and info["sections"][0]["name"] == "MP&L Procedures"
    assert TOKEN not in json.dumps(info) and EMAIL not in json.dumps(info)


def test_a_namespace_style_settings_object_still_works_for_a_registered_type_test():
    """El tester recibe el objeto de settings de la conexion: debe usar solo atributos de Settings (nada de metodos de pydantic)."""
    ns = SimpleNamespace(confluence_email=EMAIL, confluence_api_token=TOKEN, confluence_base_url=BASE, confluence_space_key="", confluence_timeout_seconds=5, confluence_https_proxy="")

    connector_ = cc.ConfluenceConnector(ns)  # type: ignore[arg-type]

    assert connector_._base == BASE and connector_._timeout == 5


# ---------------------------------------------------------------------------------------------------------------------
# Conexion armada desde el .env (sin connections.json) y campos completos del .env
# ---------------------------------------------------------------------------------------------------------------------
def test_without_a_file_the_env_root_page_becomes_a_section_named_after_the_env(monkeypatch):
    env = settings(confluence_root_page_id="611718870", confluence_root_page_name="MP&L Procedures")
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    conns.reset_for_tests()

    conn = conns.resolve("confluence")

    assert conn.name == "Principal" and cc.parse_sections(conn.fields["sections"]) == [{"name": "MP&L Procedures", "page_id": "611718870", "space": ""}]


def test_the_env_section_has_a_default_name_when_none_is_given(monkeypatch):
    env = settings(confluence_root_page_id="42")
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    conns.reset_for_tests()

    assert [s["name"] for s in cc.parse_sections(conns.resolve("confluence").fields["sections"])] == ["Pagina raiz"]


def test_without_a_root_page_in_the_env_there_are_no_sections(monkeypatch):
    monkeypatch.setattr(conns, "get_settings", lambda: settings())
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    conns.reset_for_tests()

    assert cc.parse_sections(conns.resolve("confluence").fields.get("sections", "")) == []


@pytest.mark.parametrize("bad", ["no-es-numero", "12; DROP", "../x", "1 2"])
def test_an_invalid_root_page_id_in_the_env_is_dropped_and_never_reaches_a_request(monkeypatch, bad):
    env = settings(confluence_root_page_id=bad, confluence_root_page_name="Raiz")
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    conns.reset_for_tests()

    assert cc.parse_sections(conns.resolve("confluence").fields.get("sections", "")) == []


def test_a_root_page_name_with_dangerous_characters_is_dropped_from_the_env_section(monkeypatch):
    env = settings(confluence_root_page_id="42", confluence_root_page_name='Mala "{skills_block}" <b>')
    monkeypatch.setattr(conns, "get_settings", lambda: env)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", Path("no-existe-connections.json"))
    conns.reset_for_tests()

    assert cc.parse_sections(conns.resolve("confluence").fields.get("sections", "")) == []


def test_the_env_example_lists_every_confluence_setting():
    """Cada campo de Settings de Confluence tiene su linea en .env.example: nadie tiene que adivinar que variable falta."""
    from app.config import Settings

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    declared = {f"{name.upper()}=" for name in Settings.model_fields if name.startswith("confluence_")} | {"ENABLE_CONFLUENCE="}

    missing = sorted(var for var in declared if var not in example)

    assert missing == [], f"faltan en .env.example: {missing}"


def test_the_env_example_never_ships_a_real_looking_secret():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith(("CONFLUENCE_API_TOKEN=", "CONFLUENCE_EMAIL=")):
            assert line.split("=", 1)[1] == "", f"el ejemplo no puede traer un valor: {line.split('=')[0]}"


def test_the_connections_template_ships_a_confluence_block_with_sections_and_no_token():
    template = json.loads((Path(__file__).resolve().parents[1] / "connections.example.json").read_text(encoding="utf-8"))
    blocks = [c for c in template["connections"] if c["type"] == "confluence"]

    assert blocks, "la plantilla tiene que incluir un bloque de Confluence"
    fields = blocks[0]["fields"]
    assert "sections" in fields and "confluence_base_url" in fields
    assert "confluence_api_token" not in fields and "confluence_email" not in fields, "el token personal NUNCA va en el archivo compartido"
    cleaned, errors = cc.normalize_confluence_fields({k: (v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()})
    assert errors == [] and cc.parse_sections(cleaned["sections"])
