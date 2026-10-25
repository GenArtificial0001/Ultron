"""API de Confluence para la pantalla (spec conexiones-con-nombre Fase B: REQ-006/010). Se prueba por HTTP contra la app real con un conector falso.

Lo importante: (1) las secciones salen en `/api/connections` pero NUNCA el token ni el correo; (2) las paginas viajan como TEXTO PLANO (nunca HTML), asi una
pagina del wiki con `<script>` no puede ejecutarse en la pantalla; (3) un error del conector se enmascara antes de llegar al navegador; (4) los ids tienen que
ser numericos."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import connections as conns
from app import main
from app.config import Settings

TOKEN = "TOKEN-API-77d1c0"
EMAIL = "persona.api@empresa.com"
SECTIONS = [{"name": "MP&L Procedures", "page_id": "611718870"}, {"name": "Calidad", "page_id": "42"}]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    base = Settings(_env_file=None, confluence_email=EMAIL, confluence_api_token=TOKEN)
    monkeypatch.setattr(conns, "CONNECTIONS_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(conns, "get_settings", lambda: base)
    monkeypatch.setattr(main, "get_settings", lambda: base)
    conns.reset_for_tests()
    yield
    conns.reset_for_tests()


def write(tmp_path, blocks):
    (tmp_path / "connections.json").write_text(json.dumps({"connections": blocks}), encoding="utf-8")


def block(name="la empresa", sections=None):
    return {
        "type": "confluence",
        "name": name,
        "fields": {
            "confluence_base_url": "https://empresa.atlassian.net/wiki",
            "confluence_space_key": "QOS",
            "sections": SECTIONS if sections is None else sections,
        },
    }


class Fake:
    built: list = []
    fail: Exception | None = None
    seen: list = []

    def __init__(self, settings):
        Fake.built.append(settings)

    def _fail(self):
        if Fake.fail is not None:
            raise Fake.fail

    def get_page_tree(self, ref, max_depth=0, max_nodes=300):
        self._fail()
        Fake.seen.append(("tree", ref, max_depth))
        return {"id": ref, "title": f"Raiz {ref}", "space_key": "QOS", "url": "u", "children": [], "truncated": False}

    def list_child_pages(self, page_id, limit=250):
        self._fail()
        Fake.seen.append(("children", page_id))
        return [{"id": "2", "title": "Hija"}]

    def get_page(self, ref):
        self._fail()
        Fake.seen.append(("page", ref))
        return {
            "id": "9",
            "title": "T",
            "space_key": "QOS",
            "url": "u",
            "html": "<script>alert(1)</script>",
            "text": "<b>texto</b> plano " + "x" * 70_000,
        }


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    Fake.built, Fake.seen, Fake.fail = [], [], None
    monkeypatch.setattr(main, "ConfluenceConnector", Fake)


@pytest.fixture
def client():
    return TestClient(main.app)


# ---------------------------------------------------------------------------------------------------------------------
# /api/connections: las secciones se ven, los secretos no
# ---------------------------------------------------------------------------------------------------------------------
def test_the_connections_list_shows_the_confluence_sections(client, tmp_path):
    write(tmp_path, [block()])

    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["type"] == "confluence")

    assert conn["display"] == "Confluence - la empresa" and [s["name"] for s in conn["info"]["sections"]] == [
        "MP&L Procedures",
        "Calidad",
    ]
    assert conn["info"]["space"] == "QOS" and conn["status"] == "configured"


def test_the_connections_list_never_carries_the_token_or_the_email(client, tmp_path):
    write(tmp_path, [block()])

    body = client.get("/api/connections").text

    assert TOKEN not in body and EMAIL not in body


def test_a_confluence_connection_without_the_personal_token_is_incomplete_and_names_the_variable(
    client, tmp_path, monkeypatch
):
    nobody = Settings(_env_file=None)
    monkeypatch.setattr(conns, "get_settings", lambda: nobody)
    write(tmp_path, [block()])

    conn = next(c for c in client.get("/api/connections").json()["connections"] if c["type"] == "confluence")

    assert conn["status"] == "incomplete" and set(conn["missing"]) == {"confluence_email", "confluence_api_token"}
    assert "valor" not in json.dumps(conn)


def test_a_token_written_in_the_shared_file_is_ignored_with_a_message_that_does_not_echo_it(client, tmp_path):
    bad = block()
    bad["fields"]["confluence_api_token"] = "TOKEN-EN-EL-ARCHIVO-COMPARTIDO"
    write(tmp_path, [bad])

    data = client.get("/api/connections").json()

    assert "TOKEN-EN-EL-ARCHIVO-COMPARTIDO" not in json.dumps(data)
    assert "confluence_api_token" in (data["error"] or "") and "personal" in data["error"]


# ---------------------------------------------------------------------------------------------------------------------
# /api/confluence/tree, children, page
# ---------------------------------------------------------------------------------------------------------------------
def test_the_tree_of_a_section_starts_at_its_root_page(client, tmp_path):
    write(tmp_path, [block()])

    resp = client.get("/api/confluence/tree", params={"connection": "la empresa", "section": "Calidad"})

    assert resp.status_code == 200 and ("tree", "42", 0) in Fake.seen


def test_the_section_is_case_insensitive_and_keeps_the_ampersand(client, tmp_path):
    write(tmp_path, [block()])

    client.get("/api/confluence/tree", params={"connection": "la empresa", "section": "mp&l procedures"})

    assert ("tree", "611718870", 0) in Fake.seen


def test_without_a_section_the_first_one_is_used(client, tmp_path):
    write(tmp_path, [block()])

    client.get("/api/confluence/tree", params={"connection": "la empresa"})

    assert ("tree", "611718870", 0) in Fake.seen


def test_an_unknown_section_is_a_404(client, tmp_path):
    write(tmp_path, [block()])

    assert client.get("/api/confluence/tree", params={"connection": "la empresa", "section": "Nada"}).status_code == 404


def test_an_unknown_connection_is_a_404_that_lists_the_valid_names(client, tmp_path):
    write(tmp_path, [block("la empresa"), block("Otra")])

    resp = client.get("/api/confluence/tree", params={"connection": "Nada"})

    assert (
        resp.status_code == 404
        and "'la empresa'" in resp.json()["detail"]
        and "'Otra'" in resp.json()["detail"]
        and Fake.built == []
    )


def test_the_depth_is_clamped(client, tmp_path):
    write(tmp_path, [block()])

    client.get("/api/confluence/tree", params={"connection": "la empresa", "depth": 999})
    client.get("/api/confluence/tree", params={"connection": "la empresa", "depth": -5})

    assert [d for k, _, d in Fake.seen if k == "tree"] == [3, 0]


def test_children_are_requested_by_numeric_id(client, tmp_path):
    write(tmp_path, [block()])

    resp = client.get("/api/confluence/children", params={"id": "611718870", "connection": "la empresa"})

    assert resp.json() == {"children": [{"id": "2", "title": "Hija"}]}


@pytest.mark.parametrize("bad", ["abc", "1; DROP", "../../x", "12 34", "http://evil.com", ""])
def test_a_non_numeric_page_id_is_refused_before_any_connection(client, tmp_path, bad):
    write(tmp_path, [block()])

    resp = client.get("/api/confluence/children", params={"id": bad, "connection": "la empresa"})

    assert resp.status_code in (400, 422) and Fake.built == []


def test_a_page_goes_to_the_screen_as_plain_text_never_as_html(client, tmp_path):
    write(tmp_path, [block()])

    data = client.get("/api/confluence/page", params={"ref": "9", "connection": "la empresa"}).json()

    assert "html" not in data and "<script>" not in json.dumps(data)
    assert data["text"].startswith("<b>texto</b> plano"), (
        "el texto llega tal cual y la pantalla lo pinta con textContent"
    )
    assert len(data["text"]) == 60_000


def test_the_page_endpoint_has_no_html_field_at_all(client, tmp_path):
    write(tmp_path, [block()])

    assert set(client.get("/api/confluence/page", params={"ref": "9", "connection": "la empresa"}).json()) == {
        "id",
        "title",
        "url",
        "space",
        "text",
    }


@pytest.mark.parametrize(
    "path,params",
    [("/api/confluence/tree", {}), ("/api/confluence/children", {"id": "5"}), ("/api/confluence/page", {"ref": "9"})],
)
def test_a_connector_error_is_masked_before_it_reaches_the_browser(client, tmp_path, path, params):
    write(tmp_path, [block()])
    Fake.fail = RuntimeError(f"401 para {EMAIL} con el token {TOKEN} contra empresa.atlassian.net")

    resp = client.get(path, params={**params, "connection": "la empresa"})

    assert resp.status_code == 502 and TOKEN not in resp.text and EMAIL not in resp.text


def test_the_error_log_does_not_carry_the_text_of_the_exception(client, tmp_path, caplog):
    write(tmp_path, [block()])
    Fake.fail = RuntimeError(f"fallo con {TOKEN}")

    with caplog.at_level("DEBUG"):
        client.get("/api/confluence/page", params={"ref": "9", "connection": "la empresa"})

    assert TOKEN not in caplog.text and "RuntimeError" in caplog.text


def test_with_confluence_switched_off_the_endpoints_say_so(client, tmp_path, monkeypatch):
    write(tmp_path, [block()])
    off = Settings(_env_file=None, enable_confluence=False)
    monkeypatch.setattr(main, "get_settings", lambda: off)

    resp = client.get("/api/confluence/tree", params={"connection": "la empresa"})

    assert resp.status_code == 400 and Fake.built == []


def test_the_named_connection_uses_its_own_url_but_the_token_of_the_env(client, tmp_path):
    other = block("Otra")
    other["fields"]["confluence_base_url"] = "https://otra.atlassian.net/wiki"
    write(tmp_path, [block("la empresa"), other])

    client.get("/api/confluence/page", params={"ref": "9", "connection": "Otra"})

    assert (
        Fake.built[-1].confluence_base_url == "https://otra.atlassian.net/wiki"
        and Fake.built[-1].confluence_api_token == TOKEN
    )


def test_the_confluence_connection_can_be_tested_by_name(client, tmp_path, monkeypatch):
    import app.connectors.confluence_connector as cc

    seen = []

    class FakeTest:
        def __init__(self, settings):
            seen.append(settings.confluence_base_url)

        def test_connection(self):
            return True

    monkeypatch.setattr(cc, "ConfluenceConnector", FakeTest)
    write(tmp_path, [block("la empresa")])

    resp = client.post("/api/connections/confluence/test", params={"name": "la empresa"})

    assert resp.status_code == 200 and resp.json()["ok"] is True and seen == ["https://empresa.atlassian.net/wiki"]
