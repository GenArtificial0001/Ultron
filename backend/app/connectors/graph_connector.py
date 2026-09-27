"""
Conector a Microsoft Graph (Directory + Search), usado por la skill
investigacion-interna-graph para resolver identidad corporativa y
descubrir documentos/listItems relevantes sobre una persona/proyecto.

Reusa la misma app registrada de Azure AD que SharePointConnector
(AZURE_CLIENT_ID/SECRET/TENANT_ID) -- mismo scope de Graph
(`https://graph.microsoft.com/.default`), asi que no hace falta credenciales
nuevas: si el conector de SharePoint ya funciona, este tambien.

Gates de autorizacion (ver SKILL.md de investigacion-interna-graph):
    - directory_lookup() / directory_manager(): pueden fallar con 403 si la
      app no tiene el permiso `User.Read.All`/`Directory.Read.All` otorgado
      por un admin -- en ese caso se documenta el limite (NUNCA se reintenta
      con otro scope ni se sugiere "pedir mas permisos" automaticamente).
    - search(): usa `POST /search/query` (Microsoft Search), que solo
      indexa contenido M365 al que la propia app ya tiene acceso via Graph
      -- no expone nada que la cuenta de servicio no pueda ver de por si.
"""

from __future__ import annotations

import logging
import re
import time

import msal
import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

_SUMMARY_CLEAN_RE = re.compile(r"</?c0>|<ddd/>")
_WHITESPACE_RE = re.compile(r"\s+")

# Investigacion de rendimiento 2026-09: mismo fix que
# connectors/sharepoint_connector.py -- `tools/dbquery.py` crea un
# `GraphConnector(settings)` nuevo por cada graph_search/directory_lookup/
# directory_manager, asi que el cache de `self._token` se perdia siempre.
# Cache de MODULO (propio de este conector, no compartido con
# SharePointConnector aunque use el mismo scope -- mas simple, sin acoplar
# los dos archivos).
_TOKEN_TTL_SECONDS = 3000  # 50 min
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


class GraphConnectionError(Exception):
    """Se lanza cuando falla la autenticacion o una llamada a Microsoft Graph."""


class GraphForbiddenError(GraphConnectionError):
    """Se lanza especificamente ante un 403 -- la app no tiene el permiso
    necesario (ej. Directory.Read.All). Se distingue del error generico
    para poder informarle al modelo/usuario el limite real en vez de un
    error opaco, siguiendo el gate de autorizacion de la skill."""


def _clean_summary(value: str) -> str:
    value = _SUMMARY_CLEAN_RE.sub(" ", value or "")
    return _WHITESPACE_RE.sub(" ", value).strip()


class GraphConnector:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._token: str | None = None

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
            raise GraphConnectionError(f"Fallo autenticacion Azure AD: {error}")
        self._token = result["access_token"]
        _token_cache[cache_key] = (self._token, time.monotonic())
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._acquire_token()}"}

    def _proxies(self) -> dict | None:
        return self._settings.requests_proxies

    def test_connection(self) -> bool:
        try:
            self._acquire_token()
            return True
        except Exception as exc:
            logger.warning("Graph no disponible: %s", exc)
            return False

    # -- Directory (identidad corporativa) ---------------------------------
    def directory_lookup(self, upn_or_id: str) -> dict:
        """Busca un usuario por UPN/correo/id. Puede fallar con 403 si la
        app no tiene `User.Read.All`/`Directory.Read.All` otorgado -- eso
        se propaga como GraphForbiddenError para que quien la use (tool o
        skill) documente el limite en vez de inventar datos."""
        endpoint = (
            f"{GRAPH_BASE_URL}/users/{upn_or_id}"
            "?$select=id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation,accountEnabled"
        )
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=20, proxies=self._proxies()
        )
        if resp.status_code == 403:
            raise GraphForbiddenError(
                "Graph respondio 403 (Authorization_RequestDenied) al consultar el directorio. "
                "La aplicacion no tiene el permiso 'User.Read.All'/'Directory.Read.All' otorgado "
                "por un administrador. No se puede resolver identidad via Directory -- usar "
                "Microsoft Search (tool graph_search) o fuentes documentales como alternativa."
            )
        if resp.status_code == 404:
            raise GraphConnectionError(
                f"No se encontro un usuario con identificador '{upn_or_id}'."
            )
        if resp.status_code != 200:
            raise GraphConnectionError(
                f"Error consultando directorio ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    def directory_manager(self, upn_or_id: str) -> dict:
        """Devuelve el manager directo de un usuario (mismo gate 403 que
        directory_lookup())."""
        endpoint = (
            f"{GRAPH_BASE_URL}/users/{upn_or_id}/manager"
            "?$select=id,displayName,mail,userPrincipalName,jobTitle,department"
        )
        resp = requests.get(
            endpoint, headers=self._headers(), timeout=20, proxies=self._proxies()
        )
        if resp.status_code == 403:
            raise GraphForbiddenError(
                "Graph respondio 403 al consultar el manager. Permiso de Directory no otorgado."
            )
        if resp.status_code == 404:
            raise GraphConnectionError(
                f"No se encontro manager para '{upn_or_id}' (o no tiene asignado)."
            )
        if resp.status_code != 200:
            raise GraphConnectionError(
                f"Error consultando manager ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    # -- Microsoft Search (descubrimiento de documentos/listItems) ----------
    def search(self, query: str, size: int = 25) -> list[dict]:
        """Busqueda de texto completo sobre contenido M365 indexado
        (driveItem/listItem) al que la app ya tiene acceso. `region: NAM`
        es obligatorio con autenticacion app-only (ver graph-directory-search.md)."""
        endpoint = f"{GRAPH_BASE_URL}/search/query"
        body = {
            "requests": [
                {
                    "entityTypes": ["driveItem", "listItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": max(1, min(size, 100)),
                    "region": "NAM",
                }
            ]
        }
        resp = requests.post(
            endpoint,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=90,
            proxies=self._proxies(),
        )
        if resp.status_code == 403:
            raise GraphForbiddenError(
                "Graph Search respondio 403 -- permiso insuficiente para la app."
            )
        if resp.status_code != 200:
            raise GraphConnectionError(
                f"Error en Graph Search ({resp.status_code}): {resp.text[:400]}"
            )

        results: list[dict] = []
        data = resp.json().get("value", [{}])
        root = data[0] if data else {}
        for container in root.get("hitsContainers", []):
            for hit in container.get("hits", []):
                resource = hit.get("resource") or {}
                created_by = (resource.get("createdBy") or {}).get("user") or {}
                modified_by = (resource.get("lastModifiedBy") or {}).get("user") or {}
                results.append(
                    {
                        "name": resource.get("name"),
                        "url": resource.get("webUrl"),
                        "modified": resource.get("lastModifiedDateTime")
                        or (resource.get("fileSystemInfo") or {}).get(
                            "lastModifiedDateTime"
                        ),
                        "createdBy": created_by.get("displayName"),
                        "modifiedBy": modified_by.get("displayName"),
                        "summary": _clean_summary(hit.get("summary") or ""),
                    }
                )
        return results
