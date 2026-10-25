"""Aviso de credenciales del lanzador (`launch_vision.py`, el que corre `Vision.exe`).

Bug real (2026-09-25, reportado por el usuario): al abrir el `.exe` aparecia
"AVISO: no existe backend\\.env todavia" aunque su `.env` estaba completo en
`%LOCALAPPDATA%\\Vision-personal` -- la carpeta de datos definida (`app/config.py:BASE_DIR`,
fix del 2026-09-19). El lanzador seguia buscando la ubicacion VIEJA
(`%LOCALAPPDATA%\\Vision-personal\\app\\backend\\.env`). La app si leia el correcto: el
aviso era falso. Estos tests usan SOLO carpetas temporales.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app import config

pytestmark = pytest.mark.unit

_LAUNCHER = Path(__file__).resolve().parents[2] / "launch_vision.py"
_spec = importlib.util.spec_from_file_location("launch_vision_under_test", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

# spec conexiones-con-nombre: Oracle, SharePoint/Graph y SAP ya no se avisan desde `.env`; siguen viniendo de ahi la clave de IA y BigQuery.
ALL_KEYS = "LLM_API_KEY=x\nBQ_PROJECT_ID=x\n"


@pytest.fixture(autouse=True)
def _no_real_connections_file(monkeypatch, tmp_path):
    """Nunca se mira el `~/.vision/connections.json` REAL del usuario: por defecto no hay archivo."""
    monkeypatch.setenv("VISION_CONNECTIONS_FILE", str(tmp_path / "sin-archivo" / "connections.json"))


@pytest.fixture
def dirs(tmp_path):
    data_dir = tmp_path / "Vision-personal"
    backend_dir = data_dir / "app" / "backend"  # donde el .exe extrae el codigo
    backend_dir.mkdir(parents=True)
    return backend_dir, data_dir


def test_data_dir_follows_the_same_rule_as_the_app(monkeypatch, tmp_path):
    backend_dir = tmp_path / "app" / "backend"
    monkeypatch.delenv("VISION_DEV_MODE", raising=False)
    monkeypatch.delenv("VISION_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert launcher._data_dir(backend_dir) == tmp_path / "Local" / "Vision-personal"  # .exe
    monkeypatch.setenv("VISION_DATA_DIR", str(tmp_path / "otra"))
    assert launcher._data_dir(backend_dir) == (tmp_path / "otra").resolve()
    monkeypatch.setenv("VISION_DEV_MODE", "1")
    assert launcher._data_dir(backend_dir) == backend_dir  # dev: backend/


def test_launcher_and_app_agree_in_this_environment():
    # conftest fija VISION_DATA_DIR a un temp: los dos tienen que resolver lo mismo.
    assert launcher._data_dir(Path("no-importa")) == config.BASE_DIR


def test_env_in_the_data_folder_is_found_even_without_backend_env(dirs, capsys):
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text(ALL_KEYS, encoding="utf-8")
    assert not (backend_dir / ".env").exists()  # el caso real del usuario
    launcher._check_credentials(backend_dir, data_dir)
    assert capsys.readouterr().out == ""


def test_credentials_saved_encrypted_count_as_present(dirs, capsys):
    backend_dir, data_dir = dirs
    names = ["llm_api_key", "bq_project_id"]
    (data_dir / ".env.enc").write_text(json.dumps(dict.fromkeys(names, "gAAAA-cifrado")), encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    assert capsys.readouterr().out == ""


def test_missing_ones_are_listed_from_both_sources(dirs, capsys):
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text("BQ_PROJECT_ID=\n", encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    out = capsys.readouterr().out
    assert "BQ_PROJECT_ID vacio" in out and "BigQuery" in out
    assert str(data_dir / ".env") in out and "Configuracion > Credenciales" in out


def test_the_old_connector_keys_are_no_longer_warned_about_from_env(dirs, capsys):
    """Un aviso «ORACLE_PASSWORD vacio» seria FALSO: Oracle/SharePoint/SAP ya se configuran en connections.json, no en `.env` ni en `.env.enc`."""
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text("BQ_PROJECT_ID=x\n", encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    out = capsys.readouterr().out
    for stale in ("ORACLE_PASSWORD", "AZURE_CLIENT_SECRET", "SAP_PASSWORD"):
        assert stale not in out


def test_nothing_configured_points_to_the_real_folder(dirs, capsys):
    backend_dir, data_dir = dirs
    launcher._check_credentials(backend_dir, data_dir)
    out = capsys.readouterr().out
    assert f"no existe {data_dir / '.env'}" in out
    # spec gestor-conexiones C3: ya no se manda a copiar `.env.example`; se configura en Vision
    assert ".env.example" not in out and "Configuracion > Conexiones" in out
    assert "connections.json" in out
    assert "no existe backend\\.env" not in out  # el aviso viejo, con la ruta equivocada


def test_unreadable_encrypted_file_is_ignored(dirs, capsys):
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text(ALL_KEYS, encoding="utf-8")
    (data_dir / ".env.enc").write_text("{no es json", encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    assert capsys.readouterr().out == ""


# --- spec conexiones-con-nombre: el aviso mira tambien ~/.vision/connections.json -------------------------------------
def _write_connections(tmp_path, monkeypatch, content):
    path = tmp_path / "home" / ".vision" / "connections.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("VISION_CONNECTIONS_FILE", str(path))
    return path


def test_a_connections_file_with_connections_silences_the_nothing_configured_warning(dirs, capsys, tmp_path, monkeypatch):
    backend_dir, data_dir = dirs
    _write_connections(tmp_path, monkeypatch, json.dumps({"connections": [{"type": "oracle", "name": "Datamart", "fields": {}}]}))
    launcher._check_credentials(backend_dir, data_dir)
    assert "todavia no hay conexiones configuradas" not in capsys.readouterr().out


def test_a_broken_connections_file_is_reported_without_echoing_its_content(dirs, capsys, tmp_path, monkeypatch):
    backend_dir, data_dir = dirs
    _write_connections(tmp_path, monkeypatch, '{"connections": [ {"fields": {"oracle_password": "SECRETO-LANZADOR"}')
    launcher._check_credentials(backend_dir, data_dir)
    out = capsys.readouterr().out
    assert "no se pudo leer" in out and "SECRETO-LANZADOR" not in out


def test_the_connections_summary_only_counts_blocks_and_never_reads_fields(tmp_path, monkeypatch):
    path = _write_connections(tmp_path, monkeypatch, json.dumps([{"type": "oracle", "name": "A"}, {"type": "sap", "name": "B"}]))
    assert launcher._connections_file_summary(path) == (2, None)
    assert launcher._connections_file_summary(tmp_path / "no-existe.json") == (0, None)


def test_the_launcher_and_the_app_agree_on_where_connections_json_lives(monkeypatch, tmp_path):
    from app import connections

    monkeypatch.delenv("VISION_CONNECTIONS_FILE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert launcher._connections_file() == connections._default_connections_file()
    monkeypatch.setenv("VISION_CONNECTIONS_FILE", str(tmp_path / "otro.json"))
    assert launcher._connections_file() == connections._default_connections_file() == tmp_path / "otro.json"
# ---------------------------------------------------------------------------
# spec vision-generico: la clave del proveedor de IA, los conectores apagados
# y el proxy de la empresa para instalar dependencias
# ---------------------------------------------------------------------------
def test_missing_ai_key_is_warned(dirs, capsys):
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text(ALL_KEYS.replace("LLM_API_KEY=x", "LLM_API_KEY="), encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    out = capsys.readouterr().out
    assert "LLM_API_KEY vacio: el chat con inteligencia artificial no va a funcionar." in out


def test_disabled_connectors_do_not_ask_for_their_credentials(dirs, capsys):
    backend_dir, data_dir = dirs
    (data_dir / ".env").write_text("LLM_API_KEY=x\nENABLE_ORACLE=false\nENABLE_BIGQUERY=false\nENABLE_SHAREPOINT=false\nENABLE_SAP=false\n", encoding="utf-8")
    launcher._check_credentials(backend_dir, data_dir)
    assert capsys.readouterr().out == ""


def test_pip_uses_the_company_proxy_from_the_env_file(dirs, monkeypatch):
    backend_dir, data_dir = dirs
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    (data_dir / ".env").write_text("HTTPS_PROXY=http://proxy.empresa.test:8080\n", encoding="utf-8")
    env = launcher._proxy_env(data_dir)
    assert env["HTTPS_PROXY"] == "http://proxy.empresa.test:8080"
    assert "127.0.0.1" in env["NO_PROXY"]  # Vision mismo siempre directo


def test_windows_proxy_wins_over_the_env_file(dirs, monkeypatch):
    backend_dir, data_dir = dirs
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.windows.test:3128")
    (data_dir / ".env").write_text("HTTPS_PROXY=http://proxy.empresa.test:8080\n", encoding="utf-8")
    assert launcher._proxy_env(data_dir)["HTTPS_PROXY"] == "http://proxy.windows.test:3128"


def test_without_proxy_nothing_is_added(dirs, monkeypatch):
    backend_dir, data_dir = dirs
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    env = launcher._proxy_env(data_dir)
    assert "HTTPS_PROXY" not in env and "NO_PROXY" not in env
