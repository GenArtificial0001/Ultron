"""El lanzador sin `.env` (spec gestor-conexiones C2: REQ-002/011): lee de `vision.db` de la carpeta de datos (ULTRON: nunca `~/.vision`) si la configuracion ya esta en Vision y el puerto elegido, en
modo solo lectura y sin descifrar nada; cualquier valor raro es el de siempre (8030). Solo carpetas temporales."""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import config_store as cs
from app import secret_store
from app.persistence import master_index

_LAUNCHER = Path(__file__).resolve().parents[2] / "launch_vision.py"
_spec = importlib.util.spec_from_file_location("launch_vision_vault_under_test", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def make_db(home: Path, settings: dict[str, str]) -> Path:
    db = home / "vision.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:  # closing() cierra; el segundo `conn` confirma la transaccion
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
        conn.executemany("INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)", settings.items())
    return db


def test_without_a_vision_database_the_usual_port_and_no_vault(tmp_path):
    assert launcher._configured_port(tmp_path) == 8030 and launcher._vault_active(tmp_path) is False
    assert not (tmp_path / "vision.db").exists(), "leer nunca crea la base"


def test_the_port_chosen_in_vision_is_used(tmp_path):
    make_db(tmp_path, {"launcher_app_port": "8021"})

    assert launcher._configured_port(tmp_path) == 8021


@pytest.mark.parametrize("raw", ["ocho mil", "0", "70000", ""])
def test_a_strange_port_falls_back_to_the_usual_one(tmp_path, raw):
    make_db(tmp_path, {"launcher_app_port": raw})

    assert launcher._configured_port(tmp_path) == 8030


def test_a_database_without_the_settings_table_is_harmless(tmp_path):
    db = tmp_path / "vision.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(db).close()

    assert launcher._configured_port(tmp_path) == 8030 and launcher._vault_active(tmp_path) is False


def test_the_vault_is_detected_when_the_configuration_lives_in_vision(tmp_path):
    make_db(tmp_path, {"config_source": "vision_db"})

    assert launcher._vault_active(tmp_path) is True


def test_reading_never_modifies_the_database(tmp_path):
    db = make_db(tmp_path, {"launcher_app_port": "8022", "config_source": "vision_db"})
    before = db.read_bytes()

    launcher._configured_port(tmp_path)
    launcher._vault_active(tmp_path)

    assert db.read_bytes() == before


@pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")
def test_what_vision_saves_is_what_the_launcher_reads_and_only_the_port_is_in_clear(tmp_path, monkeypatch):
    """De punta a punta: la app guarda el puerto (cifrado + copia en claro) y el lanzador lo lee de la MISMA base; ningun secreto queda en claro."""
    db = tmp_path / "vision.db"
    monkeypatch.setattr(master_index, "master_db_path", lambda: db)
    cs.invalidate()

    cs.set_general({"app_port": "8023", "fish_api_key": "Clave-Fish-Secreta"})

    assert launcher._configured_port(tmp_path) == 8023 and launcher._vault_active(tmp_path) is True
    with closing(sqlite3.connect(db)) as conn:
        plain = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    assert "Clave-Fish-Secreta" not in repr(plain) and plain.get("launcher_app_port") == "8023"

    cs.set_general({"app_port": None})
    assert launcher._configured_port(tmp_path) == 8030, "borrar el puerto vuelve al de siempre"
    cs.invalidate()
