"""Unit de `webfetch`: `requests.get` se reemplaza por un fake (nunca red).
Se prueba normalizacion de URL, errores, formatos, truncados y limite de salida."""

from __future__ import annotations

import io

import pytest
import requests

from app.tools import webfetch as wf


class FakeResp:
    def __init__(self, body: bytes = b"<h1>Hola</h1><p>mundo</p>", status=200, encoding="utf-8"):
        self.status_code = status
        self.encoding = encoding
        self.raw = io.BytesIO(body)

        def read(n, decode_content=True):
            return io.BytesIO(body).read(n)

        self.raw.read = read  # imita urllib3: read(amt, decode_content=)


@pytest.fixture
def get(monkeypatch):
    calls: list[tuple] = []
    state = {"resp": FakeResp(), "exc": None}

    def fake_get(url, **kw):
        calls.append((url, kw))
        if state["exc"]:
            raise state["exc"]
        return state["resp"]

    monkeypatch.setattr(wf.requests, "get", fake_get)
    state["calls"] = calls
    return state


def test_missing_url():
    assert wf._webfetch_execute({}).startswith("Error: falta")


def test_scheme_is_added_when_missing(get):
    wf._webfetch_execute({"url": "ejemplo.com/x"})
    assert get["calls"][0][0] == "https://ejemplo.com/x"
    wf._webfetch_execute({"url": "http://plano.com"})
    assert get["calls"][1][0] == "http://plano.com"


def test_request_uses_timeout_stream_and_user_agent(get):
    wf._webfetch_execute({"url": "a.com"})
    kw = get["calls"][0][1]
    assert kw["timeout"] == wf.TIMEOUT and kw["stream"] is True and "ULTRON" in kw["headers"]["User-Agent"]


def test_network_error_is_returned_as_text(get):
    get["exc"] = requests.ConnectionError("dns")
    assert wf._webfetch_execute({"url": "a.com"}) == "Error obteniendo la URL: dns"


@pytest.mark.parametrize("status", [400, 404, 500])
def test_http_errors(get, status):
    get["resp"] = FakeResp(status=status)
    assert f"status {status}" in wf._webfetch_execute({"url": "a.com"})


def test_default_format_is_markdown(get):
    out = wf._webfetch_execute({"url": "a.com"})
    assert "Hola" in out and "mundo" in out and "<h1>" not in out  # markdownify: h1 -> subrayado setext


@pytest.mark.parametrize("fmt", ["text", "html"])
def test_raw_formats_keep_html(get, fmt):
    assert "<h1>Hola</h1>" in wf._webfetch_execute({"url": "a.com", "format": fmt})


def test_markdownify_failure_falls_back_to_raw_text(get, monkeypatch):
    def boom(text):
        raise ValueError("html roto")

    monkeypatch.setattr(wf, "markdownify", boom)
    assert wf._webfetch_execute({"url": "a.com"}) == "<h1>Hola</h1><p>mundo</p>"


def test_body_over_limit_is_truncated_with_notice(get, monkeypatch):
    monkeypatch.setattr(wf, "MAX_BYTES", 10)
    get["resp"] = FakeResp(b"x" * 50)
    out = wf._webfetch_execute({"url": "a.com", "format": "text"})
    assert out.startswith("x" * 10) and "truncado" in out and "x" * 11 not in out.split("\n")[0]


def test_output_is_capped_at_20000_chars(get):
    get["resp"] = FakeResp(b"y" * 100_000)
    assert len(wf._webfetch_execute({"url": "a.com", "format": "text"})) == 20000


def test_undeclared_encoding_defaults_to_utf8_and_bad_bytes_are_replaced(get):
    get["resp"] = FakeResp("canción".encode(), encoding=None)
    assert "canción" in wf._webfetch_execute({"url": "a.com", "format": "text"})
    get["resp"] = FakeResp(b"ok \xff fin", encoding="utf-8")
    assert "fin" in wf._webfetch_execute({"url": "a.com", "format": "text"})
