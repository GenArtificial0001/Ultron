"""Errores de la automatizacion de Office, dichos para una persona no tecnica (spec office-com-automatizacion REQ-010).

Por que existe: Excel y Word se controlan por COM, y COM falla de formas que a una persona no le dicen nada (`com_error -2147221021`). Cada situacion real tiene SU frase, que dice que paso y que hacer:
Excel cerrado, Excel ocupado con un cuadro de dialogo, una llamada que tarda mas de 15 s, una PC sin pywin32 u Office, o un pedido que no se puede hacer (un rango invalido, un libro que no esta abierto).
Pasan el mismo guardian de jerga que el resto de los mensajes de Vision (`tests/test_friendly_errors.py`).

Reglas:
- el texto crudo de una excepcion COM NUNCA va al mensaje: va solo a `technical_detail()`, recortado y sin el contenido de las celdas;
- `OfficeUserError` es para un pedido que no se puede cumplir y cuyo mensaje ya esta escrito para la persona (lo arma quien valida, no una excepcion de COM);
- NINGUNA de estas fallas lanza Excel por su cuenta ni abre un libro: decirle a la persona que lo abra es lo unico que se hace.

Reusa `FriendlyError` de `app/friendly_errors.py` para que el resto de Vision (pantalla, tools, log) lo trate igual que cualquier otro error de conector."""

from __future__ import annotations

from app.friendly_errors import FriendlyError

# HRESULT de COM (en decimal, como los devuelve pywin32) que significan algo concreto para la persona.
_NOT_RUNNING = {
    -2147221021,
    -2147221005,
}  # MK_E_UNAVAILABLE / CO_E_CLASSSTRING: no hay una instancia abierta (o no esta instalado)
_BUSY = {
    -2147418111,
    -2147417846,
    -2147417836,
}  # RPC_E_CALL_REJECTED / SERVERCALL_RETRYLATER / SERVERCALL_REJECTED: Excel esta ocupado
_PROTECTED = {
    -2146827284
}  # 0x800A03EC: Excel no dejo hacer el cambio (hoja protegida, celdas combinadas, libro de solo lectura...)


class OfficeError(Exception):
    """Falla de Office con su frase ya lista para la persona (`friendly`)."""

    def __init__(self, friendly: FriendlyError) -> None:
        super().__init__(friendly.message)
        self.friendly = friendly


class OfficeNotRunning(OfficeError):
    """La aplicacion no esta abierta. NUNCA se la lanza: se le pide a la persona que la abra."""

    def __init__(self, app: str = "Excel") -> None:
        super().__init__(
            FriendlyError(
                "OFFICE-NO-ABIERTO",
                f"{app} no está abierto. Abrí {app} con el archivo que querés usar y volvé a pedirlo.",
            )
        )


class OfficeBusy(OfficeError):
    """La aplicacion esta ocupada (un cuadro de dialogo abierto, una celda en edicion) y rechaza llamadas."""

    def __init__(self, app: str = "Excel") -> None:
        super().__init__(
            FriendlyError(
                "OFFICE-OCUPADO",
                f"{app} está ocupado: seguro tiene un cuadro de diálogo abierto o una celda en edición. Cerrá ese cuadro y volvé a probar.",
            )
        )


class OfficeTimeout(OfficeError):
    """Una llamada tardo mas del plazo (15 s)."""

    def __init__(self) -> None:
        super().__init__(
            FriendlyError(
                "OFFICE-DEMORA",
                "Office tardó más de 15 segundos en responder. Revisá que no haya un cuadro de diálogo abierto y probá de nuevo.",
            )
        )


class OfficeUnavailable(OfficeError):
    """Esta PC no puede controlar Office (falta pywin32 u Office no esta instalado). El detalle va al log, no a la persona."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            FriendlyError(
                "OFFICE-NO-DISPONIBLE",
                "Esta PC no tiene lo necesario para controlar Office desde Vision (falta un componente de Windows o Office no está instalado). Avisá a soporte para que lo revisen.",
            )
        )
        self.detail = detail


class OfficeUserError(OfficeError):
    """Un pedido que no se puede cumplir (rango invalido, libro que no esta abierto...). El mensaje ya viene escrito para la persona."""

    def __init__(self, message: str) -> None:
        super().__init__(FriendlyError("OFFICE-PEDIDO", message))


def from_com_error(exc: BaseException, app: str = "Excel") -> OfficeError:
    """Traduce un `pywintypes.com_error` (o parecido) a la falla que le corresponde a la persona. Lo que no se reconoce queda como un fallo generico, nunca con el texto crudo."""
    hresult = getattr(exc, "hresult", None)
    if hresult is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        hresult = exc.args[0]
    if hresult in _NOT_RUNNING:
        return OfficeNotRunning(app)
    if hresult in _BUSY:
        return OfficeBusy(app)
    if hresult in _PROTECTED:
        return OfficeUserError(
            f"{app} no dejó hacer ese cambio: la hoja puede estar protegida, el libro ser de solo lectura o las celdas estar combinadas. Revisalo en {app} y probá de nuevo."
        )
    return OfficeError(
        FriendlyError(
            "OFFICE-FALLO", f"{app} devolvió un error inesperado. Probá de nuevo; si se repite, avisá a soporte."
        )
    )


def describe(exc: BaseException) -> FriendlyError:
    """La frase para la persona. Una `OfficeError` ya trae la suya; lo demas se traduce o queda como fallo generico (nunca el texto crudo)."""
    if isinstance(exc, OfficeError):
        return exc.friendly
    if isinstance(exc, TimeoutError):
        return OfficeTimeout().friendly
    if type(exc).__name__ == "com_error":
        return from_com_error(exc).friendly
    return FriendlyError(
        "OFFICE-FALLO", "Office devolvió un error inesperado. Probá de nuevo; si se repite, avisá a soporte."
    )


def technical_detail(exc: BaseException) -> str:
    """El crudo recortado para quien soporta Vision. Solo el TIPO y el HRESULT: el mensaje de una excepcion COM puede traer parte del contenido de una celda."""
    hresult = getattr(exc, "hresult", None)
    suffix = f" hresult={hresult}" if hresult is not None else ""
    return f"{type(exc).__name__}{suffix}"[:200]
