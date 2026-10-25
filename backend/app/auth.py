"""
Auth minima por token compartido -- spec jarvis-mobile-pwa.

Vision nacio pensado para correr en 127.0.0.1 (un solo usuario, su propia
notebook, nadie mas en el medio). El plan "Jarvis Mobile" requiere levantar
el backend con APP_HOST=0.0.0.0 para que celulares en la misma red WiFi
puedan conectarse -- pero eso vuelve el puerto alcanzable para CUALQUIER
dispositivo de esa red, no solo el celular del usuario. Sin ningun tipo de
auth, cualquiera en la LAN tendria control total del API (tools bash/write/
edit con acceso al filesystem local, conectores Oracle/SharePoint/SAP con
credenciales reales).

Este modulo agrega la barrera minima viable: un token compartido de un solo
valor (no hay usuarios, no hay sesiones de login, no hay expiracion) que el
cliente manda en el header ``X-Jarvis-Token``. Si ``settings.access_token``
esta vacio (default), la funcion no hace nada -- el comportamiento actual en
127.0.0.1 sin token configurado sigue exactamente igual que antes de esta
spec. Explicitamente NO es un sistema de autenticacion robusto (no hay
rate-limiting, ni rotacion, ni multi-usuario) -- pensado para confianza de
LAN/red privada, mismo nivel de garantia que el Basic Auth de opencode-dev
(ver server/src/auth.py de ese proyecto de referencia).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings

# Rutas que quedan siempre exentas del chequeo de token, incluso con
# ACCESS_TOKEN configurado: el healthcheck debe poder confirmarse sin
# credenciales (asi la pantalla "Conectar a servidor" del frontend puede
# validar que la IP:puerto responden ANTES de pedir/probar el token), y el
# frontend estatico (HTML/CSS/JS/vendor) no expone informacion sensible por
# si solo -- lo sensible vive detras de /api/*.
_EXEMPT_PREFIXES = ("/api/status",)


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """Exige el header ``X-Jarvis-Token`` en todas las rutas ``/api/*``
    (salvo las exentas) cuando ``settings.access_token`` esta configurado."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        token = settings.access_token
        path = request.url.path

        if not token:
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)
        if path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # El header es el camino normal (usado por apiFetch() del
        # frontend). El query param ?token=... es el fallback para el unico
        # caso que no puede setear headers: la descarga de export via
        # `window.location.href` (navegacion directa del navegador, ver
        # app.js exportBtn.onclick).
        provided = request.headers.get("x-jarvis-token") or request.query_params.get(
            "token", ""
        )
        if provided != token:
            # Texto para personas no tecnicas (pedido del usuario 2026-09-24);
            # el nombre del header queda en el docstring del modulo.
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Este navegador no tiene la clave de acceso de ULTRON, o no es la "
                        "correcta. Cargala en Configuración > Servidor, en «Token de acceso»."
                    )
                },
            )

        return await call_next(request)


class OpaqueOriginMiddleware(BaseHTTPMiddleware):
    """REQ-006 (spec navegador-integrado-ventana): rechaza con 403 toda
    request a ``/api/*`` que llegue con ``Origin: null``.

    Un iframe con ``sandbox`` SIN ``allow-same-origin`` (el modo "Aislado" del
    Navegador Integrado) tiene origen opaco y el navegador manda ese header.
    Aunque el CORS de Vision es ``*``, un dashboard aislado NO debe poder
    llamar a la API (``POST /api/chat`` corre ``bash`` sin confirmacion).
    Ningun cliente legitimo de Vision manda ``Origin: null`` (el frontend
    comparte origen y curl/tests no mandan Origin), por eso el rechazo puede
    estar siempre activo: lo que habilita el aislamiento es solo el sandbox."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.headers.get("origin") == "null":
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "Esta vista previa está aislada y no puede usar las funciones de "
                        "ULTRON. Desactivá el modo «Aislado» del Navegador Integrado si "
                        "necesitás que se comunique con ULTRON."
                    )
                },
            )
        return await call_next(request)
