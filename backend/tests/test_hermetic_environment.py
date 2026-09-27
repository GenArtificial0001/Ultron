"""Auto-verificacion del aislamiento que impone `conftest.py`.

Si estos tests fallan, la suite dejo de ser hermetica (podria leer credenciales
reales, escribir en `%LOCALAPPDATA%\\Vision-personal` -- datos de PRODUCCION -- o salir a
la red). Es la red de seguridad de la propia red de seguridad.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from app import config


def _inside(child: Path, parent: Path) -> bool:
    return parent.resolve() in child.resolve().parents or child.resolve() == parent.resolve()


def test_base_dir_is_inside_session_temp_not_real_data_dir():
    base = config.BASE_DIR
    assert "vision-pytest-" in str(base)
    assert _inside(base, Path(os.environ["VISION_DATA_DIR"]))
    assert not config.IS_FROZEN


def test_runtime_dirs_are_created_under_isolated_base():
    for sub in ("keys", "data", "logs", "workspace", "browser"):
        assert (config.BASE_DIR / sub).is_dir()


def test_home_is_isolated():
    assert "vision-pytest-" in str(Path.home())
    assert "vision-pytest-" in str(config.get_settings().app_log_file)


def test_no_real_credentials_leak_into_settings():
    settings = config.get_settings()
    assert settings.llm_api_key == "" and settings.stt_api_key == ""
    assert settings.access_token == ""
    for name in os.environ:
        assert not name.upper().startswith(("LLM_", "STT_", "ORACLE_", "SHAREPOINT_", "SAP_"))


def test_external_network_is_blocked():
    with pytest.raises(RuntimeError, match="red bloqueado"):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(RuntimeError, match="red bloqueado"):
        socket.getaddrinfo("oracle.empresa.example", 1521)


def test_loopback_is_still_allowed():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


@pytest.mark.network
def test_network_marker_disables_the_guard():
    # No hace conexion real: solo confirma que el guard NO esta activo.
    assert socket.getaddrinfo("localhost", 80)
    assert socket.socket.connect.__name__ == "connect"


def test_master_db_is_redirected_to_test_tmp(tmp_master_db):
    from app.persistence import master_index as mi

    assert mi.master_db_path() == tmp_master_db
    assert "vision-pytest-" in str(tmp_master_db) or "pytest-of-" in str(tmp_master_db)


def test_project_config_is_isolated_per_test_first(tmp_path):
    """Par de tests: el 1ro guarda un override; el 2do (abajo) verifica que NO se filtra.
    Regresion del bug de orden entre test_connections_config_endpoint y test_bigquery_guardrails."""
    from app import project_context as pc

    pc.set_connection_thresholds(bq_cost_guardrail_bytes=123)
    assert pc.get_project_config().bq_cost_guardrail_bytes == 123
    assert pc.CONFIG_FILE.parent == tmp_path


def test_project_config_is_isolated_per_test_second():
    from app import project_context as pc

    assert pc.get_project_config().bq_cost_guardrail_bytes is None
