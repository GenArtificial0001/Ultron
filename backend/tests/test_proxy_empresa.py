"""Proxy de la empresa para todo el proceso (spec vision-generico REQ-004, portado
de Vision-personal junto con la voz de Fish Audio/ElevenLabs).

Sin proxy, una PC detras de una VPN con PAC no resuelve `api.fish.audio` ni
`api.elevenlabs.io` ("getaddrinfo failed") y el Modo Cerebro queda sin dictado ni
voz. Se prueba `_apply_proxy` sin tocar el entorno real: todo con `monkeypatch`."""

from __future__ import annotations

import os
from types import SimpleNamespace

from app import config

VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _clean(monkeypatch):
    for var in VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_proxy_applies_to_the_whole_process_and_localhost_stays_direct(monkeypatch):
    _clean(monkeypatch)
    settings = SimpleNamespace(
        http_proxy="",
        https_proxy="http://proxy.empresa.test:8080",
        no_proxy="intranet.local",
    )

    config._apply_proxy(settings)

    assert os.environ["HTTPS_PROXY"] == "http://proxy.empresa.test:8080"
    assert os.environ["NO_PROXY"].split(",")[0] == "intranet.local"
    assert (
        "127.0.0.1" in os.environ["NO_PROXY"] and "localhost" in os.environ["NO_PROXY"]
    )
    monkeypatch.undo()


def test_proxy_already_defined_in_windows_is_not_overwritten(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.windows.test:3128")
    monkeypatch.setenv("NO_PROXY", "ya-definido")

    config._apply_proxy(
        SimpleNamespace(http_proxy="", https_proxy="http://otro.test:1", no_proxy="")
    )

    assert os.environ["HTTPS_PROXY"] == "http://proxy.windows.test:3128"
    assert os.environ["NO_PROXY"] == "ya-definido"


def test_without_proxy_nothing_is_touched(monkeypatch):
    _clean(monkeypatch)

    config._apply_proxy(SimpleNamespace(http_proxy="", https_proxy="", no_proxy=""))

    assert not any(os.environ.get(v) for v in VARS)


def test_blank_spaces_do_not_count_as_a_proxy(monkeypatch):
    _clean(monkeypatch)
    config._apply_proxy(
        SimpleNamespace(http_proxy="   ", https_proxy="  ", no_proxy="")
    )
    assert not any(os.environ.get(v) for v in VARS)


def test_http_and_https_proxies_are_both_applied(monkeypatch):
    _clean(monkeypatch)
    config._apply_proxy(
        SimpleNamespace(
            http_proxy="http://a.test:1", https_proxy="http://b.test:2", no_proxy=""
        )
    )
    assert os.environ["HTTP_PROXY"] == "http://a.test:1"
    assert os.environ["HTTPS_PROXY"] == "http://b.test:2"


def test_sharepoint_keeps_using_https_proxy_through_requests_proxies():
    # `https_proxy` ya existia en dev para SharePoint/Graph: el campo nuevo `http_proxy`
    # no puede cambiar ese comportamiento.
    settings = config.Settings(https_proxy="http://proxy.test:83")
    assert settings.requests_proxies == {
        "http": "http://proxy.test:83",
        "https": "http://proxy.test:83",
    }
    assert config.Settings(https_proxy="").requests_proxies is None


def test_get_settings_applies_the_proxy(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setenv("HTTP_PROXY", "http://desde-env.test:9")
    config.get_settings.cache_clear()
    try:
        config.get_settings()
        assert os.environ["HTTP_PROXY"] == "http://desde-env.test:9"
    finally:
        config.get_settings.cache_clear()
