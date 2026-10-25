"""Conector de Microsoft Graph (`app/connectors/graph_connector.py`, BL-010): token de Azure AD con cache, directorio (usuario y manager) y Microsoft Search.

Estaba en 22 % de cobertura y lo usan las tools `graph_search`, `graph_directory_lookup` y `graph_directory_manager`. El punto mas delicado es el GATE de
autorizacion: un 403 tiene que salir como `GraphForbiddenError` (para que se documente el limite) y NUNCA reintentarse con otro permiso.
Sin red ni Azure: `msal` y `requests` se reemplazan por falsos.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.connectors import graph_connector as gc


def resp(status=200, body=None, text=""):
    return SimpleNamespace(status_code=status, text=text, json=lambda: body if body is not None else {})


class FakeMsal:
    """Reemplaza `msal.ConfidentialClientApplication`: cuenta cuantas veces se pidio un token."""

    instances: list = []

    def __init__(self, client_id, client_credential, authority, proxies=None):
        self.args = SimpleNamespace(
            client_id=client_id, authority=authority, proxies=proxies, credential=client_credential
        )
        self.result = {"access_token": "TOKEN-1"}
        self.scopes_asked: list = []
        FakeMsal.instances.append(self)

    def acquire_token_for_client(self, scopes):
        self.scopes_asked.append(scopes)
        return FakeMsal.next_result if FakeMsal.next_result is not None else self.result

    next_result = None


class FakeHttp:
    """Reemplaza `requests.get/post`: registra cada llamada y responde lo que se le indique."""

    def __init__(self, reply=None):
        self.reply = reply or resp(body={})
        self.calls: list = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.reply

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.reply


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    gc._token_cache.clear()
    FakeMsal.instances = []
    FakeMsal.next_result = None
    monkeypatch.setattr(gc.msal, "ConfidentialClientApplication", FakeMsal)
    yield
    gc._token_cache.clear()


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(gc.requests, "get", fake.get)
    monkeypatch.setattr(gc.requests, "post", fake.post)
    return fake


def make(**kw):
    cfg = Settings(azure_client_id="cid", azure_client_secret="secreto-falso", azure_tenant_id="tid", **kw)
    return gc.GraphConnector(cfg)


# ---------------------------------------------------------------------------
# Token de Azure AD
# ---------------------------------------------------------------------------
class TestToken:
    def test_it_asks_azure_for_the_graph_default_scope_and_returns_the_token(self):
        c = make()

        assert c._acquire_token() == "TOKEN-1"
        assert FakeMsal.instances[0].scopes_asked == [["https://graph.microsoft.com/.default"]]
        assert FakeMsal.instances[0].args.client_id == "cid"

    def test_the_same_instance_asks_azure_only_once(self):
        c = make()
        c._acquire_token()
        c._acquire_token()

        assert len(FakeMsal.instances) == 1

    def test_a_new_connector_reuses_the_module_cache(self):
        make()._acquire_token()

        assert make()._acquire_token() == "TOKEN-1"
        assert len(FakeMsal.instances) == 1  # cada tool-call crea un conector nuevo: sin cache serian N pedidos a Azure

    def test_the_cache_expires_after_the_ttl(self):
        make()._acquire_token()
        key = next(iter(gc._token_cache))
        token, acquired = gc._token_cache[key]
        gc._token_cache[key] = (token, acquired - gc._TOKEN_TTL_SECONDS - 1)
        FakeMsal.next_result = {"access_token": "TOKEN-2"}

        assert make()._acquire_token() == "TOKEN-2"
        assert len(FakeMsal.instances) == 2

    def test_the_cache_is_per_app_and_tenant(self):
        make()._acquire_token()
        other = gc.GraphConnector(Settings(azure_client_id="otra", azure_client_secret="x", azure_tenant_id="tid"))
        FakeMsal.next_result = {"access_token": "TOKEN-OTRA"}

        assert other._acquire_token() == "TOKEN-OTRA"

    def test_the_same_app_in_another_tenant_does_not_share_the_token(self):
        make()._acquire_token()
        other_tenant = gc.GraphConnector(Settings(azure_client_id="cid", azure_client_secret="x", azure_tenant_id="OTRO-TENANT"))
        FakeMsal.next_result = {"access_token": "TOKEN-OTRO-TENANT"}

        assert other_tenant._acquire_token() == "TOKEN-OTRO-TENANT"

    def test_the_instance_keeps_its_token_even_if_the_module_cache_is_cleared(self):
        """Dentro de una misma instancia el token no se vuelve a pedir ni aunque el cache de modulo se vacie (varias llamadas seguidas)."""
        c = make()
        c._acquire_token()
        gc._token_cache.clear()
        FakeMsal.next_result = {"access_token": "OTRO"}

        assert c._acquire_token() == "TOKEN-1"
        assert len(FakeMsal.instances) == 1

    def test_an_azure_error_is_reported_with_its_description(self):
        FakeMsal.next_result = {"error": "invalid_client", "error_description": "AADSTS7000215: secreto invalido"}

        with pytest.raises(gc.GraphConnectionError, match="Fallo autenticacion Azure AD: AADSTS7000215"):
            make()._acquire_token()

    def test_without_a_description_the_error_code_is_used(self):
        FakeMsal.next_result = {"error": "invalid_client"}

        with pytest.raises(gc.GraphConnectionError, match="invalid_client"):
            make()._acquire_token()

    def test_a_failed_login_is_not_cached(self):
        FakeMsal.next_result = {"error": "x"}
        with pytest.raises(gc.GraphConnectionError):
            make()._acquire_token()
        FakeMsal.next_result = None

        assert make()._acquire_token() == "TOKEN-1"

    def test_the_error_never_contains_the_client_secret(self):
        FakeMsal.next_result = {"error": "invalid_client", "error_description": "fallo"}

        with pytest.raises(gc.GraphConnectionError) as err:
            make()._acquire_token()
        assert "secreto-falso" not in str(err.value)

    def test_test_connection_reports_true_and_false_without_raising(self):
        assert make().test_connection() is True
        gc._token_cache.clear()
        FakeMsal.next_result = {"error": "x"}
        assert make().test_connection() is False


# ---------------------------------------------------------------------------
# Directorio: usuario y manager
# ---------------------------------------------------------------------------
class TestDirectory:
    def test_lookup_returns_the_user_and_sends_the_bearer_token(self, http):
        http.reply = resp(body={"displayName": "Jorge", "mail": "j@empresa.com"})

        user = make().directory_lookup("usuario1@empresa.com")

        assert user["displayName"] == "Jorge"
        method, url, kw = http.calls[0]
        assert method == "GET" and "/users/usuario1@empresa.com?$select=" in url
        assert kw["headers"] == {"Authorization": "Bearer TOKEN-1"} and kw["timeout"] == 20

    def test_lookup_403_is_a_forbidden_error_that_names_the_missing_permission(self, http):
        http.reply = resp(status=403)

        with pytest.raises(gc.GraphForbiddenError, match="User.Read.All.*Directory.Read.All"):
            make().directory_lookup("x")

    def test_a_forbidden_error_is_also_a_connection_error_and_is_never_retried(self, http):
        http.reply = resp(status=403)

        with pytest.raises(gc.GraphConnectionError):  # GraphForbiddenError hereda de GraphConnectionError
            make().directory_lookup("x")
        assert len(http.calls) == 1  # el gate de autorizacion: NO se reintenta con otro scope ni otro permiso
        assert len(FakeMsal.instances) == 1

    def test_lookup_404_says_the_user_was_not_found(self, http):
        http.reply = resp(status=404)

        with pytest.raises(gc.GraphConnectionError, match="No se encontro un usuario con identificador 'ghost'"):
            make().directory_lookup("ghost")

    def test_lookup_other_errors_include_the_status_and_a_trimmed_body(self, http):
        http.reply = resp(status=500, text="boom " + "x" * 600)

        with pytest.raises(gc.GraphConnectionError, match=r"directorio \(500\): boom") as err:
            make().directory_lookup("x")
        assert len(str(err.value)) < 400

    def test_manager_returns_the_manager(self, http):
        http.reply = resp(body={"displayName": "Jefe"})

        assert make().directory_manager("usuario1")["displayName"] == "Jefe"
        assert "/users/usuario1/manager?$select=" in http.calls[0][1]

    def test_manager_403_is_forbidden(self, http):
        http.reply = resp(status=403)

        with pytest.raises(gc.GraphForbiddenError):
            make().directory_manager("x")

    def test_manager_404_says_there_is_no_manager(self, http):
        http.reply = resp(status=404)

        with pytest.raises(gc.GraphConnectionError, match="No se encontro manager para 'x'"):
            make().directory_manager("x")

    def test_manager_other_errors_include_the_status(self, http):
        http.reply = resp(status=502, text="bad gateway")

        with pytest.raises(gc.GraphConnectionError, match=r"manager \(502\): bad gateway"):
            make().directory_manager("x")


# ---------------------------------------------------------------------------
# Microsoft Search
# ---------------------------------------------------------------------------
def search_body(*hits):
    return {"value": [{"hitsContainers": [{"hits": list(hits)}]}]}


def hit(name="Plan.xlsx", url="https://x/plan", summary="<c0>pieza</c0> <ddd/> clave", **resource):
    return {"resource": {"name": name, "webUrl": url, **resource}, "summary": summary}


class TestSearch:
    def test_it_posts_the_query_with_region_nam_and_returns_the_hits(self, http):
        http.reply = resp(
            body=search_body(
                hit(
                    createdBy={"user": {"displayName": "Ana"}},
                    lastModifiedBy={"user": {"displayName": "Beto"}},
                    lastModifiedDateTime="2026-09-01",
                )
            )
        )

        rows = make().search("slotting")

        method, url, kw = http.calls[0]
        assert method == "POST" and url.endswith("/search/query") and kw["timeout"] == 90
        request = kw["json"]["requests"][0]
        assert request["query"] == {"queryString": "slotting"}
        assert request["region"] == "NAM"  # obligatorio con autenticacion app-only
        assert request["entityTypes"] == ["driveItem", "listItem"]
        assert (
            kw["headers"]["Content-Type"] == "application/json" and kw["headers"]["Authorization"] == "Bearer TOKEN-1"
        )
        assert rows == [
            {
                "name": "Plan.xlsx",
                "url": "https://x/plan",
                "modified": "2026-09-01",
                "createdBy": "Ana",
                "modifiedBy": "Beto",
                "summary": "pieza clave",
            }
        ]

    @pytest.mark.parametrize(("asked", "sent"), [(1, 1), (25, 25), (100, 100), (500, 100), (0, 1), (-5, 1)])
    def test_the_page_size_is_clamped_between_1_and_100(self, http, asked, sent):
        http.reply = resp(body=search_body())

        make().search("q", size=asked)

        assert http.calls[0][2]["json"]["requests"][0]["size"] == sent

    def test_the_summary_markers_are_removed_and_spaces_collapsed(self, http):
        http.reply = resp(body=search_body(hit(summary="  <c0>uno</c0>   <ddd/>\n dos  ")))

        assert make().search("q")[0]["summary"] == "uno dos"

    def test_the_modified_date_falls_back_to_the_file_system_info(self, http):
        http.reply = resp(body=search_body(hit(fileSystemInfo={"lastModifiedDateTime": "2026-08-01"})))

        assert make().search("q")[0]["modified"] == "2026-08-01"

    def test_a_hit_without_authors_or_summary_does_not_crash(self, http):
        http.reply = resp(body=search_body({"resource": {"name": "x"}}))

        row = make().search("q")[0]
        assert row["createdBy"] is None and row["modifiedBy"] is None and row["summary"] == ""

    def test_a_hit_without_resource_gives_an_all_empty_row(self, http):
        http.reply = resp(body=search_body({}))

        assert make().search("q") == [
            {"name": None, "url": None, "modified": None, "createdBy": None, "modifiedBy": None, "summary": ""}
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"value": []},
            {"value": [{}]},
            {"value": [{"hitsContainers": []}]},
            {"value": [{"hitsContainers": [{"hits": []}]}]},
        ],
    )
    def test_no_results_in_any_shape_gives_an_empty_list(self, http, body):
        http.reply = resp(body=body)

        assert make().search("q") == []

    def test_hits_from_several_containers_are_all_returned(self, http):
        body = {"value": [{"hitsContainers": [{"hits": [hit("a")]}, {"hits": [hit("b"), hit("c")]}]}]}
        http.reply = resp(body=body)

        assert [r["name"] for r in make().search("q")] == ["a", "b", "c"]

    def test_search_403_is_forbidden_and_not_retried(self, http):
        http.reply = resp(status=403)

        with pytest.raises(gc.GraphForbiddenError, match="permiso insuficiente"):
            make().search("q")
        assert len(http.calls) == 1

    def test_search_other_errors_include_the_status_and_a_trimmed_body(self, http):
        http.reply = resp(status=429, text="throttled " + "x" * 800)

        with pytest.raises(gc.GraphConnectionError, match=r"Graph Search \(429\): throttled") as err:
            make().search("q")
        assert len(str(err.value)) < 500

    def test_the_corporate_proxy_reaches_azure_and_every_graph_call(self, http):
        """Detras de la VPN de tu empresa sin proxy no sale nada. Debe llegar a TODAS las llamadas (token, usuario, manager y busqueda)."""
        proxy = {"http": "http://proxy.empresa.test:83", "https": "http://proxy.empresa.test:83"}
        c = make(https_proxy="http://proxy.empresa.test:83")

        c.search("q")
        c.directory_lookup("x")
        c.directory_manager("x")

        assert FakeMsal.instances[0].args.proxies == proxy
        assert [kw["proxies"] for _, _, kw in http.calls] == [proxy, proxy, proxy]

    def test_without_a_proxy_none_is_sent(self, http):
        # Se fija vacio a proposito: `Settings` lee HTTPS_PROXY del entorno y otros tests (test_proxy_empresa) lo dejan seteado en el proceso.
        make(https_proxy="").search("q")

        assert http.calls[0][2]["proxies"] is None and FakeMsal.instances[0].args.proxies is None


def test_clean_summary_handles_none_and_empty():
    assert gc._clean_summary("") == "" and gc._clean_summary(None) == ""
