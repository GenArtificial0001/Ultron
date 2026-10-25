"""Migracion de la carpeta de datos de Vision-Personal a ULTRON (spec portar-desde-origen REQ-009). Solo carpetas temporales."""

from __future__ import annotations

import json

from app import data_migration as dm


def _old_install(tmp_path):
    old = tmp_path / "Vision-personal"
    (old / "keys").mkdir(parents=True)
    (old / ".env").write_text("LLM_API_KEY=clave-de-prueba\n", encoding="utf-8")
    (old / "keys" / "credentials.key").write_bytes(b"clave-local")
    (old / "vision.db").write_bytes(b"indice")
    (old / "app" / "backend").mkdir(parents=True)
    (old / "app" / "backend" / "pesado.py").write_text("x", encoding="utf-8")
    (old / "logs").mkdir()
    (old / "logs" / "app.log").write_text("log viejo", encoding="utf-8")
    return old


def test_the_first_start_copies_the_data_and_leaves_the_old_folder_untouched(tmp_path):
    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    before = sorted(p.relative_to(old).as_posix() for p in old.rglob("*"))

    copied = dm.migrate(old, new)

    assert copied == [".env", "keys", "vision.db"]
    assert (new / ".env").read_text(encoding="utf-8") == "LLM_API_KEY=clave-de-prueba\n"
    assert (new / "keys" / "credentials.key").read_bytes() == b"clave-local"
    assert not (new / "app").exists() and not (new / "logs").exists(), "ni el codigo extraido ni los logs viejos"
    assert sorted(p.relative_to(old).as_posix() for p in old.rglob("*")) == before, "la carpeta vieja no cambia"


def test_it_never_overwrites_an_ultron_that_already_has_data(tmp_path):
    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    new.mkdir()
    (new / ".env").write_text("LLM_API_KEY=la-de-ultron\n", encoding="utf-8")

    assert dm.migrate(old, new) is None
    assert (new / ".env").read_text(encoding="utf-8") == "LLM_API_KEY=la-de-ultron\n"
    assert not (new / "vision.db").exists()


def test_the_launcher_folder_alone_does_not_count_as_data(tmp_path):
    """El lanzador crea `Ultron/app` ANTES de que arranque la app: eso no cuenta como datos de ULTRON."""
    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    (new / "app").mkdir(parents=True)

    assert dm.migrate(old, new) == [".env", "keys", "vision.db"]


def test_without_an_old_install_nothing_happens(tmp_path):
    assert dm.migrate(tmp_path / "no-existe", tmp_path / "Ultron") is None
    empty = tmp_path / "Vision-personal"
    (empty / "logs").mkdir(parents=True)
    assert dm.migrate(empty, tmp_path / "Ultron") is None, "una carpeta sin datos de la persona no se copia"


def test_the_chat_notice_comes_once_and_says_what_was_copied(tmp_path):
    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    dm.migrate(old, new)

    notice = dm.take_notice(new)
    assert notice and notice["copied"] == [".env", "keys", "vision.db"]
    assert ".env" in notice["message"] and "intacta" in notice["message"]
    assert dm.take_notice(new) is None, "una sola vez"
    assert json.loads((new / dm.NOTICE_FILE).read_text(encoding="utf-8"))["notified"] is True


def test_a_second_start_does_not_copy_again(tmp_path):
    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    dm.migrate(old, new)
    (old / ".env").write_text("LLM_API_KEY=cambiada-despues\n", encoding="utf-8")

    assert dm.migrate(old, new) is None
    assert (new / ".env").read_text(encoding="utf-8") == "LLM_API_KEY=clave-de-prueba\n"


def test_the_status_route_carries_the_notice_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    old, new = _old_install(tmp_path), tmp_path / "Ultron"
    dm.migrate(old, new)
    monkeypatch.setattr(main, "BASE_DIR", new)
    api = TestClient(main.app)

    first = api.get("/api/status").json()["data_migration"]
    assert first and "Vision-Personal" in first["message"]
    assert api.get("/api/status").json()["data_migration"] is None


def test_the_previous_folder_name_is_the_old_one_and_the_new_one_is_ultron():
    """Guarda contra un renombre automatico: la carpeta de origen de la migracion es la VIEJA."""
    from app import config

    assert dm.PREVIOUS_FOLDER_NAME == "Vision-" + "personal"
    assert config.DATA_FOLDER_NAME == "Ultron"
