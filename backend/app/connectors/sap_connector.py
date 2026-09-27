"""
Conector propio de Vision a SAP Fiori Launchpad / servicios OData v2
custom (prefijo Z), version reducida del conector de DBManager
(app/connectors/sap_connector.py, 1600 lineas -- ahi vive la version
completa con SAP GUI clasico/WebGUI/ITS, atajos de reportes especificos,
etc.). Esta version solo cubre lo esencial para que el agente pueda
consultar CUALQUIER servicio OData generico:

    1. Login por formulario clasico (token anti-CSRF `sap-login-XSRF`).
    2. Token CSRF de OData (`X-CSRF-Token: Fetch`).
    3. fetch_generic(): trae datos de cualquier entidad/servicio OData v2,
       con paginacion automatica (via ODataQueryBuilder, patron Builder).

Variables de entorno usadas:
    SAP_BASE_URL, SAP_CLIENT, SAP_USER, SAP_PASSWORD, SAP_LANGUAGE
"""

from __future__ import annotations

import html
import logging
import re
import time

import pandas as pd
import requests

from app.config import BASE_DIR, Settings, get_settings
from app.connectors.odata_query_builder import ODataQueryBuilder

logger = logging.getLogger(__name__)

_XSRF_INPUT_RE = re.compile(r'name="sap-login-XSRF"\s+value="([^"]+)"')

# Investigacion de rendimiento 2026-09 ("Vision tarda mucho"): antes CADA
# `SapConnector(settings)` (uno nuevo por cada `sap_query` tool-call, ver
# tools/dbquery.py) arrancaba con `_logged_in=False` -- asi que cada consulta
# repetia el login completo (2 GET + 1 POST) MAS el fetch de CSRF token (1
# GET) antes de la consulta OData real: 4 round-trips de pura autenticacion
# por cada llamada, incluso dos seguidas en la misma conversacion. Cache de
# MODULO: la sesion autenticada (cookies) y los CSRF tokens ya obtenidos se
# comparten entre instancias mientras no venza el TTL.
_SESSION_TTL_SECONDS = 1500  # 25 min -- conservador, sin dato oficial de vida util de la cookie de sesion SAP
_session_cache: dict[tuple[str, str, str], dict] = {}  # (base_url, client, user) -> {session, csrf_tokens, logged_in_at}


class SapConnectionError(Exception):
    """Se lanza cuando falla el login, el token CSRF o una llamada OData a SAP."""


class SapConnector:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cache_key = (
            self._settings.sap_base_url,
            self._settings.sap_client,
            self._settings.sap_user,
        )
        cached = _session_cache.get(self._cache_key)
        if cached is not None and (time.monotonic() - cached["logged_in_at"]) < _SESSION_TTL_SECONDS:
            # Reusa la sesion (cookies) y los CSRF tokens ya obtenidos por
            # una llamada anterior -- mismo dict de `_csrf_tokens` que el
            # cache (referencia compartida), asi que un token nuevo que se
            # agregue mas abajo queda visible para la proxima instancia sin
            # tener que re-guardar el cache a mano.
            self._session = cached["session"]
            self._logged_in = True
            self._csrf_tokens: dict[str, str] = cached["csrf_tokens"]
        else:
            self._session = requests.Session()
            self._logged_in = False
            self._csrf_tokens = {}

    def _login(self) -> None:
        if self._logged_in:
            return

        settings = self._settings
        logon_url = (
            settings.sap_logon_url
            if hasattr(settings, "sap_logon_url")
            else (f"{settings.sap_base_url.rstrip('/')}/sap/bc/ui2/flp")
        )
        params = {
            "sap-client": settings.sap_client,
            "sap-language": settings.sap_language,
        }

        try:
            self._session.get(logon_url, params=params, timeout=20)
            resp = self._session.get(logon_url, params=params, timeout=20)
        except requests.RequestException as exc:
            raise SapConnectionError(
                f"No se pudo alcanzar SAP en {logon_url}: {exc}"
            ) from exc

        match = _XSRF_INPUT_RE.search(resp.text)
        if not match:
            raise SapConnectionError(
                "No se encontro el token 'sap-login-XSRF' en la pagina de logon. "
                f"Verifica SAP_BASE_URL en {BASE_DIR / '.env'} o en Configuracion > Credenciales."
            )
        login_xsrf = html.unescape(match.group(1))

        form_data = {
            "sap-system-login-oninputprocessing": "onLogin",
            "sap-urlscheme": "",
            "sap-system-login": "onLogin",
            "sap-system-login-basic_auth": "",
            "sap-client": settings.sap_client,
            "sap-accessibility": "",
            "sap-login-XSRF": login_xsrf,
            "sap-system-login-cookie_disabled": "",
            "sap-hash": "JTIzU2hlbGwtaG9tZQ",
            "sap-user": settings.sap_user,
            "sap-password": settings.sap_password,
            "__sap-sl__dummy": "1",
            "sap-language": settings.sap_language,
        }

        try:
            resp = self._session.post(
                logon_url,
                params={"_sap-hash": "JTIzU2hlbGwtaG9tZQ"},
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise SapConnectionError(
                f"No se pudo enviar el login a SAP: {exc}"
            ) from exc

        if _XSRF_INPUT_RE.search(resp.text) and "sap-password" in resp.text:
            raise SapConnectionError(
                "Login SAP rechazado. Verifica SAP_USER/SAP_PASSWORD en "
                f"{BASE_DIR / '.env'} o en Configuracion > Credenciales."
            )

        self._logged_in = True
        _session_cache[self._cache_key] = {
            "session": self._session,
            "csrf_tokens": self._csrf_tokens,
            "logged_in_at": time.monotonic(),
        }
        logger.info("Login SAP exitoso para usuario %s", settings.sap_user)

    def _get_csrf_token(self, service_name: str) -> str:
        if service_name in self._csrf_tokens:
            return self._csrf_tokens[service_name]

        self._login()
        url = self._settings.sap_odata_url(service_name)
        try:
            resp = self._session.get(
                url,
                params={"sap-client": self._settings.sap_client},
                headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise SapConnectionError(
                f"No se pudo obtener CSRF token de {service_name}: {exc}"
            ) from exc

        token = resp.headers.get("x-csrf-token")
        if not token:
            raise SapConnectionError(
                f"SAP no devolvio 'x-csrf-token' para el servicio '{service_name}' "
                f"(HTTP {resp.status_code}). Verifica que el servicio exista."
            )
        self._csrf_tokens[service_name] = token
        return token

    def test_connection(self) -> bool:
        try:
            self._login()
            return True
        except Exception as exc:
            logger.warning("SAP no disponible: %s", exc)
            return False

    def get_service_metadata(self, service_name: str) -> str:
        """Devuelve el XML de metadata ($metadata) de un servicio OData."""
        self._login()
        url = f"{self._settings.sap_odata_url(service_name)}$metadata"
        resp = self._session.get(
            url, params={"sap-client": self._settings.sap_client}, timeout=30
        )
        if resp.status_code != 200:
            raise SapConnectionError(
                f"Error obteniendo metadata de '{service_name}': {resp.text[:300]}"
            )
        return resp.text

    def fetch_generic(
        self,
        service_name: str,
        entity_name: str,
        entity_params: dict[str, str] | None = None,
        select_fields: list[str] | None = None,
        odata_filter: str | None = None,
        results_suffix: str = "/Results",
        page_size: int = 200,
        # Investigacion de rendimiento 2026-09: 500 era el default -- una
        # consulta sin filtro selectivo podia disparar hasta 500 requests
        # HTTP SECUENCIALES (c/u con timeout propio) antes de rendirse, para
        # resultados que igual se muestran truncados a 50 filas al modelo
        # (_df_to_text). 25 paginas (~5000 filas con el page_size default)
        # sigue siendo generoso frente a eso; quien necesite mas puede
        # pasarlo explicito.
        max_pages: int = 25,
    ) -> pd.DataFrame:
        """Trae datos de cualquier entidad/servicio OData v2 de SAP.
        Ver docstring del modulo para el detalle de cada argumento."""
        token = self._get_csrf_token(service_name)
        base_url = self._settings.sap_odata_url(service_name)

        query = (
            ODataQueryBuilder(entity_name=entity_name, results_suffix=results_suffix)
            .with_params(entity_params)
            .with_select(select_fields)
            .with_filter(odata_filter)
            .with_page_size(page_size)
        )
        url = query.build_url(base_url)

        registros: list[dict] = []
        skip = 0
        total_count: int | None = None
        pages_fetched = 0

        while True:
            params = query.build_query_params(
                skip=skip, sap_client=self._settings.sap_client
            )
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json", "X-CSRF-Token": token},
                    timeout=60,
                )
            except requests.RequestException as exc:
                raise SapConnectionError(
                    f"Error consultando '{service_name}/{entity_name}': {exc}"
                ) from exc

            if resp.status_code != 200:
                raise SapConnectionError(
                    f"Error HTTP {resp.status_code} consultando '{service_name}/{entity_name}': "
                    f"{resp.text[:400]}"
                )

            data = resp.json().get("d", {})
            if total_count is None and "__count" in data:
                total_count = int(data.get("__count", 0))

            page_results = data.get("results", [])
            for item in page_results:
                item.pop("__metadata", None)
                registros.append(item)

            pages_fetched += 1
            skip += page_size

            no_more_by_count = total_count is not None and skip >= total_count
            no_more_by_empty_page = not page_results
            if no_more_by_count or no_more_by_empty_page or pages_fetched >= max_pages:
                break

        return pd.DataFrame(registros)
