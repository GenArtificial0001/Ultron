"""Conector de SharePoint (`app/connectors/sharepoint_connector.py`, BL-006): token de Azure AD con cache, resolucion del sitio, busqueda de la lista,
bibliotecas de documentos, carpetas y ruta de un item.

Estaba en 42,4 %. `test_sharepoint_pagination.py` ya cubre la paginacion de items pero DESACTIVA a proposito el token, el sitio y la lista (los reemplaza por
falsos), asi que justo eso no tenia ninguna prueba. Sin red ni Azure: `msal` y `requests` se reemplazan por falsos.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.connectors import sharepoint_connector as spc

SITE_URL = "https://empresa.sharepoint.com/sites/Equipo"


def resp(status=200, body=None, text=""):
    return SimpleNamespace(status_code=status, text=text, json=lambda: body if body is not None else {})


class FakeMsal:
    instances: list = []
    next_result = None

    def __init__(self, client_id, client_credential, authority, proxies=None):
        self.args = SimpleNamespace(client_id=client_id, authority=authority, proxies=proxies)
        self.scopes_asked: list = []
        FakeMsal.instances.append(self)

    def acquire_token_for_client(self, scopes):
        self.scopes_asked.append(scopes)
        return FakeMsal.next_result if FakeMsal.next_result is not None else {"access_token": "TOKEN-1"}


class FakeGraph:
    """Reemplaza `requests.get`: una respuesta por fragmento de URL (el primero que aparezca en la URL pedida gana)."""

    def __init__(self):
        self.routes: list[tuple[str, SimpleNamespace]] = []
        self.calls: list[tuple[str, dict]] = []

    def on(self, fragment, reply):
        self.routes.append((fragment, reply))
        return self

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for fragment, reply in self.routes:
            if fragment in url:
                return reply
        return resp(404, text="sin ruta en el falso: " + url)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    spc._token_cache.clear()
    spc._site_id_cache.clear()
    FakeMsal.instances = []
    FakeMsal.next_result = None
    monkeypatch.setattr(spc.msal, "ConfidentialClientApplication", FakeMsal)
    yield
    spc._token_cache.clear()
    spc._site_id_cache.clear()


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(spc.requests, "get", g.get)
    return g


def make(**kw):
    cfg = Settings(
        azure_client_id="cid",
        azure_client_secret="secreto-falso",
        azure_tenant_id="tid",
        sharepoint_site_url=SITE_URL,
        **kw,
    )
    return spc.SharePointConnector(cfg)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------
class TestToken:
    def test_it_asks_azure_for_the_graph_scope(self):
        assert make()._acquire_token() == "TOKEN-1"
        assert FakeMsal.instances[0].scopes_asked == [["https://graph.microsoft.com/.default"]]

    def test_the_same_instance_asks_azure_once(self):
        c = make()
        c._acquire_token()
        c._acquire_token()

        assert len(FakeMsal.instances) == 1

    def test_a_new_connector_reuses_the_module_cache(self):
        make()._acquire_token()
        make()._acquire_token()

        assert len(FakeMsal.instances) == 1  # cada tool-call crea un conector nuevo

    def test_the_instance_keeps_its_token_even_if_the_module_cache_is_cleared(self):
        c = make()
        c._acquire_token()
        spc._token_cache.clear()
        FakeMsal.next_result = {"access_token": "OTRO"}

        assert c._acquire_token() == "TOKEN-1"
        assert len(FakeMsal.instances) == 1

    def test_the_cache_expires_after_the_ttl(self):
        make()._acquire_token()
        key = next(iter(spc._token_cache))
        token, acquired = spc._token_cache[key]
        spc._token_cache[key] = (token, acquired - spc._TOKEN_TTL_SECONDS - 1)

        make()._acquire_token()

        assert len(FakeMsal.instances) == 2

    def test_the_cache_is_per_app_and_per_tenant(self):
        make()._acquire_token()
        spc.SharePointConnector(
            Settings(azure_client_id="otra", azure_client_secret="x", azure_tenant_id="tid")
        )._acquire_token()
        spc.SharePointConnector(
            Settings(azure_client_id="cid", azure_client_secret="x", azure_tenant_id="otro")
        )._acquire_token()

        assert len(FakeMsal.instances) == 3

    def test_an_azure_error_uses_its_description_or_the_code(self):
        FakeMsal.next_result = {"error": "invalid_client", "error_description": "AADSTS7000215: secreto invalido"}
        with pytest.raises(spc.SharePointConnectionError, match="Fallo autenticacion Azure AD: AADSTS7000215"):
            make()._acquire_token()

        FakeMsal.next_result = {"error": "invalid_client"}
        with pytest.raises(spc.SharePointConnectionError, match="invalid_client"):
            make()._acquire_token()

    def test_a_failed_login_is_not_cached_and_the_error_hides_the_secret(self):
        FakeMsal.next_result = {"error": "x", "error_description": "fallo"}
        with pytest.raises(spc.SharePointConnectionError) as err:
            make()._acquire_token()
        assert "secreto-falso" not in str(err.value)
        FakeMsal.next_result = None

        assert make()._acquire_token() == "TOKEN-1"

    def test_the_proxy_reaches_azure(self):
        make(https_proxy="http://proxy.empresa.test:83")._acquire_token()

        assert FakeMsal.instances[0].args.proxies == {
            "http": "http://proxy.empresa.test:83",
            "https": "http://proxy.empresa.test:83",
        }

    def test_headers_carry_the_bearer_token(self):
        assert make()._headers() == {"Authorization": "Bearer TOKEN-1"}


# ---------------------------------------------------------------------------
# Resolucion del sitio
# ---------------------------------------------------------------------------
class TestSite:
    def test_it_builds_the_graph_url_from_host_and_path_and_returns_the_id(self, graph):
        graph.on("/sites/", resp(body={"id": "SITE-ID"}))

        assert make()._resolve_site_id() == "SITE-ID"
        url, kw = graph.calls[0]
        assert url == "https://graph.microsoft.com/v1.0/sites/empresa.sharepoint.com:/sites/Equipo"
        assert kw["headers"] == {"Authorization": "Bearer TOKEN-1"} and kw["timeout"] == 15

    def test_the_site_id_is_cached_per_instance_and_per_module(self, graph):
        graph.on("/sites/", resp(body={"id": "SITE-ID"}))
        first = make()
        first._resolve_site_id()
        first._resolve_site_id()
        make()._resolve_site_id()

        assert len(graph.calls) == 1  # una sola consulta a Graph para las tres llamadas

    def test_the_instance_keeps_its_site_id_even_if_the_module_cache_is_cleared(self, graph):
        graph.on("/sites/", resp(body={"id": "SITE-ID"}))
        c = make()
        c._resolve_site_id()
        spc._site_id_cache.clear()

        assert c._resolve_site_id() == "SITE-ID"
        assert len(graph.calls) == 1

    def test_the_cache_is_per_site_url(self, graph):
        graph.on("/sites/", resp(body={"id": "SITE-ID"}))
        make()._resolve_site_id()
        other = spc.SharePointConnector(
            Settings(
                azure_client_id="cid",
                azure_client_secret="x",
                azure_tenant_id="tid",
                sharepoint_site_url="https://empresa.sharepoint.com/sites/Otro",
            )
        )
        other._resolve_site_id()

        assert len(graph.calls) == 2

    def test_a_site_error_includes_the_status_and_a_trimmed_body(self, graph):
        graph.on("/sites/", resp(404, text="no existe " + "x" * 600))

        with pytest.raises(spc.SharePointConnectionError, match=r"resolviendo sitio \(404\): no existe") as err:
            make()._resolve_site_id()
        assert len(str(err.value)) < 400
        assert spc._site_id_cache == {}  # un error no se cachea


# ---------------------------------------------------------------------------
# Busqueda de la lista por nombre
# ---------------------------------------------------------------------------
class TestListId:
    def lists(self, graph, *names):
        graph.on("/sites/SITE/lists", resp(body={"value": [{"displayName": n, "id": f"id-{n}"} for n in names]}))
        graph.on("/sites/", resp(body={"id": "SITE"}))

    def test_it_finds_the_list_ignoring_case_and_surrounding_spaces(self, graph):
        self.lists(graph, "ZCC Tracker", "Otra")

        assert make()._get_list_id("  zcc tracker ") == "id-ZCC Tracker"

    def test_a_missing_list_names_the_list(self, graph):
        self.lists(graph, "Otra")

        with pytest.raises(spc.SharePointConnectionError, match="No se encontro la lista 'ZCC'"):
            make()._get_list_id("ZCC")

    def test_a_listing_error_includes_the_status(self, graph):
        graph.on("/sites/SITE/lists", resp(500, text="boom"))
        graph.on("/sites/", resp(body={"id": "SITE"}))

        with pytest.raises(spc.SharePointConnectionError, match=r"listando listas \(500\): boom"):
            make()._get_list_id("ZCC")

    def test_a_list_without_a_display_name_does_not_crash_the_search(self, graph):
        graph.on("/sites/SITE/lists", resp(body={"value": [{"id": "x"}, {"displayName": "ZCC", "id": "ok"}]}))
        graph.on("/sites/", resp(body={"id": "SITE"}))

        assert make()._get_list_id("ZCC") == "ok"


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------
class TestConnection:
    def test_true_when_token_and_site_resolve(self, graph):
        graph.on("/sites/", resp(body={"id": "SITE"}))

        assert make().test_connection() is True

    def test_false_instead_of_raising_when_azure_fails(self, graph):
        FakeMsal.next_result = {"error": "x"}

        assert make().test_connection() is False

    def test_false_instead_of_raising_when_the_site_fails(self, graph):
        graph.on("/sites/", resp(403, text="prohibido"))

        assert make().test_connection() is False


# ---------------------------------------------------------------------------
# Bibliotecas de documentos, carpetas y ruta
# ---------------------------------------------------------------------------
class TestDrives:
    def setup_site(self, graph):
        graph.routes.insert(0, ("/sites/empresa.sharepoint.com", resp(body={"id": "SITE"})))

    def test_list_drives_returns_id_name_and_url(self, graph):
        self.setup_site(graph)
        graph.on(
            "/sites/SITE/drives",
            resp(body={"value": [{"id": "d1", "name": "Digital", "webUrl": "https://x/d1", "extra": 1}]}),
        )

        assert make().list_drives() == [{"id": "d1", "name": "Digital", "webUrl": "https://x/d1"}]

    def test_list_drives_tolerates_missing_fields(self, graph):
        self.setup_site(graph)
        graph.on("/sites/SITE/drives", resp(body={"value": [{"id": "d1"}]}))

        assert make().list_drives() == [{"id": "d1", "name": None, "webUrl": None}]

    def test_list_drives_error_includes_the_status(self, graph):
        self.setup_site(graph)
        graph.on("/sites/SITE/drives", resp(500, text="boom"))

        with pytest.raises(spc.SharePointConnectionError, match=r"bibliotecas \(500\): boom"):
            make().list_drives()

    def test_the_root_of_a_drive_is_listed_when_there_is_no_item(self, graph):
        graph.on("/drives/D1/root/children", resp(body={"value": []}))

        make().list_folder_children("D1")

        assert graph.calls[0][0].endswith("/drives/D1/root/children") and graph.calls[0][1]["timeout"] == 20

    def test_a_folder_is_listed_by_item_id(self, graph):
        graph.on("/drives/D1/items/F1/children", resp(body={"value": []}))

        make().list_folder_children("D1", "F1")

        assert graph.calls[0][0].endswith("/drives/D1/items/F1/children")

    def test_children_come_folders_first_then_alphabetical_ignoring_case(self, graph):
        graph.on(
            "/children",
            resp(
                body={
                    "value": [
                        {"id": "1", "name": "zeta.xlsx", "size": 10, "webUrl": "u1"},
                        {"id": "2", "name": "Beta", "folder": {"childCount": 3}, "webUrl": "u2"},
                        {"id": "3", "name": "alfa.docx", "size": 5, "webUrl": "u3"},
                        {"id": "4", "name": "Alfa", "folder": {"childCount": 0}, "webUrl": "u4"},
                    ]
                }
            ),
        )

        items = make().list_folder_children("D1")

        assert [i["name"] for i in items] == ["Alfa", "Beta", "alfa.docx", "zeta.xlsx"]
        folder = items[1]
        assert folder == {"id": "2", "name": "Beta", "is_folder": True, "child_count": 3, "webUrl": "u2", "size": None}
        assert items[2]["is_folder"] is False and items[2]["child_count"] is None and items[2]["size"] == 5

    def test_the_order_ignores_upper_and_lower_case_inside_each_group(self, graph):
        """En ASCII toda mayuscula va antes que cualquier minuscula: sin `.lower()` 'Zeta' saldria antes que 'alfa'."""
        graph.on(
            "/children",
            resp(
                body={
                    "value": [
                        {"id": "1", "name": "alfa.txt"},
                        {"id": "2", "name": "Zeta.txt"},
                        {"id": "3", "name": "beta.txt"},
                        {"id": "4", "name": "zcarpeta", "folder": {}},
                        {"id": "5", "name": "Acarpeta", "folder": {}},
                    ]
                }
            ),
        )

        assert [i["name"] for i in make().list_folder_children("D1")] == ["Acarpeta", "zcarpeta", "alfa.txt", "beta.txt", "Zeta.txt"]

    def test_a_folder_listing_error_includes_the_status(self, graph):
        graph.on("/children", resp(403, text="prohibido"))

        with pytest.raises(spc.SharePointConnectionError, match=r"listando carpeta \(403\): prohibido"):
            make().list_folder_children("D1")

    def test_the_item_path_joins_the_parent_path_and_the_name(self, graph):
        graph.on(
            "/drives/D1/items/I1",
            resp(body={"name": "Slotting", "parentReference": {"path": "/drives/D1/root:/Material Flow & IE"}}),
        )

        assert make().get_item_path("D1", "I1") == "Material Flow & IE/Slotting"
        assert "$select=parentReference,name" in graph.calls[0][0]

    def test_an_item_at_the_root_has_just_its_name(self, graph):
        graph.on("/drives/D1/items/I1", resp(body={"name": "Carpeta", "parentReference": {"path": "/drives/D1/root:"}}))

        assert make().get_item_path("D1", "I1") == "Carpeta"

    def test_an_item_without_parent_info_has_just_its_name(self, graph):
        graph.on("/drives/D1/items/I1", resp(body={"name": "Solo"}))

        assert make().get_item_path("D1", "I1") == "Solo"

    def test_an_error_response_is_never_used_to_build_the_path_even_if_it_has_a_body(self, graph):
        """Un error con cuerpo (Graph a veces devuelve JSON en un 404/403) no puede terminar como una ruta inventada en el breadcrumb."""
        graph.on("/drives/D1/items/I1", resp(403, body={"name": "NO-ES-UNA-RUTA", "parentReference": {"path": "/drives/D1/root:/Ajena"}}))

        assert make().get_item_path("D1", "I1") == ""

    def test_a_path_error_gives_an_empty_string_instead_of_failing_the_breadcrumb(self, graph):
        graph.on("/drives/D1/items/I1", resp(404))

        assert make().get_item_path("D1", "I1") == ""


def test_the_proxy_from_settings_reaches_every_call(graph):
    graph.on("/sites/empresa.sharepoint.com", resp(body={"id": "SITE"}))
    graph.on("/sites/SITE/drives", resp(body={"value": []}))
    c = make(https_proxy="http://proxy.empresa.test:83")
    c.list_drives()

    proxy = {"http": "http://proxy.empresa.test:83", "https": "http://proxy.empresa.test:83"}
    assert [kw["proxies"] for _, kw in graph.calls] == [proxy, proxy]
