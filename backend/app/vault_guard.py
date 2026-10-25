"""Barrera de `/api/vault/*` ANTES que todo lo demas (spec gestor-conexiones REQ-007b: «siempre restringe el acceso a la pantalla de contraseñas»).

Por que un middleware y no solo un chequeo dentro de cada ruta: FastAPI valida el cuerpo del pedido (422) y resuelve las rutas (404) ANTES de ejecutar la funcion de
la ruta; el rechazo de un pedido ajeno tiene que ser lo PRIMERO que pasa, para cualquier ruta del gestor (tambien las que se agreguen despues). Las rutas igual
repiten el chequeo (defensa en profundidad). No hay interruptor ni variable que lo apague.

Vive en su propio modulo (no en app/auth.py) a proposito: la tarea de CORS (spec restringir-cors) puede tocar app/auth.py en paralelo."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import secret_access

VAULT_PREFIX = "/api/vault/"


class VaultGuardMiddleware(BaseHTTPMiddleware):
    """403 a todo pedido a `/api/vault/*` que no venga de esta PC y de la propia pagina de Vision (ver `secret_access.local_page_problem`)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(VAULT_PREFIX) or request.url.path == VAULT_PREFIX.rstrip("/"):
            problem = secret_access.local_page_problem(
                request.client.host if request.client else None,
                request.url.hostname,
                request.headers.get("origin"),
                request.headers.get("sec-fetch-site"),
                f"{request.url.scheme}://{request.url.netloc}",
            )
            if problem:
                return JSONResponse(status_code=403, content={"detail": problem}, headers={"Cache-Control": "no-store"})
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"  # revision de seguridad: nada del gestor queda en la cache del navegador
            return response
        return await call_next(request)
