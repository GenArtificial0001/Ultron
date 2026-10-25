"""Conector de Vision a Confluence Cloud (empresa.atlassian.net/wiki): SOLO LECTURA (spec conexiones-con-nombre, Fase B, REQ-006/008/009/010).

Adaptado del prototipo `C:\\Proyectos\\Conexion Confluence` (verificado contra la API real). Decisiones del usuario que cambian el prototipo:
  * Sin exportar Word/PDF (Playwright es solo de desarrollo) y sin la especializacion MP&L: queda lo generico.
  * Una conexion de Confluence lleva una lista de SECCIONES (nombre + pagina raiz), ej. «MP&L Procedures».
  * El correo y el TOKEN son PERSONALES: salen del `.env` de cada usuario (`CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`), nunca de `connections.json`.

Seguridad (REQ-009 y revision skill-interna):
  * Solo se hace `GET`. No hay ningun metodo que escriba, y el unico punto de salida a la red (`_request`) rechaza cualquier verbo distinto de GET.
  * Anti-SSRF: toda URL se valida contra el host configurado ANTES de pedirla, y los redirects se siguen A MANO validando cada salto. El prototipo dejaba que
    `requests` siguiera redirects solo en las descargas, y mandaba el token en cada salto: un redirect a otro host le habria entregado el token.
  * El token viaja unicamente en el header de la peticion; no se loguea ni entra en ningun mensaje de error (los errores salen por `connections.redact`).
  * Se exige HTTPS salvo para localhost (pruebas): el token no debe viajar en claro.
  * La respuesta se lee con tope de tamano para que un adjunto enorme no agote la memoria."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from html import unescape
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://empresa.atlassian.net/wiki"
MAX_REDIRECTS = 6
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # un adjunto mas grande no se baja entero a memoria
MAX_SECTIONS = 20
_PAGE_ID_CACHE_MAX = 512

# Cache a nivel de MODULO (en Vision cada tool-call crea un conector nuevo): resolver un short link cuesta un viaje a la red. Acotado (LRU) y con el HOST de la
# conexion dentro de la clave: dos conexiones a sitios distintos nunca comparten una resolucion.
_PAGE_ID_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_cache_lock = threading.Lock()


class ConfluenceConnectionError(Exception):
    """No se pudo autenticar o alcanzar Confluence, o una referencia no se pudo resolver. El texto NUNCA lleva el token ni el correo."""


# ---------------------------------------------------------------------------------------------------------------------
# Utilidades puras (sin red)
# ---------------------------------------------------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_ATTACHMENT_API_URL_RE = re.compile(r"/content/(\d+)/child/attachment/(att\d+)/download", re.I)
_ATTACHMENT_WEBUI_URL_RE = re.compile(r"/download/attachments/(\d+)/([^?\"']+)", re.I)


def _to_text(html_fragment: str) -> str:
    """Saca etiquetas, desescapa entidades y colapsa espacios. Borra <style>/<script> antes: si no, el «texto» arranca con CSS."""
    text = _STYLE_SCRIPT_RE.sub(" ", html_fragment or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", unescape(text)).strip()


def page_id_from_href(href: str) -> str | None:
    """pageId cuando el link ya lo trae (`pageId=` o `/pages/{id}`). Los `/display` y `/wiki/x/` se resuelven de forma perezosa."""
    if not href:
        return None
    match = re.search(r"pageId=(\d+)", href) or re.search(r"/pages/(\d+)", href)
    return match.group(1) if match else None


def _filename_from_disposition(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", value, re.I)
    return unquote(match.group(1)).strip().strip('"') if match else ""


def _escape_cql(value: str) -> str:
    """Escapa para meter `value` entre comillas dobles de una CQL: barra invertida primero y despues la comilla."""
    return value.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"')


# ---------------------------------------------------------------------------------------------------------------------
# Secciones (REQ-010): varias paginas raiz dentro de UNA conexion
# ---------------------------------------------------------------------------------------------------------------------
def parse_sections(raw: str) -> list[dict[str, str]]:
    """Lee el campo `sections` (JSON) ya normalizado. Cada seccion es {name, page_id[, space]}. Lo invalido se descarta."""
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        out.append({"name": str(item.get("name", "")), "page_id": str(item.get("page_id", "")), "space": str(item.get("space", ""))})
    return out


def normalize_confluence_fields(fields: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Valida y limpia los campos de un bloque de Confluence del archivo. Devuelve (campos, errores sin valores secretos)."""
    from app import connections  # import diferido: `connections` registra este tipo al importarse (evita ciclo)

    errors: list[str] = []
    clean = dict(fields)
    base = clean.get("confluence_base_url", "").strip()
    if base:
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
        local = host in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            errors.append("confluence_base_url debe ser https (el token no puede viajar en claro); se usa la URL por defecto")
            clean["confluence_base_url"] = ""
        elif not host or parsed.username or parsed.password:
            errors.append("confluence_base_url invalida o con usuario/clave dentro de la URL; se usa la URL por defecto")
            clean["confluence_base_url"] = ""
    raw_sections = clean.get("sections", "")
    if raw_sections.strip():
        try:
            items = json.loads(raw_sections)
        except ValueError:
            items = None
            errors.append("sections no es una lista valida; se ignora")
        if items is not None and not isinstance(items, list):
            errors.append("sections debe ser una lista de {name, page_id}; se ignora")
            items = None
        good: list[dict[str, str]] = []
        seen: set[str] = set()
        for i, item in enumerate(items or [], start=1):
            if not isinstance(item, dict):
                errors.append(f"seccion #{i}: no es un bloque valido")
                continue
            name = str(item.get("name", ""))
            page_id = str(item.get("page_id", "")).strip()
            space = str(item.get("space", "")).strip()
            if not connections.valid_label(name):
                errors.append(f"seccion #{i}: nombre invalido (1 a {connections.MAX_NAME_LENGTH} caracteres, sin llaves, comillas ni barras)")
                continue
            if not page_id.isdigit():
                errors.append(f"seccion '{name}': page_id debe ser el numero de la pagina raiz")
                continue
            if space and not re.fullmatch(r"[A-Za-z0-9_~-]{1,64}", space):
                errors.append(f"seccion '{name}': space invalido")
                space = ""
            if name.lower() in seen:
                errors.append(f"seccion '{name}': nombre repetido")
                continue
            if len(good) >= MAX_SECTIONS:
                errors.append(f"se supero el tope de {MAX_SECTIONS} secciones")
                break
            seen.add(name.lower())
            good.append({"name": name, "page_id": page_id, "space": space})
        clean["sections"] = json.dumps(good, ensure_ascii=False)
    return clean, errors


def confluence_public_info(conn: Any) -> dict[str, Any]:
    """Lo que la pantalla puede ver de una conexion de Confluence: sus secciones (nombre y pagina raiz) y el espacio por defecto. NUNCA el token ni el correo."""
    sections = parse_sections(conn.fields.get("sections", ""))
    info: dict[str, Any] = {"sections": sections}
    space = conn.fields.get("confluence_space_key", "").strip()
    if space:
        info["space"] = space
    return info


def confluence_legacy_fields(settings: Any) -> dict[str, str]:
    """Campos de la conexion de Confluence armada desde el `.env` (cuando NO hay `connections.json`): la URL y el espacio salen de CONFLUENCE_BASE_URL y
    CONFLUENCE_SPACE_KEY, y la pagina raiz de CONFLUENCE_ROOT_PAGE_ID (con CONFLUENCE_ROOT_PAGE_NAME como nombre) pasa a ser UNA seccion. Asi quien completa
    solo el `.env` ve su arbol en Conexiones sin escribir ningun archivo. Pasa por el mismo normalizador que un bloque del archivo: lo invalido se descarta."""
    root = str(getattr(settings, "confluence_root_page_id", "") or "").strip()
    if not root:
        return {}
    name = str(getattr(settings, "confluence_root_page_name", "") or "").strip() or "Pagina raiz"
    fields, _errors = normalize_confluence_fields({"sections": json.dumps([{"name": name, "page_id": root}])})
    return {"sections": fields.get("sections", "")}


def register_confluence_type() -> None:
    """Registra el tipo «Confluence» por la MISMA puerta que Oracle/SharePoint/SAP (REQ-012)."""
    from app import connections

    def _test(settings: Settings) -> bool:
        return ConfluenceConnector(settings).test_connection()

    connections.register_type(
        connections.ConnectionType(
            "confluence",
            "Confluence",
            (
                connections.ConnectionField("confluence_base_url", "URL base"),
                connections.ConnectionField("confluence_space_key", "Espacio por defecto"),
                connections.ConnectionField("sections", "Secciones (nombre + pagina raiz)", structured=True),
            ),
            legacy_name="Principal",
            legacy_probe=("confluence_email",),
            tester=_test,
            enabled_by="enable_confluence",
            personal_fields=(
                connections.ConnectionField("confluence_email", "Correo (en tu .env)", required=True),
                connections.ConnectionField("confluence_api_token", "Token personal (en tu .env)", secret=True, required=True),
            ),
            normalize=normalize_confluence_fields,
            public_info=confluence_public_info,
            legacy_fields=confluence_legacy_fields,
            name_setting="confluence_conexion_name",
        )
    )


# ---------------------------------------------------------------------------------------------------------------------
# Conector
# ---------------------------------------------------------------------------------------------------------------------
class ConfluenceConnector:
    """Cliente minimo y de SOLO LECTURA a Confluence Cloud."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        base = (getattr(self._settings, "confluence_base_url", "") or DEFAULT_BASE_URL).strip().rstrip("/")
        self._base = base
        parsed = urlparse(base)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._host = (parsed.hostname or "").lower()
        self._timeout = int(getattr(self._settings, "confluence_timeout_seconds", 30) or 30)
        self._space = str(getattr(self._settings, "confluence_space_key", "") or "")
        self._proxies = self._resolve_proxies()

    # -- infraestructura HTTP ------------------------------------------------
    def _resolve_proxies(self) -> dict[str, str] | None:
        proxy = (
            getattr(self._settings, "confluence_https_proxy", "")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or ""
        )
        return {"http": proxy, "https": proxy} if proxy else None

    def _auth(self) -> tuple[str, str]:
        email = str(getattr(self._settings, "confluence_email", "") or "")
        token = str(getattr(self._settings, "confluence_api_token", "") or "")
        if not email or not token:
            raise ConfluenceConnectionError(
                "Falta tu correo o tu token de Confluence. Cargá CONFLUENCE_EMAIL y CONFLUENCE_API_TOKEN en tu .env "
                "(el token se genera en id.atlassian.com > Seguridad > Tokens de API)."
            )
        return email, token

    def _normalize_url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if path.startswith("/wiki/"):  # `_links.next` de la API v2 ya trae /wiki
            return self._origin + path
        return self._base + path

    def _assert_allowed_url(self, url: str) -> None:
        """Lista blanca de host (anti-SSRF): solo se habla con el Confluence configurado. Compara el host EXACTO: `empresa.atlassian.net.evil.com` no pasa."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host or host != self._host:
            raise ConfluenceConnectionError(f"URL no permitida: solo se aceptan paginas de {self._host}.")
        if parsed.username or parsed.password:
            raise ConfluenceConnectionError("URL no permitida: no puede llevar usuario ni clave.")
        if parsed.scheme not in ("https", "http"):
            raise ConfluenceConnectionError("URL no permitida: esquema no soportado.")

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """UNICO punto de salida a la red. Solo GET (REQ-009); la URL se valida; los redirects se siguen A MANO, validando cada salto, para que el token
        nunca se mande a otro host."""
        if method.upper() != "GET":
            raise ConfluenceConnectionError("Esta conexion es de solo lectura: solo se permite GET.")
        url = self._normalize_url(path)
        auth = self._auth()
        kwargs.setdefault("timeout", self._timeout)
        kwargs.setdefault("proxies", self._proxies)
        kwargs.pop("allow_redirects", None)
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._assert_allowed_url(current)
            resp = requests.request("GET", current, auth=auth, allow_redirects=False, **kwargs)
            location = resp.headers.get("Location", "")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                nxt = self._normalize_url(location) if not location.startswith("http") else location
                if "://" not in location and location.startswith("/"):
                    nxt = self._origin + location
                self._assert_allowed_url(nxt)  # el salto tambien tiene que ser del mismo host, o se corta ANTES de mandarle el token
                current = nxt
                kwargs.pop("params", None)  # el Location ya trae la query completa
                continue
            return resp
        raise ConfluenceConnectionError("Demasiados redirects al consultar Confluence.")

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = self._request("GET", path, params=params)
        if resp.status_code == 401:
            raise ConfluenceConnectionError("Confluence rechazo tu correo o tu token (401). Revisá CONFLUENCE_EMAIL y CONFLUENCE_API_TOKEN en tu .env.")
        if resp.status_code == 403:
            raise ConfluenceConnectionError("Tu usuario no tiene permiso para ver esto en Confluence (403).")
        if resp.status_code == 404:
            raise ConfluenceConnectionError("Confluence no encontro lo pedido (404).")
        if resp.status_code != 200:
            raise ConfluenceConnectionError(f"Confluence respondio con error {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise ConfluenceConnectionError("Confluence devolvio una respuesta que no es JSON.") from exc

    def _paginate(self, path: str, params: dict[str, Any] | None = None, max_items: int = 1000) -> list[dict]:
        """Recorre `results` siguiendo `_links.next` (v1 y v2)."""
        out: list[dict] = []
        endpoint: str | None = path
        current = params
        for _ in range(200):  # tope duro de paginas: un `next` que se repite nunca cuelga la tool
            if not endpoint:
                break
            data = self._get_json(endpoint, current)
            out.extend(data.get("results", []))
            if len(out) >= max_items:
                return out[:max_items]
            endpoint = (data.get("_links") or {}).get("next")
            current = None
        return out

    # -- diagnostico ---------------------------------------------------------
    def current_user(self) -> dict:
        return self._get_json("/rest/api/user/current")

    def test_connection(self) -> bool:
        try:
            self.current_user()
            return True
        except Exception as exc:  # noqa: BLE001 -- diagnostico: nunca debe romper la UI; el texto NO se loguea (puede traer detalles de la cuenta)
            logger.warning("Confluence no disponible (%s)", type(exc).__name__)
            return False

    # -- listados y busqueda -------------------------------------------------
    def list_spaces(self, limit: int = 200) -> list[dict[str, str]]:
        rows = self._paginate("/rest/api/space", {"limit": 100}, max_items=limit)
        return [{"key": r.get("key", ""), "name": r.get("name", ""), "type": r.get("type", "")} for r in rows]

    def _page_url(self, page_id: str, space_key: str | None = None) -> str:
        space = space_key or self._space
        return f"{self._base}/spaces/{space}/pages/{page_id}" if space else f"{self._base}/pages/viewpage.action?pageId={page_id}"

    def search_cql(self, cql: str, limit: int = 25) -> list[dict[str, Any]]:
        """Busqueda global de Confluence con CQL, con los permisos del PROPIO usuario (solo lectura: no hay escalada)."""
        rows = self._paginate("/rest/api/content/search", {"cql": cql, "limit": 25, "expand": "space"}, max_items=limit)
        out: list[dict[str, Any]] = []
        for r in rows:
            links = r.get("_links", {}) or {}
            webui = links.get("webui", "")
            out.append(
                {
                    "id": str(r.get("id", "")),
                    "title": r.get("title", ""),
                    "space_key": (r.get("space", {}) or {}).get("key", ""),
                    "url": f"{self._base}{webui}" if webui else "",  # la base sale de la conexion, NO de lo que diga la respuesta
                    "excerpt": _to_text(r.get("excerpt", "")),
                }
            )
        return out

    def search(self, query: str, space_key: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        term = _escape_cql((query or "").strip())
        if not term:
            return []
        cql = f'type = page AND text ~ "{term}"'
        if space_key:
            cql = f'type = page AND space = "{_escape_cql(space_key)}" AND text ~ "{term}"'
        return self.search_cql(cql, limit=limit)

    # -- resolucion de referencias (id / url / titulo) -----------------------
    def resolve_page_id(self, ref: str) -> str:
        key = str(ref).strip()
        if not key:
            raise ConfluenceConnectionError("Referencia vacia: se esperaba pageId, URL o titulo.")
        cache_key = (self._host, key)
        with _cache_lock:
            cached = _PAGE_ID_CACHE.get(cache_key)
            if cached:
                _PAGE_ID_CACHE.move_to_end(cache_key)
                return cached
        if key.isdigit():
            page_id = key
        elif "://" in key.split(" ", 1)[0]:
            # cualquier cosa con forma de URL (incluidos ftp:// y file://) se valida: antes solo se miraba `http`, y un `ftp://...` se trataba como TITULO
            self._assert_allowed_url(key)
            page_id = page_id_from_href(key) or self._resolve_by_redirect(key)
        else:
            page_id = self._resolve_title(key)
        with _cache_lock:
            _PAGE_ID_CACHE[cache_key] = page_id
            while len(_PAGE_ID_CACHE) > _PAGE_ID_CACHE_MAX:
                _PAGE_ID_CACHE.popitem(last=False)
        return page_id

    def _resolve_by_redirect(self, url: str) -> str:
        """Sigue los redirects de un short link (/wiki/x/...) hasta encontrar un pageId en la URL. Cada salto se valida contra el host permitido."""
        current = url
        for _ in range(MAX_REDIRECTS):
            self._assert_allowed_url(current)
            resp = requests.request("GET", current, auth=self._auth(), allow_redirects=False, timeout=self._timeout, proxies=self._proxies)
            location = resp.headers.get("Location", "")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                nxt = location if location.startswith("http") else self._origin + location
                self._assert_allowed_url(nxt)
                page_id = page_id_from_href(nxt)
                if page_id:
                    return page_id
                current = nxt
                continue
            page_id = page_id_from_href(current)
            if page_id:
                return page_id
            break
        raise ConfluenceConnectionError("No se pudo resolver esa referencia a una pagina de Confluence.")

    def _resolve_title(self, title: str, space_key: str | None = None) -> str:
        space = space_key or self._space
        if not space:
            raise ConfluenceConnectionError("Para buscar por titulo hace falta el espacio: indicalo o usa el pageId.")
        data = self._get_json("/rest/api/content", {"spaceKey": space, "title": title, "limit": 1})
        for item in data.get("results", []):
            return str(item["id"])
        cql = f'space = "{_escape_cql(space)}" and title = "{_escape_cql(title)}"'
        data = self._get_json("/rest/api/content/search", {"cql": cql, "limit": 1})
        for item in data.get("results", []):
            return str(item["id"])
        raise ConfluenceConnectionError(f"No se encontro la pagina '{title}' en el espacio {space}.")

    # -- paginas y arbol -----------------------------------------------------
    def get_page(self, ref: str) -> dict[str, Any]:
        """Cualquier pagina con su HTML renderizado y su texto plano."""
        page_id = self.resolve_page_id(ref)
        data = self._get_json(f"/rest/api/content/{page_id}", {"expand": "body.view,version,space"})
        view = (((data.get("body") or {}).get("view") or {}).get("value")) or ""
        space_key = ((data.get("space") or {}).get("key")) or self._space
        return {
            "id": str(data.get("id", page_id)),
            "title": data.get("title", ""),
            "space_key": space_key,
            "version": (data.get("version") or {}).get("number"),
            "url": self._page_url(page_id, space_key),
            "html": view,
            "text": _to_text(view),
        }

    def list_child_pages(self, page_id: str, limit: int = 250) -> list[dict[str, str]]:
        """Hijos DIRECTOS (v2). Es lo que usa el arbol con carga perezosa."""
        page_id = self.resolve_page_id(page_id)
        rows = self._paginate(f"/api/v2/pages/{page_id}/children", {"limit": 250}, max_items=limit)
        return [{"id": str(r["id"]), "title": r.get("title", "")} for r in rows]

    def get_page_tree(self, ref: str, max_depth: int = 1, max_nodes: int = 300) -> dict[str, Any]:
        """Arbol padre/hijo hasta `max_depth` niveles, con tope de nodos (un arbol ancho dispararia cientos de requests)."""
        max_depth = max(0, min(int(max_depth), 5))
        max_nodes = max(1, min(int(max_nodes), 1000))
        root = self.get_page(ref)
        state = {"count": 0, "truncated": False}

        def branch(page_id: str, title: str, depth: int) -> dict[str, Any]:
            node: dict[str, Any] = {
                "id": page_id,
                "title": title,
                "space_key": root["space_key"],
                "url": self._page_url(page_id, root["space_key"]),
                "children": [],
            }
            state["count"] += 1
            if depth < max_depth and not state["truncated"]:
                for child in self.list_child_pages(page_id):
                    if state["count"] >= max_nodes:
                        state["truncated"] = True
                        break
                    node["children"].append(branch(child["id"], child["title"], depth + 1))
            return node

        result = branch(root["id"], root["title"], 0)
        result["truncated"] = state["truncated"]
        return result

    def get_page_with_descendants(self, ref: str, max_depth: int = 3, max_pages: int = 50) -> dict[str, Any]:
        """Pagina + subpaginas concatenadas (texto), con tope de paginas."""
        page = self.get_page(ref)
        parts = [page["text"]]
        descendants: list[dict[str, str]] = []

        def walk(parent_id: str, depth: int) -> None:
            if depth > max_depth or len(descendants) >= max_pages:
                return
            for child in self.list_child_pages(parent_id):
                if len(descendants) >= max_pages:
                    return
                child_page = self.get_page(child["id"])
                descendants.append({"id": child_page["id"], "title": child_page["title"]})
                parts.append(f"\n\n## {child_page['title']}\n{child_page['text']}")
                walk(child["id"], depth + 1)

        walk(page["id"], 1)
        page["text_full"] = "".join(parts)
        page["descendants"] = descendants
        return page

    # -- adjuntos ------------------------------------------------------------
    def list_attachments(self, ref: str, limit: int = 200) -> list[dict[str, Any]]:
        page_id = self.resolve_page_id(ref)
        rows = self._paginate(f"/rest/api/content/{page_id}/child/attachment", {"limit": 100, "expand": "version"}, max_items=limit)
        out: list[dict[str, Any]] = []
        for item in rows:
            links = item.get("_links", {}) or {}
            meta = item.get("metadata", {}) or {}
            ext = item.get("extensions", {}) or {}
            out.append(
                {
                    "id": str(item.get("id", "")),
                    "title": item.get("title", ""),
                    "media_type": meta.get("mediaType", ""),
                    "size": ext.get("fileSize"),
                    "version": (item.get("version") or {}).get("number"),
                    "download_url": f"{self._base}{links.get('download', '')}" if links.get("download") else "",
                }
            )
        return out

    def get_attachment(self, page_id: str, attachment_id: str) -> tuple[bytes, str, str]:
        """Descarga un adjunto (con tope de tamano). Devuelve (bytes, nombre, tipo)."""
        if not re.fullmatch(r"att\d+|\d+", str(attachment_id)):
            raise ConfluenceConnectionError("Identificador de adjunto invalido.")
        page_id = self.resolve_page_id(page_id)
        resp = self._request("GET", f"/rest/api/content/{page_id}/child/attachment/{attachment_id}/download", stream=True)
        try:
            if resp.status_code != 200:
                raise ConfluenceConnectionError(f"No se pudo descargar el adjunto ({resp.status_code}).")
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > MAX_ATTACHMENT_BYTES:
                raise ConfluenceConnectionError(f"El adjunto pesa mas de {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB: no se descarga.")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_ATTACHMENT_BYTES:
                    raise ConfluenceConnectionError(f"El adjunto pesa mas de {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB: no se descarga.")
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise ConfluenceConnectionError("El adjunto esta vacio.")
            name = _filename_from_disposition(resp.headers.get("Content-Disposition")) or str(attachment_id)
            return data, name, resp.headers.get("Content-Type") or "application/octet-stream"
        finally:
            resp.close()

    def get_attachment_by_url(self, url: str) -> tuple[bytes, str, str]:
        """Descarga un adjunto a partir de la URL que aparece en el HTML. Solo URLs del Confluence configurado y de los dos formatos conocidos."""
        key = str(url).strip()
        self._assert_allowed_url(key)
        match = _ATTACHMENT_API_URL_RE.search(key)
        if match:
            return self.get_attachment(match.group(1), match.group(2))
        match = _ATTACHMENT_WEBUI_URL_RE.search(key)
        if match:
            page_id = match.group(1)
            wanted = unquote(match.group(2))
            for att in self.list_attachments(page_id):
                if att["title"] == wanted or att["download_url"].endswith(wanted):
                    return self.get_attachment(page_id, att["id"])
            raise ConfluenceConnectionError("No se encontro ese adjunto en la pagina.")
        raise ConfluenceConnectionError("URL de adjunto no reconocida.")


__all__ = [
    "ConfluenceConnectionError",
    "ConfluenceConnector",
    "confluence_legacy_fields",
    "confluence_public_info",
    "normalize_confluence_fields",
    "page_id_from_href",
    "parse_sections",
    "register_confluence_type",
]
