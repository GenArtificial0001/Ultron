"""Unit de `app/browser_workspace.py`: carpeta de vista previa por sesion.
Foco de seguridad: path traversal (`_safe_join`) y tipos MIME."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import browser_workspace as bw


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "get_settings", lambda: SimpleNamespace(browser_root_path=tmp_path / "browser"))
    return tmp_path / "browser"


def test_get_does_not_create_but_get_or_create_does(root):
    assert not bw.get_browser_dir("s1").exists()
    d = bw.get_or_create_browser_dir("s1")
    assert d.is_dir() and d == root / "s1"


def test_delete_returns_whether_something_existed():
    assert bw.delete_browser_dir("nada") is False
    d = bw.get_or_create_browser_dir("s1")
    (d / "a.txt").write_text("x")
    assert bw.delete_browser_dir("s1") is True
    assert not d.exists()


@pytest.mark.parametrize(
    "attack", ["../secreto.txt", "../../etc/passwd", "sub/../../fuera.txt", "..\\..\\fuera.txt"]
)
def test_path_traversal_is_rejected(root, attack):
    d = bw.get_or_create_browser_dir("s1")
    (root / "secreto.txt").write_text("no debe salir")
    assert bw._safe_join("s1", attack) is None
    assert bw.read_file("s1", attack) is None
    assert d.exists()


def test_absolute_looking_path_is_confined_to_session(root):
    bw.get_or_create_browser_dir("s1")
    (root / "s1" / "etc").mkdir()
    (root / "s1" / "etc" / "hosts").write_text("dentro")
    content, _ = bw.read_file("s1", "/etc/hosts")  # el '/' inicial se recorta
    assert content == b"dentro"


def test_read_file_returns_bytes_and_content_type(root):
    d = bw.get_or_create_browser_dir("s1")
    (d / "index.html").write_text("<h1>hola</h1>", encoding="utf-8")
    assert bw.read_file("s1", "index.html") == (b"<h1>hola</h1>", "text/html; charset=utf-8")


def test_read_missing_or_directory_returns_none(root):
    d = bw.get_or_create_browser_dir("s1")
    (d / "carpeta").mkdir()
    assert bw.read_file("s1", "no-existe.html") is None
    assert bw.read_file("s1", "carpeta") is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.JS", "application/javascript; charset=utf-8"),
        ("a.css", "text/css; charset=utf-8"),
        ("a.png", "image/png"),
        ("a.svg", "image/svg+xml"),
        ("a.bin", "application/octet-stream"),
        ("sinextension", "application/octet-stream"),
    ],
)
def test_content_types(name, expected, tmp_path):
    assert bw._guess_content_type(tmp_path / name) == expected


def test_find_index_file_priority_and_fallback(root):
    assert bw.find_index_file("s1") is None  # carpeta inexistente
    d = bw.get_or_create_browser_dir("s1")
    assert bw.find_index_file("s1") is None  # carpeta vacia
    (d / "zeta.html").write_text("z")
    (d / "alfa.html").write_text("a")
    assert bw.find_index_file("s1") == "alfa.html"  # fallback alfabetico
    (d / "dashboard.html").write_text("d")
    assert bw.find_index_file("s1") == "dashboard.html"
    (d / "index.html").write_text("i")
    assert bw.find_index_file("s1") == "index.html"  # prioridad


def test_fingerprint_changes_with_content(root):
    assert bw.get_fingerprint("s1") == "empty"
    d = bw.get_or_create_browser_dir("s1")
    empty = bw.get_fingerprint("s1")
    (d / "a.txt").write_text("x")
    one = bw.get_fingerprint("s1")
    (d / "a.txt").write_text("xxxxxxxx")
    two = bw.get_fingerprint("s1")
    assert empty.startswith("0:") and one.startswith("1:")
    assert len({empty, one, two}) == 3
