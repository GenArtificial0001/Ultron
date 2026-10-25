"""Acceso SIEMPRE restringido a las credenciales guardadas (spec gestor-conexiones REQ-007/007b).

Por que existe: el usuario decidio que el ojo muestre tambien la contrasena GUARDADA (2026-10-08: «la configuracion de claves es personal») y pidio «siempre restringe
el acceso a la pantalla de contraseñas». Este modulo concentra las dos barreras, que NO se pueden apagar:

1. `local_page_problem()`: el pedido tiene que venir de ESTA PC (loopback), con un `Host` local (corta el DNS rebinding: un dominio ajeno que resuelve a 127.0.0.1) y,
   si el navegador manda `Origin`/`Sec-Fetch-Site`, desde la propia pagina de Vision. El CORS de Vision hoy es `*` (hallazgo del 2026-10-08, tarea aparte): sin esta
   barrera, cualquier pagina abierta en el navegador podria leer las respuestas.
2. Desbloqueo con vencimiento: ver o cambiar un secreto YA guardado exige haber confirmado la identidad (Windows Hello / clave de Windows) hace pocos minutos. El
   verificador real llega en la Fase B (B3); hasta entonces no hay verificador y desbloquear se rechaza con un mensaje claro. Los tests inyectan uno.

El estado de desbloqueo vive en MEMORIA del proceso (nunca en disco): reiniciar Vision lo bloquea."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

UNLOCK_SECONDS = 300  # «unos minutos» (spec REQ-007b); vence solo
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SAME_SITE_VALUES = frozenset({"same-origin", "none"})  # «none» = el usuario abrio la URL a mano (no la pidio otra pagina)

_lock = threading.Lock()
_unlocked_until = 0.0
_verifier: Callable[[], bool] | None = None


class UnlockError(Exception):
    """No se pudo desbloquear. El texto es para la persona."""


def set_verifier(verifier: Callable[[], bool] | None) -> None:
    """Instala la funcion que confirma la identidad (Windows Hello en B3). Devuelve True solo si la persona la confirmo."""
    global _verifier
    _verifier = verifier


def local_page_problem(client_host: str | None, host_header_hostname: str | None, origin: str | None, sec_fetch_site: str | None, own_origin: str) -> str | None:
    """Motivo para rechazar el pedido (texto para la persona), o None si viene de la propia pagina de Vision en esta PC."""
    if (client_host or "") not in _LOCAL_HOSTS:
        return "Las credenciales solo se pueden ver o cambiar desde la PC donde corre ULTRON."
    if (host_header_hostname or "").lower() not in _LOCAL_HOSTS:
        return "Las credenciales solo se pueden ver o cambiar desde la página de ULTRON en esta PC."
    if origin and origin.rstrip("/") != own_origin.rstrip("/"):
        return "Otra página intentó usar las credenciales de ULTRON y se bloqueó."
    if sec_fetch_site and sec_fetch_site.lower() not in _SAME_SITE_VALUES:
        return "Otra página intentó usar las credenciales de ULTRON y se bloqueó."
    return None


def unlock() -> float:
    """Pide la confirmacion de identidad y, si la persona la da, desbloquea por `UNLOCK_SECONDS`. Devuelve hasta cuando (epoch)."""
    global _unlocked_until
    verifier = _verifier
    if verifier is None:
        raise UnlockError("Todavía no se puede desbloquear: falta la confirmación con Windows Hello (llega con la pantalla de conexiones).")
    try:
        confirmed = bool(verifier())
    except Exception as exc:  # noqa: BLE001 -- cualquier falla del verificador es «no confirmado», nunca un desbloqueo
        raise UnlockError("Windows no pudo confirmar que sos vos. Probá de nuevo.") from exc
    if not confirmed:
        raise UnlockError("No se confirmó tu identidad: las contraseñas guardadas siguen ocultas.")
    with _lock:
        _unlocked_until = time.time() + UNLOCK_SECONDS
        return _unlocked_until


def lock() -> None:
    global _unlocked_until
    with _lock:
        _unlocked_until = 0.0


def unlocked_until() -> float | None:
    """Hasta cuando esta desbloqueado (epoch), o None si esta bloqueado."""
    with _lock:
        return _unlocked_until if time.time() < _unlocked_until else None


def is_unlocked() -> bool:
    return unlocked_until() is not None
