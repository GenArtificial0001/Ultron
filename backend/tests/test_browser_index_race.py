"""mypy (BL-003, linea base) senalo en `serve_browser_index` un `content, content_type = result` con `result` posiblemente None.

Es real aunque improbable: `find_index_file` ve el archivo y, antes de que `read_file` lo lea, alguien lo borra (el agente reescribe el dashboard
justo cuando el iframe pide la raiz). `read_file` devuelve None y el desempaquetado lanzaba TypeError => 500 con un traceback. Ahora responde lo
mismo que cuando todavia no hay ningun archivo: la pagina de espera, sin error.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import browser_workspace
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_the_index_vanishing_between_lookup_and_read_gives_the_waiting_page_not_a_500(client, monkeypatch):
    monkeypatch.setattr(browser_workspace, "find_index_file", lambda sid: "index.html")
    monkeypatch.setattr(browser_workspace, "read_file", lambda sid, name: None)  # el archivo desaparecio justo en medio

    r = client.get("/browser/sesion-x/")

    assert r.status_code == 200
    assert "Aún no hay ningún archivo HTML" in r.text
    assert r.headers["content-type"].startswith("text/html")  # el iframe tiene que renderizarla como pagina, no como texto


def test_with_no_index_the_waiting_page_is_still_served(client, monkeypatch):
    monkeypatch.setattr(browser_workspace, "find_index_file", lambda sid: None)

    r = client.get("/browser/sesion-x/")

    assert r.status_code == 200 and "Aún no hay ningún archivo HTML" in r.text


def test_an_existing_index_is_served_with_its_content_type(client, monkeypatch):
    monkeypatch.setattr(browser_workspace, "find_index_file", lambda sid: "index.html")
    monkeypatch.setattr(browser_workspace, "read_file", lambda sid, name: (b"<html>hola</html>", "text/html; charset=utf-8"))

    r = client.get("/browser/sesion-x/")

    assert r.status_code == 200 and r.text == "<html>hola</html>"
    assert r.headers["content-type"].startswith("text/html")
