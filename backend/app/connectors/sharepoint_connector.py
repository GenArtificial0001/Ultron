"""
Conector propio de Vision a SharePoint Online via Microsoft Graph API
(MSAL client credentials). Version reducida del conector de DBManager
(app/connectors/sharepoint_connector.py): solo las operaciones de LECTURA
mas utiles para un asistente conversacional (listar listas, leer items,
listar bibliotecas de documentos y archivos) -- no incluye CRUD de
escritura en listas ni upload de archivos en esta v1.

Variables de entorno usadas:
    SHAREPOINT_SITE_URL, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    AZURE_TENANT_ID, SHAREPOINT_DEFAULT_LIST_NAME, HTTPS_PROXY
"""

from __future__ import annotations

import logging
import time

import msal
import pandas as pd
import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): antes el
# cache de token/site_id vivia solo en `self` -- pero `tools/dbquery.py`
# crea un `SharePointConnector(settings)` NUEVO por cada tool-call, asi que
# ese cache se perdia siempre: cada `sharepoint_list_items`/
# `sharepoint_list_lists` repetia el OAuth completo (MSAL) + la resolucion
# de site_id via Graph antes de hacer la consulta real. Cache a nivel de
# MODULO, sobrevive entre instancias/llamadas (mismo criterio que
# `llm/client.py` para el cliente del proveedor de IA). TTL conservador: un token de
# client-credentials de Entra ID suele durar ~60-90 min: se renueva antes
# de esa ventana para no arriesgar un 401 a mitad de una consulta larga.
_TOKEN_TTL_SECONDS = 3000  # 50 min
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}  # (client_id, tenant) -> (token, acquired_at)
_site_id_cache: dict[str, str] = {}  # sharepoint_site_url -> site_id


class SharePointConnectionError(Exception):
    """Se lanza cuando no se puede autenticar o alcanzar SharePoint/Graph."""


class SharePointConnector:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._token: str | None = None
        self._site_id: str | None = None

    def _acquire_token(self) -> str:
        if self._token:
            return self._token
        cache_key = (self._settings.azure_client_id, self._settings.azure_tenant_id)
        cached = _token_cache.get(cache_key)
        if cached is not None:
            token, acquired_at = cached
            if (time.monotonic() - acquired_at) < _TOKEN_TTL_SECONDS:
                self._token = token
                return token
        app = msal.ConfidentialClientApplication(
            client_id=self._settings.azure_client_id,
            client_credential=self._settings.azure_client_secret,
            authority=self._settings.azure_authority,
            proxies=self._settings.requests_proxies,
        )
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "desconocido"))
            raise SharePointConnectionError(f"Fallo autenticacion Azure AD: {error}")
        self._token = result["access_token"]
        _token_cache[cache_key] = (self._token, time.monotonic())
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._acquire_token()}"}

    def _proxies(self) -> dict | None:
        return self._settings.requests_proxies

    def _resolve_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        cached_site_id = _site_id_cache.get(self._settings.sharepoint_site_url)
        if cached_site_id is not None:
            self._site_id = cached_site_id
            return cached_site_id
        url = self._settings.sharepoint_site_url
        hostname = url.split("//")[1].split("/")[0]
        site_path = "/" + "/".join(url.split("//")[1].split("/")[1:])
        endpoint = f"{GRAPH_BASE_URL}/sites/{hostname}:{site_path}"
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=15, proxies=self._proxies()
        )
        if resp.status_code != 200:
            raise SharePointConnectionError(
                f"Error resolviendo sitio ({resp.status_code}): {resp.text[:300]}"
            )
        self._site_id = resp.json()["id"]
        _site_id_cache[self._settings.sharepoint_site_url] = self._site_id
        return self._site_id

    def _get_list_id(self, list_display_name: str) -> str:
        site_id = self._resolve_site_id()
        endpoint = f"{GRAPH_BASE_URL}/sites/{site_id}/lists"
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=15, proxies=self._proxies()
        )
        if resp.status_code != 200:
            raise SharePointConnectionError(
                f"Error listando listas ({resp.status_code}): {resp.text[:300]}"
            )
        for lst in resp.json().get("value", []):
            if (
                lst.get("displayName", "").strip().upper()
                == list_display_name.strip().upper()
            ):
                return lst["id"]
        raise SharePointConnectionError(
            f"No se encontro la lista '{list_display_name}'"
        )

    def test_connection(self) -> bool:
        try:
            self._acquire_token()
            self._resolve_site_id()
            return True
        except Exception as exc:
            logger.warning("SharePoint no disponible: %s", exc)
            return False

    def list_lists(self) -> list[str]:
        # Mismo bug de fondo que `fetch_list_items` (ver comentario largo ahi):
        # un sitio con mas listas que una sola pagina de Graph quedaba
        # truncado porque no se seguia `@odata.nextLink`. Se pagina completo
        # por las mismas razones (consistencia de criterio en todo el
        # conector), aunque en la practica un sitio rara vez supera el
        # tamanio de pagina default en cantidad de LISTAS (si en items).
        site_id = self._resolve_site_id()
        endpoint = f"{GRAPH_BASE_URL}/sites/{site_id}/lists"
        nombres: list[str] = []
        while endpoint:
            resp = requests.get(
                endpoint, headers=self._headers(), timeout=15, proxies=self._proxies()
            )
            if resp.status_code != 200:
                raise SharePointConnectionError(
                    f"Error listando listas ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            nombres.extend(lst.get("displayName", "") for lst in data.get("value", []))
            endpoint = data.get("@odata.nextLink")
        return nombres

    # -- Bibliotecas de documentos (drives) y navegacion de carpetas -------
    # Usado por el explorador de "Configuracion > Sitio SharePoint" del
    # frontend, para que el usuario pueda seleccionar visualmente una
    # biblioteca (ej. Digital, Documents, FPS, Ambiental) y una carpeta
    # dentro de ella como contexto de proyecto -- igual espiritu que elegir
    # una carpeta local, pero para contenido que vive en el sitio.
    def list_drives(self) -> list[dict]:
        """Lista las bibliotecas de documentos (drives) del sitio configurado."""
        site_id = self._resolve_site_id()
        endpoint = f"{GRAPH_BASE_URL}/sites/{site_id}/drives"
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=15, proxies=self._proxies()
        )
        if resp.status_code != 200:
            raise SharePointConnectionError(
                f"Error listando bibliotecas ({resp.status_code}): {resp.text[:300]}"
            )
        return [
            {"id": d.get("id"), "name": d.get("name"), "webUrl": d.get("webUrl")}
            for d in resp.json().get("value", [])
        ]

    def list_folder_children(
        self, drive_id: str, item_id: str | None = None
    ) -> list[dict]:
        """Lista el contenido (carpetas y archivos) de una carpeta dentro de
        una biblioteca. `item_id=None` lista la raiz del drive."""
        base = f"{GRAPH_BASE_URL}/drives/{drive_id}"
        endpoint = (
            f"{base}/root/children"
            if not item_id
            else f"{base}/items/{item_id}/children"
        )
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=20, proxies=self._proxies()
        )
        if resp.status_code != 200:
            raise SharePointConnectionError(
                f"Error listando carpeta ({resp.status_code}): {resp.text[:300]}"
            )
        items = []
        for it in resp.json().get("value", []):
            items.append(
                {
                    "id": it.get("id"),
                    "name": it.get("name"),
                    "is_folder": "folder" in it,
                    "child_count": (it.get("folder") or {}).get("childCount"),
                    "webUrl": it.get("webUrl"),
                    "size": it.get("size"),
                }
            )
        # Carpetas primero, alfabetico dentro de cada grupo
        items.sort(key=lambda x: (not x["is_folder"], x["name"].lower()))
        return items

    def get_item_path(self, drive_id: str, item_id: str) -> str:
        """Devuelve la ruta legible (ej. 'Material Flow & IE/Slotting') de un
        item dentro del drive, usada para mostrar 'donde estoy parado' en el
        breadcrumb del explorador."""
        endpoint = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}?$select=parentReference,name"
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=15, proxies=self._proxies()
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        parent_path = (data.get("parentReference") or {}).get("path", "")
        # parentReference.path viene como '/drives/{id}/root:/Carpeta/Subcarpeta'
        clean = parent_path.split("root:", 1)[-1].lstrip("/")
        return f"{clean}/{data.get('name', '')}".strip("/")

    def fetch_list_items(
        self,
        list_display_name: str | None = None,
        page_size: int = 200,
        max_items: int | None = None,
    ) -> pd.DataFrame:
        """Lee los items de una lista de SharePoint, recorriendo TODAS las
        paginas via `@odata.nextLink` hasta agotar la lista real.

        Bug real (2026-10-01, reportado por Jorge Gamez: el tracker ZCC
        mostraba items 249-354 en la UI de SharePoint pero Vision solo veia
        hasta el id 213, siempre el MISMO bloque viejo en cada consulta):
        antes, el parametro `top` se usaba a la vez como (a) tamanio de
        pagina para el `$top` de la URL de Graph y (b) tope TOTAL de filas
        via `if len(registros) >= top: break` -- con el default `top=200` y
        sin que `dbquery.py` pasara nada distinto, toda lista con mas de 200
        items quedaba cortada SIEMPRE en las primeras 200 (ordenadas por id
        ascendente, las mas viejas), sin importar cuantas paginas mas hubiera
        disponibles via `@odata.nextLink`.

        Fix: separar los dos conceptos.
        - `page_size` (antes `top`): SOLO controla cuantos items trae Graph
          por request HTTP (eficiencia de red/memoria por pagina). No limita
          el total.
        - `max_items` (nuevo, opcional): tope TOTAL real, si el que llama
          quiere explicitamente acotar el resultado (ej. "dame los primeros
          50"). Por defecto `None` = sin tope, se recorren TODAS las paginas
          hasta que Graph deje de devolver `@odata.nextLink`.
        """
        list_name = list_display_name or self._settings.sharepoint_default_list_name
        if not list_name:
            raise ValueError(
                "Debes indicar list_display_name o configurar SHAREPOINT_DEFAULT_LIST_NAME"
            )

        # Resuelve el sitio y la lista una sola vez (cacheados a nivel de
        # instancia/modulo, ver _resolve_site_id/_get_list_id mas arriba).
        site_id = self._resolve_site_id()
        list_id = self._get_list_id(list_name)
        # `$top` aca es SOLO el tamanio de cada pagina HTTP, no un limite total.
        endpoint = (
            f"{GRAPH_BASE_URL}/sites/{site_id}/lists/{list_id}/items"
            f"?expand=fields&$top={page_size}"
        )

        registros: list[dict] = []
        # Recorre paginas hasta que Graph no devuelva mas `@odata.nextLink`
        # (fin real de la lista) o se alcance `max_items` si el que llama
        # pidio explicitamente un tope.
        while endpoint:
            resp = requests.get(
                endpoint, headers=self._headers(), timeout=30, proxies=self._proxies()
            )
            if resp.status_code != 200:
                raise SharePointConnectionError(
                    f"Error leyendo items ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            for item in data.get("value", []):
                registros.append(item.get("fields", {}))
            # `@odata.nextLink` es la URL completa (con el skip-token) de la
            # proxima pagina; viene `None`/ausente cuando ya no hay mas filas.
            endpoint = data.get("@odata.nextLink")
            if max_items is not None and len(registros) >= max_items:
                registros = registros[:max_items]
                break

        return pd.DataFrame(registros)
