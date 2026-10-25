"""Unit de `app/connectors_status.py`: chequeo paralelo de conectores con
timeout. Los conectores reales se reemplazan por fakes (nunca red)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import connectors_status as cs


def _fake(result=True, exc=None, delay=0.0):
    class Fake:
        def __init__(self, settings):
            pass

        def test_connection(self):
            if delay:
                time.sleep(delay)
            if exc:
                raise exc
            return result

    return Fake


class _S(SimpleNamespace):
    """Como `Settings` para lo que usa el registro: `model_copy(update=...)` y los atributos."""

    def model_copy(self, update=None):
        return _S(**{**self.__dict__, **(update or {})})


def _settings(**flags):
    base = {
        "enable_oracle": False,
        "enable_bigquery": False,
        "enable_sharepoint": False,
        "enable_sap": False,
        # datos minimos para que cada tipo cuente como "configurado" (`legacy_probe` + campos obligatorios): el estado es por conexion
        "oracle_host": "h", "oracle_user": "u", "oracle_password": "p", "oracle_service_name": "s", "oracle_port": 1521, "oracle_schema": "",
        "sharepoint_site_url": "https://x", "azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t",
        "sharepoint_default_list_name": "",
        "sap_base_url": "https://sap", "sap_user": "u", "sap_password": "p", "sap_client": "100", "sap_language": "EN",
    }
    base.update(flags)
    return _S(**base)


def _patch(monkeypatch, settings, **fakes):
    """Settings falsos y conectores falsos. Con spec conexiones-con-nombre el estado sale del REGISTRO de tipos: los testers importan el conector de
    `app.connectors` al probar, y las conexiones se arman con el `.env` de hoy (sin archivo), asi que se parchean ahi."""
    import importlib

    from app import connections

    monkeypatch.setattr(cs, "get_settings", lambda: settings)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)
    monkeypatch.setattr(connections, "CONNECTIONS_FILE", __import__("pathlib").Path("no-existe-connections.json"))
    connections.reset_for_tests()
    modules = {"OracleConnector": "oracle_connector", "SharePointConnector": "sharepoint_connector", "GraphConnector": "graph_connector", "SapConnector": "sap_connector"}
    for name, cls in fakes.items():
        if name == "BigQueryConnector":
            monkeypatch.setattr(cs, name, cls)
        else:
            monkeypatch.setattr(importlib.import_module(f"app.connectors.{modules[name]}"), name, cls)


def test_all_disabled_returns_empty(monkeypatch):
    _patch(monkeypatch, _settings())
    assert asyncio.run(cs.check_all_connectors()) == []


def test_online_and_error_statuses(monkeypatch):
    _patch(
        monkeypatch,
        _settings(enable_oracle=True, enable_bigquery=True, enable_sap=True),
        OracleConnector=_fake(True),
        BigQueryConnector=_fake(False),
        SapConnector=_fake(exc=RuntimeError("x" * 500)),
    )
    result = {r["name"]: r for r in asyncio.run(cs.check_all_connectors())}
    assert result["oracle"] == {"name": "oracle", "status": "online", "connection": "Datamart", "display": "Oracle - Datamart"}
    assert result["bigquery"]["status"] == "error"
    assert result["sap"]["status"] == "error" and len(result["sap"]["technical"]) == 200  # truncado
    # Pedido del usuario 2026-09-24: lo que se muestra es entendible, no el error crudo.
    assert result["sap"]["detail"].startswith("SAP devolvió un error inesperado.")


def test_sharepoint_flag_checks_sharepoint_and_graph(monkeypatch):
    _patch(
        monkeypatch,
        _settings(enable_sharepoint=True),
        SharePointConnector=_fake(True),
        GraphConnector=_fake(True),
    )
    names = {r["name"] for r in asyncio.run(cs.check_all_connectors())}
    assert names == {"sharepoint", "graph"}


def test_timeout_is_reported_without_blocking_others(monkeypatch):
    monkeypatch.setattr(cs, "CHECK_TIMEOUT", 0.05)
    _patch(
        monkeypatch,
        _settings(enable_oracle=True, enable_bigquery=True),
        OracleConnector=_fake(delay=0.5),
        BigQueryConnector=_fake(True),
    )
    result = {r["name"]: r for r in asyncio.run(cs.check_all_connectors())}
    assert result["oracle"]["status"] == "error" and result["oracle"]["technical"] == "timeout"
    assert result["oracle"]["detail"].startswith("Oracle tardó demasiado en responder.")
    assert result["bigquery"]["status"] == "online"


@pytest.mark.parametrize("value", [True, False])
def test_check_one_maps_truthiness(value):
    assert asyncio.run(cs._check_one("x", lambda: value))["status"] == ("online" if value else "error")


# ---------------------------------------------------------------------------------------------------------------------
# spec conexiones-con-nombre REQ-002: estado POR CONEXION
# ---------------------------------------------------------------------------------------------------------------------
def _with_file(monkeypatch, tmp_path, blocks, **flags):
    import json

    from app import connections

    _patch(monkeypatch, _settings(enable_oracle=True, **flags))
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": blocks}), encoding="utf-8")
    monkeypatch.setattr(connections, "CONNECTIONS_FILE", path)
    connections.reset_for_tests()


def _oracle_block(name, host="h", **drop):
    fields = {"oracle_user": "u", "oracle_password": "p", "oracle_host": host, "oracle_service_name": "s"}
    for k in drop:
        fields.pop(k, None)
    return {"type": "oracle", "name": name, "fields": fields}


def test_each_named_connection_has_its_own_status(monkeypatch, tmp_path):
    """Una caida de «Oracle - Planta» no pinta de rojo a «Oracle - Datamart»: cada una se prueba con SUS credenciales."""
    seen = []

    class Fake:
        def __init__(self, settings):
            seen.append(settings.oracle_host)
            self._ok = settings.oracle_host != "h-planta"

        def test_connection(self):
            return self._ok

    import app.connectors.oracle_connector as connectors_pkg

    _with_file(monkeypatch, tmp_path, [_oracle_block("Datamart", "h-datamart"), _oracle_block("Planta", "h-planta")])
    monkeypatch.setattr(connectors_pkg, "OracleConnector", Fake)

    result = {r["display"]: r["status"] for r in asyncio.run(cs.check_all_connectors())}

    assert result == {"Oracle - Datamart": "online", "Oracle - Planta": "error"}
    assert sorted(seen) == ["h-datamart", "h-planta"]


def test_an_incomplete_connection_is_red_without_calling_the_system_and_names_the_missing_field(monkeypatch, tmp_path):
    called = []

    class Fake:
        def __init__(self, settings):
            called.append(settings.oracle_host)

        def test_connection(self):
            return True

    import app.connectors.oracle_connector as connectors_pkg

    _with_file(monkeypatch, tmp_path, [_oracle_block("Bien", "h-bien"), _oracle_block("Rota", "h-rota", oracle_password=None)])
    monkeypatch.setattr(connectors_pkg, "OracleConnector", Fake)

    result = {r["connection"]: r for r in asyncio.run(cs.check_all_connectors())}

    assert result["Bien"]["status"] == "online"
    assert result["Rota"]["status"] == "error" and "oracle_password" in result["Rota"]["detail"]
    assert called == ["h-bien"], "una conexion incompleta no debe tocar el sistema"
    assert "h-rota" not in str(result["Rota"]), "ningun valor de campo en el estado"


def test_a_disabled_type_is_not_checked_even_with_named_connections(monkeypatch, tmp_path):
    _with_file(monkeypatch, tmp_path, [_oracle_block("Datamart")])
    from app import connections

    monkeypatch.setattr(connections, "get_settings", lambda: _settings(enable_oracle=False))
    monkeypatch.setattr(cs, "get_settings", lambda: _settings(enable_oracle=False))

    assert asyncio.run(cs.check_all_connectors()) == []


def test_a_type_registered_later_is_checked_without_touching_this_module(monkeypatch, tmp_path):
    """REQ-012: un tipo nuevo con `tester` aparece en el estado solo por estar registrado."""
    import json

    from app import connections

    _patch(monkeypatch, _settings())
    probes = []
    connections.register_type(
        connections.ConnectionType("jira", "Jira", (connections.ConnectionField("jira_url", "URL", required=True),), tester=lambda s: probes.append(1) or True)
    )
    try:
        path = tmp_path / "connections.json"
        path.write_text(json.dumps({"connections": [{"type": "jira", "name": "Equipo A", "fields": {"jira_url": "https://j"}}]}), encoding="utf-8")
        monkeypatch.setattr(connections, "CONNECTIONS_FILE", path)
        connections.reset_for_tests()

        result = asyncio.run(cs.check_all_connectors())
    finally:
        connections.unregister_type("jira")

    assert [(r["name"], r["display"], r["status"]) for r in result] == [("jira", "Jira - Equipo A", "online")] and probes == [1]


def test_test_one_connection_uses_the_requested_credentials(monkeypatch, tmp_path):
    import app.connectors.oracle_connector as connectors_pkg

    class Fake:
        def __init__(self, settings):
            self.host = settings.oracle_host

        def test_connection(self):
            return self.host == "h-planta"

    _with_file(monkeypatch, tmp_path, [_oracle_block("Datamart", "h-datamart"), _oracle_block("Planta", "h-planta")])
    monkeypatch.setattr(connectors_pkg, "OracleConnector", Fake)

    ok = asyncio.run(cs.test_one_connection("oracle", "Planta"))
    not_ok = asyncio.run(cs.test_one_connection("oracle", "Datamart"))

    assert ok["ok"] is True and not_ok["ok"] is False and ok["connection"] == "Planta"


def test_test_one_connection_with_an_unknown_name_raises_a_lookup_error(monkeypatch, tmp_path):
    from app import connections

    _with_file(monkeypatch, tmp_path, [_oracle_block("Datamart")])

    with pytest.raises(connections.ConnectionLookupError):
        asyncio.run(cs.test_one_connection("oracle", "Nada"))


def test_test_one_connection_reports_missing_fields_without_connecting(monkeypatch, tmp_path):
    _with_file(monkeypatch, tmp_path, [_oracle_block("Rota", oracle_host=None)])

    result = asyncio.run(cs.test_one_connection("oracle", "Rota"))

    assert result["ok"] is False and "oracle_host" in result["detail"]


# ---------------------------------------------------------------------------------------------------------------------
# Pedido del usuario 2026-10-09: «Probar» decia solo «No se pudo conectar.» (SAP fallaba por el proxy y no habia forma de saberlo sin
# abrir el log). Los conectores atrapan su propio error, lo dejan en el log y devuelven False: el motivo sale de ese aviso, del MISMO hilo.
# ---------------------------------------------------------------------------------------------------------------------
import logging  # noqa: E402

PROXY_FAIL = (
    "No se pudo alcanzar SAP en https://wms.empresa.com/sap/bc/ui2/flp: HTTPSConnectionPool(host='wms.empresa.com', port=443): "
    "Max retries exceeded with url: /sap/bc/ui2/flp (Caused by ProxyError('Unable to connect to proxy', "
    "OSError('Tunnel connection failed: 503 Service Unavailable')))"
)


def _logging_tester(message, logger_name="app.connectors.sap_connector", pause=0.0):
    def tester():
        if pause:
            time.sleep(pause)
        logging.getLogger(logger_name).warning("No disponible: %s", message)
        return False

    return tester


def test_a_failed_check_says_why_from_the_connector_warning():
    result = asyncio.run(cs._check_one("sap", _logging_tester(PROXY_FAIL)))

    assert result["status"] == "error"
    assert "Sin proxy para" in result["detail"] and ".empresa.com" in result["detail"]
    assert "ProxyError" in result["technical"], "el texto crudo queda en el detalle tecnico"


def test_a_failed_check_without_a_warning_keeps_the_same_result_as_before():
    assert asyncio.run(cs._check_one("sap", lambda: False)) == {"name": "sap", "status": "error"}


def test_a_check_that_passes_ignores_old_warnings():
    def tester():
        logging.getLogger("app.connectors.sap_connector").warning("un reintento que despues anduvo")
        return True

    assert asyncio.run(cs._check_one("sap", tester)) == {"name": "sap", "status": "online"}


def test_parallel_checks_never_mix_their_reasons():
    """Oracle falla SIN dejar aviso mientras SAP deja el suyo: Oracle no puede quedarse con el motivo de SAP (se filtra por hilo)."""

    def silent_oracle():
        time.sleep(0.4)  # sigue corriendo mientras SAP loguea
        return False

    async def both():
        return await asyncio.gather(
            cs._check_one("sap", _logging_tester(PROXY_FAIL, pause=0.1)),
            cs._check_one("oracle", silent_oracle),
        )

    sap, oracle = asyncio.run(both())

    assert "Sin proxy para" in sap["detail"]
    assert oracle == {"name": "oracle", "status": "error"}, "el aviso de SAP no se mezcla con Oracle"


def test_probar_shows_the_reason_and_never_the_password(monkeypatch, tmp_path):
    import app.connectors.oracle_connector as connectors_pkg

    class Fake:
        def __init__(self, settings):
            self.password = settings.oracle_password

        def test_connection(self):
            logging.getLogger("app.connectors.oracle_connector").warning(
                "Oracle no disponible: ORA-01017 invalid username/password (clave %s)", self.password
            )
            return False

    block = _oracle_block("Datamart")
    block["fields"]["oracle_password"] = "Clave-Secreta-123"  # el enmascarado ignora valores muy cortos a proposito
    _with_file(monkeypatch, tmp_path, [block])
    monkeypatch.setattr(connectors_pkg, "OracleConnector", Fake)

    result = asyncio.run(cs.test_one_connection("oracle", "Datamart"))

    assert result["ok"] is False and "credenciales" in result["detail"]
    assert "Clave-Secreta-123" not in result["technical"] and "Clave-Secreta-123" not in result["detail"], "la contraseña sale enmascarada"


def test_bigquery_gets_more_time_than_the_rest(monkeypatch):
    """BigQuery es la mas lenta en frio (pide el token a Google y despues lista, por el proxy): con la key del usuario tardo 3,4 s y con la PC
    cargada pasaba de los 8 s generales (2026-10-10: «tardo demasiado» aunque anduviera). Tiene su propio limite; las demas siguen igual."""
    monkeypatch.setattr(cs, "CHECK_TIMEOUT", 0.1)
    monkeypatch.setattr(cs, "BIGQUERY_CHECK_TIMEOUT", 3.0)
    _patch(
        monkeypatch,
        _settings(enable_oracle=True, enable_bigquery=True),
        OracleConnector=_fake(delay=0.5),
        BigQueryConnector=_fake(True, delay=0.5),
    )
    result = {r["name"]: r for r in asyncio.run(cs.check_all_connectors())}
    assert result["oracle"]["technical"] == "timeout", "las demas mantienen su limite"
    assert result["bigquery"]["status"] == "online"
