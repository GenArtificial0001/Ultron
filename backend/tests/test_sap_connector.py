"""Conector de SAP (`app/connectors/sap_connector.py`, BL-010): login por formulario, token CSRF, cache de sesion y OData paginado.

Estaba en 15,9 % de cobertura y lo usa la tool `sap_query` en cada consulta a SAP EWM/WMS. Sin red: `requests.Session` se reemplaza por un SAP falso
que sirve la pagina de logon (con el token `sap-login-XSRF`), acepta o rechaza el POST de login, entrega el `x-csrf-token` y pagina el OData.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.config import Settings
from app.connectors import sap_connector as sc

LOGON_OK = '<form><input type="hidden" name="sap-login-XSRF" value="tok&amp;123"></form>'
LOGON_AGAIN = '<input name="sap-login-XSRF" value="x"> <input name="sap-password">'  # SAP rechazo la clave: vuelve a mostrar el formulario


def resp(status=200, text="", headers=None, body=None):
    return SimpleNamespace(status_code=status, text=text, headers=headers or {}, json=lambda: body or {})


class FakeSession:
    """Una `requests.Session` de SAP. `script` mapea un marcador de la URL al resultado de la proxima llamada GET; `posts` guarda cada login enviado."""

    def __init__(
        self,
        logon_text=LOGON_OK,
        post_text="<html>Fiori Launchpad</html>",
        csrf="CSRF-1",
        rows=0,
        page_size=200,
        fail=None,
        send_count=True,
    ):
        self.logon_text, self.post_text, self.csrf, self.send_count = logon_text, post_text, csrf, send_count
        self.rows, self.page_size, self.fail = rows, page_size, fail or {}
        self.gets: list[tuple[str, dict, dict]] = []
        self.posts: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params or {}, headers or {}))
        if "get" in self.fail:
            raise requests.ConnectionError(self.fail["get"])
        if "/sap/bc/ui2/flp" in url:
            return resp(text=self.logon_text)
        if url.endswith("$metadata"):
            return self.fail.get("metadata") or resp(text="<edmx/>")
        if (headers or {}).get("X-CSRF-Token") == "Fetch":
            return resp(headers={"x-csrf-token": self.csrf} if self.csrf else {}, status=200)
        return self._odata_page(params or {})

    def post(self, url, params=None, data=None, headers=None, timeout=None, allow_redirects=None):
        if "post" in self.fail:
            raise requests.ConnectionError(self.fail["post"])
        self.posts.append(data)
        return resp(text=self.post_text)

    def _odata_page(self, params):
        if "odata" in self.fail:
            return self.fail["odata"]
        skip = int(params.get("$skip", 0))
        top = int(params.get("$top", self.page_size))
        results = [{"Id": i + 1, "__metadata": {"uri": "x"}} for i in range(skip, min(skip + top, self.rows))]
        d = {"results": results}
        if skip == 0 and self.send_count:
            d["__count"] = str(self.rows)
        return resp(body={"d": d})


@pytest.fixture(autouse=True)
def clean_cache():
    sc._session_cache.clear()
    yield
    sc._session_cache.clear()


def make(session: FakeSession, monkeypatch, **settings):
    cfg = Settings(
        sap_base_url="https://sap.empresa.test/",
        sap_client="100",
        sap_user="usuario1",
        sap_password="clave-falsa",
        sap_language="ES",
        **settings,
    )
    monkeypatch.setattr(sc.requests, "Session", lambda: session)
    return sc.SapConnector(cfg)


# ---------------------------------------------------------------------------
# Login por formulario
# ---------------------------------------------------------------------------
class TestLogin:
    def test_it_loads_the_logon_page_twice_then_posts_the_form_with_the_unescaped_xsrf_token(self, monkeypatch):
        s = FakeSession()
        make(s, monkeypatch)._login()

        logon_gets = [g for g in s.gets if "/sap/bc/ui2/flp" in g[0]]
        assert len(logon_gets) == 2  # el primer GET fija la cookie; el segundo trae el formulario con el token
        assert logon_gets[0][1] == {"sap-client": "100", "sap-language": "ES"}
        form = s.posts[0]
        assert form["sap-login-XSRF"] == "tok&123"  # &amp; del HTML -> &
        assert (form["sap-user"], form["sap-password"], form["sap-client"], form["sap-language"]) == (
            "usuario1",
            "clave-falsa",
            "100",
            "ES",
        )

    def test_a_second_login_on_the_same_instance_does_nothing(self, monkeypatch):
        s = FakeSession()
        c = make(s, monkeypatch)
        c._login()
        gets, posts = len(s.gets), len(s.posts)

        c._login()

        assert (len(s.gets), len(s.posts)) == (gets, posts)

    def test_a_missing_xsrf_token_says_to_check_the_url(self, monkeypatch):
        c = make(FakeSession(logon_text="<html>otra pagina</html>"), monkeypatch)

        with pytest.raises(sc.SapConnectionError, match="sap-login-XSRF.*SAP_BASE_URL"):
            c._login()

    def test_rejected_credentials_say_to_check_user_and_password(self, monkeypatch):
        c = make(FakeSession(post_text=LOGON_AGAIN), monkeypatch)

        with pytest.raises(sc.SapConnectionError, match="Login SAP rechazado.*SAP_USER/SAP_PASSWORD"):
            c._login()
        assert c._logged_in is False  # y no queda marcado como logueado

    def test_a_network_failure_loading_the_logon_page_is_reported(self, monkeypatch):
        c = make(FakeSession(fail={"get": "sin ruta"}), monkeypatch)

        with pytest.raises(sc.SapConnectionError, match="No se pudo alcanzar SAP.*sin ruta"):
            c._login()

    def test_a_network_failure_sending_the_login_is_reported(self, monkeypatch):
        c = make(FakeSession(fail={"post": "reset"}), monkeypatch)

        with pytest.raises(sc.SapConnectionError, match="No se pudo enviar el login.*reset"):
            c._login()

    def test_the_error_message_never_contains_the_password(self, monkeypatch):
        c = make(FakeSession(post_text=LOGON_AGAIN), monkeypatch)

        with pytest.raises(sc.SapConnectionError) as err:
            c._login()
        assert "clave-falsa" not in str(err.value)


# ---------------------------------------------------------------------------
# Cache de sesion entre instancias (cada tool-call crea un conector nuevo)
# ---------------------------------------------------------------------------
class TestSessionCache:
    def test_a_second_connector_reuses_the_session_without_logging_in_again(self, monkeypatch):
        s = FakeSession()
        make(s, monkeypatch)._login()
        posts_before = len(s.posts)

        second = make(FakeSession(), monkeypatch)  # otra instancia, otra Session "nueva" que NO deberia usarse

        assert second._logged_in is True and second._session is s
        second._login()
        assert len(s.posts) == posts_before

    def test_the_cache_expires_after_the_ttl(self, monkeypatch):
        s = FakeSession()
        make(s, monkeypatch)._login()
        key = next(iter(sc._session_cache))
        sc._session_cache[key]["logged_in_at"] -= sc._SESSION_TTL_SECONDS + 1

        fresh = FakeSession()
        again = make(fresh, monkeypatch)

        assert again._logged_in is False and again._session is fresh

    def test_a_different_user_does_not_share_the_session(self, monkeypatch):
        make(FakeSession(), monkeypatch)._login()
        other_session = FakeSession()
        cfg = Settings(sap_base_url="https://sap.empresa.test/", sap_client="100", sap_user="otro", sap_password="x")
        monkeypatch.setattr(sc.requests, "Session", lambda: other_session)

        other = sc.SapConnector(cfg)

        assert other._logged_in is False and other._session is other_session

    def test_csrf_tokens_are_shared_between_instances(self, monkeypatch):
        s = FakeSession()
        first = make(s, monkeypatch)
        first._get_csrf_token("ZSERV_SRV")

        second = make(FakeSession(), monkeypatch)

        assert second._get_csrf_token("ZSERV_SRV") == "CSRF-1"
        assert len([g for g in s.gets if g[2].get("X-CSRF-Token") == "Fetch"]) == 1  # el token se pidio una sola vez


# ---------------------------------------------------------------------------
# Token CSRF
# ---------------------------------------------------------------------------
class TestCsrf:
    def test_it_fetches_the_token_once_per_service_and_logs_in_first(self, monkeypatch):
        s = FakeSession()
        c = make(s, monkeypatch)

        assert c._get_csrf_token("ZSERV_SRV") == "CSRF-1"
        assert c._get_csrf_token("ZSERV_SRV") == "CSRF-1"

        fetches = [g for g in s.gets if g[2].get("X-CSRF-Token") == "Fetch"]
        assert len(fetches) == 1
        assert fetches[0][0] == "https://sap.empresa.test/sap/opu/odata/sap/ZSERV_SRV/"
        assert fetches[0][1] == {"sap-client": "100"} and s.posts  # y hizo login antes

    def test_each_service_has_its_own_token(self, monkeypatch):
        s = FakeSession()
        c = make(s, monkeypatch)
        c._get_csrf_token("A_SRV")
        s.csrf = "CSRF-2"

        assert c._get_csrf_token("B_SRV") == "CSRF-2"
        assert c._csrf_tokens == {"A_SRV": "CSRF-1", "B_SRV": "CSRF-2"}

    def test_a_service_that_returns_no_token_names_the_service_and_the_status(self, monkeypatch):
        c = make(FakeSession(csrf=None), monkeypatch)

        with pytest.raises(sc.SapConnectionError, match=r"ZSERV_SRV.*HTTP 200"):
            c._get_csrf_token("ZSERV_SRV")

    def test_a_network_failure_fetching_the_token_is_reported(self, monkeypatch):
        s = FakeSession()
        c = make(s, monkeypatch)
        c._login()
        s.fail = {"get": "timeout"}

        with pytest.raises(sc.SapConnectionError, match="CSRF token de ZSERV_SRV.*timeout"):
            c._get_csrf_token("ZSERV_SRV")


# ---------------------------------------------------------------------------
# test_connection y metadata
# ---------------------------------------------------------------------------
class TestConnectionAndMetadata:
    def test_test_connection_is_true_when_login_works(self, monkeypatch):
        assert make(FakeSession(), monkeypatch).test_connection() is True

    def test_test_connection_is_false_instead_of_raising_when_login_fails(self, monkeypatch):
        assert make(FakeSession(post_text=LOGON_AGAIN), monkeypatch).test_connection() is False

    def test_metadata_returns_the_xml(self, monkeypatch):
        s = FakeSession()
        xml = make(s, monkeypatch).get_service_metadata("ZSERV_SRV")

        assert xml == "<edmx/>"
        assert any(g[0].endswith("/ZSERV_SRV/$metadata") for g in s.gets)

    def test_metadata_http_error_is_reported_with_the_beginning_of_the_body(self, monkeypatch):
        s = FakeSession(fail={"metadata": resp(status=404, text="No existe el servicio" + "x" * 500)})
        c = make(s, monkeypatch)

        with pytest.raises(sc.SapConnectionError, match="metadata de 'ZSERV_SRV': No existe el servicio") as err:
            c.get_service_metadata("ZSERV_SRV")
        assert len(str(err.value)) < 400  # el cuerpo se corta a 300 caracteres


# ---------------------------------------------------------------------------
# fetch_generic: OData paginado
# ---------------------------------------------------------------------------
class TestFetchGeneric:
    def test_it_returns_a_dataframe_without_the_odata_metadata_column(self, monkeypatch):
        df = make(FakeSession(rows=3), monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos")

        assert list(df["Id"]) == [1, 2, 3]
        assert "__metadata" not in df.columns

    def test_it_pages_until_the_count_is_reached(self, monkeypatch):
        s = FakeSession(rows=5)
        df = make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos", page_size=2)

        assert len(df) == 5
        skips = [int(g[1].get("$skip", 0)) for g in s.gets if "Pedidos" in g[0]]
        assert skips == [0, 2, 4]

    def test_it_sends_the_csrf_token_and_json_accept_on_every_page(self, monkeypatch):
        s = FakeSession(rows=4)
        make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos", page_size=2)

        pages = [g for g in s.gets if "Pedidos" in g[0]]
        assert len(pages) == 2
        assert all(g[2] == {"Accept": "application/json", "X-CSRF-Token": "CSRF-1"} for g in pages)
        assert all(g[1].get("sap-client") == "100" for g in pages)

    def test_it_stops_at_max_pages_even_if_there_are_more_rows(self, monkeypatch):
        s = FakeSession(rows=1000)
        df = make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos", page_size=10, max_pages=3)

        assert len(df) == 30
        assert len([g for g in s.gets if "Pedidos" in g[0]]) == 3

    def test_without_a_count_it_stops_on_the_first_empty_page(self, monkeypatch):
        """Hay servicios OData que no devuelven `__count`: ahi lo unico que corta la paginacion es una pagina vacia."""
        s = FakeSession(rows=5, send_count=False)
        df = make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos", page_size=2)

        assert len(df) == 5
        skips = [int(g[1].get("$skip", 0)) for g in s.gets if "Pedidos" in g[0]]
        assert skips == [0, 2, 4, 6]  # 3 paginas con datos y una cuarta vacia que corta

    def test_by_default_it_stops_after_25_pages(self, monkeypatch):
        """El tope por defecto evita cientos de requests secuenciales (nota de rendimiento 2026-09 del conector)."""
        s = FakeSession(rows=100_000)
        df = make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos", page_size=10)

        assert len(df) == 250
        assert len([g for g in s.gets if "Pedidos" in g[0]]) == 25

    def test_an_empty_result_gives_an_empty_dataframe_after_one_request(self, monkeypatch):
        s = FakeSession(rows=0)
        df = make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos")

        assert df.empty and len([g for g in s.gets if "Pedidos" in g[0]]) == 1

    def test_the_filter_and_select_reach_the_request(self, monkeypatch):
        s = FakeSession(rows=1)
        make(s, monkeypatch).fetch_generic(
            "ZSERV_SRV", "Pedidos", select_fields=["Id", "Estado"], odata_filter="Estado eq 'A'"
        )

        params = next(g[1] for g in s.gets if "Pedidos" in g[0])
        assert params["$select"] == "Id,Estado" and params["$filter"] == "Estado eq 'A'"

    def test_an_http_error_includes_the_status_and_the_start_of_the_body(self, monkeypatch):
        s = FakeSession(fail={"odata": resp(status=500, text="Error interno de SAP " + "x" * 600)})

        with pytest.raises(sc.SapConnectionError, match=r"HTTP 500.*ZSERV_SRV/Pedidos.*Error interno de SAP") as err:
            make(s, monkeypatch).fetch_generic("ZSERV_SRV", "Pedidos")
        assert len(str(err.value)) < 600  # cuerpo cortado a 400

    def test_a_network_failure_mid_query_is_reported_with_service_and_entity(self, monkeypatch):
        s = FakeSession(rows=5)
        c = make(s, monkeypatch)
        c._get_csrf_token("ZSERV_SRV")
        s.fail = {"get": "se corto la red"}

        with pytest.raises(sc.SapConnectionError, match="ZSERV_SRV/Pedidos.*se corto la red"):
            c.fetch_generic("ZSERV_SRV", "Pedidos")
