"""Nucleo de la automatizacion de Excel por COM (spec office-com-automatizacion REQ-002, REQ-003, REQ-010).

Por que existe: Vision lee y escribe celdas en el Excel que la persona YA TIENE ABIERTO en su PC. Todo lo que toca COM pasa por aca, de modo que hay UN solo lugar donde se aplican las reglas.

Reglas (decisiones del usuario, spec):
- NUNCA se lanza Excel ni se abre un libro: solo se engancha a una instancia que ya esta corriendo (`GetActiveObject`, que falla si no hay una; `Dispatch` la lanzaria y por eso no se usa).
- Un libro se identifica SOLO por los que Excel ya tiene abiertos, por su nombre: una ruta arbitraria jamas se abre desde aca (seria leer cualquier archivo del disco con otro nombre).
- NUNCA se guarda un libro por su cuenta: escribir cambia las celdas en memoria; guardar es decision de la persona.
- Escribir VUELVE A LEER lo escrito y lo informa (`matches`): no un «ok» a ciegas. Si Excel dejo otra cosa (una formula que se evaluo, un formato), se dice.
- Plazo de 15 s por llamada: COM se cuelga con un cuadro de dialogo abierto; pasado el plazo se informa en lenguaje claro y no se espera mas.
- Los rangos se validan ANTES de tocar Excel: un rango que no es un rango simple, o enorme, se rechaza (el modelo no puede traer un millon de celdas ni armar una formula en el rango).

COM exige que cada hilo lo inicialice (`CoInitialize`) y que los objetos no se compartan entre hilos. Las tools corren en hilos distintos, asi que cada llamada abre SU propio hilo, inicializa COM, se engancha a
Excel, hace lo suyo y lo cierra: no queda ningun objeto COM vivo entre llamadas. El backend es inyectable (`set_backend`) para que la suite use un Office falso y nunca toque el de nadie."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Protocol

from app.office import errors

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = (
    15.0  # decision del usuario (D8): «si Excel no responde en 15 segundos, Vision lo informa y no se cuelga»
)
MAX_CELLS_READ = 5_000  # un rango mas grande se rechaza: el modelo no necesita un millon de celdas para responder
MAX_CELLS_WRITE = 500
MAX_ROW = 1_048_576  # limites de una hoja de Excel
MAX_COL = 16_384

_CELL = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]{0,6})$")


class OfficeBackend(Protocol):
    """Lo que el nucleo necesita de Excel. La implementacion real habla COM; la de las pruebas es un Excel falso."""

    def excel_books(self) -> list[dict[str, Any]]: ...

    def excel_read(self, book: str, sheet: str, rng: str) -> list[list[Any]]: ...

    def excel_write(self, book: str, sheet: str, rng: str, values: list[list[Any]]) -> None: ...


_backend: OfficeBackend | None = None


def set_backend(backend: OfficeBackend | None) -> None:
    """Reemplaza el backend (los tests inyectan un Excel falso). `None` vuelve al backend real de COM."""
    global _backend
    _backend = backend


def _import_com() -> tuple[Any, Any]:
    """Importa pywin32 de forma DIFERIDA: una PC sin el paquete (o sin Windows) no rompe el arranque de Vision; las herramientas dicen que falta."""
    import pythoncom  # noqa: PLC0415 -- diferido a proposito, ver el docstring
    import win32com.client  # noqa: PLC0415

    return pythoncom, win32com.client


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _parse_cell(ref: str) -> tuple[int, int]:
    m = _CELL.match(ref)
    if not m:
        raise errors.OfficeUserError(f"«{ref}» no es una celda válida. Escribí algo como A1 o B2.")
    col, row = _col_number(m.group(1)), int(m.group(2))
    if col > MAX_COL or row > MAX_ROW:
        raise errors.OfficeUserError(
            f"«{ref}» queda fuera de la hoja de Excel (llega hasta XFD1048576). Elegí una celda más cerca."
        )
    return col, row


def normalize_range(rng: str) -> tuple[str, int, int]:
    """Valida un rango simple (`A1` o `A1:B7`) y devuelve (rango normalizado, filas, columnas). Cualquier otra cosa (una formula, otra hoja, texto) se rechaza ANTES de tocar Excel."""
    text = (rng or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z]{1,3}[0-9]{1,7}(?::[A-Za-z]{1,3}[0-9]{1,7})?", text):
        raise errors.OfficeUserError(
            f"«{text}» no es un rango válido. Escribí una celda (A1) o un rango (A1:C10), sin nombre de hoja ni fórmulas."
        )
    if ":" in text:
        a, b = text.split(":")
    else:
        a = b = text
    c1, r1 = _parse_cell(a)
    c2, r2 = _parse_cell(b)
    c1, c2, r1, r2 = min(c1, c2), max(c1, c2), min(r1, r2), max(r1, r2)
    return text.upper(), r2 - r1 + 1, c2 - c1 + 1


def _in_com_thread(work: Callable[[], Any], timeout: float) -> Any:
    """Corre `work` en un hilo propio con COM inicializado y espera como maximo `timeout` segundos. El hilo es daemon: si Excel se cuelga, el plazo corta y el hilo no impide que Vision cierre."""
    box: dict[str, Any] = {}

    def runner() -> None:
        pythoncom = None
        try:
            try:
                pythoncom, _ = _import_com()
            except ImportError as exc:
                box["error"] = errors.OfficeUnavailable(str(exc))
                return
            pythoncom.CoInitialize()
            try:
                box["value"] = work()
            finally:
                pythoncom.CoUninitialize()
        except errors.OfficeError as exc:
            box["error"] = exc
        except BaseException as exc:  # noqa: BLE001 -- cualquier falla de COM se traduce; nada crudo llega a la persona
            box["error"] = (
                errors.from_com_error(exc)
                if type(exc).__name__ == "com_error"
                else errors.OfficeError(errors.describe(exc))
            )
            logger.warning("Office: fallo una llamada COM (%s)", errors.technical_detail(exc))

    thread = threading.Thread(target=runner, name="office-com", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        logger.warning("Office: una llamada COM supero %.0f s", timeout)
        raise errors.OfficeTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class _ComBackend:
    """Backend real: se engancha al Excel que YA esta corriendo. Se crea y se usa dentro del hilo COM de cada llamada."""

    def __init__(self) -> None:
        _, client = _import_com()
        try:
            self._app = client.GetActiveObject("Excel.Application")  # NO `Dispatch`: eso lanzaria Excel
        except Exception as exc:  # noqa: BLE001
            raise (
                errors.from_com_error(exc, "Excel")
                if type(exc).__name__ == "com_error"
                else errors.OfficeNotRunning("Excel")
            ) from exc

    def excel_books(self) -> list[dict[str, Any]]:
        return [{"name": str(wb.Name), "sheets": [str(ws.Name) for ws in wb.Worksheets]} for wb in self._app.Workbooks]

    def _sheet(self, book: str, sheet: str) -> Any:
        return self._app.Workbooks(book).Worksheets(sheet)

    def excel_read(self, book: str, sheet: str, rng: str) -> list[list[Any]]:
        value = self._sheet(book, sheet).Range(rng).Value2
        if not isinstance(value, tuple):
            return [[value]]
        return [[cell for cell in row] if isinstance(row, tuple) else [row] for row in value]

    def excel_write(self, book: str, sheet: str, rng: str, values: list[list[Any]]) -> None:
        # `Formula` acepta texto, numeros y formulas (los que empiezan con «=»). Se asigna una matriz para escribir todo en UNA llamada.
        self._sheet(book, sheet).Range(rng).Formula = tuple(
            tuple("" if v is None else v for v in row) for row in values
        )


def _with_backend(work: Callable[[OfficeBackend], Any], timeout: float) -> Any:
    """Ejecuta `work(backend)` con el backend inyectado (tests) o con el real dentro de un hilo COM."""
    if _backend is not None:
        injected = _backend
        return _in_injected_thread(lambda: work(injected), timeout)
    return _in_com_thread(lambda: work(_ComBackend()), timeout)


def _in_injected_thread(work: Callable[[], Any], timeout: float) -> Any:
    """Igual que `_in_com_thread` pero SIN COM (el backend inyectado es un objeto de Python): conserva el plazo y la traduccion de errores, asi los tests prueban lo mismo."""
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = work()
        except errors.OfficeError as exc:
            box["error"] = exc
        except BaseException as exc:  # noqa: BLE001
            box["error"] = errors.OfficeError(errors.describe(exc))

    thread = threading.Thread(target=runner, name="office-injected", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise errors.OfficeTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _pick_book(backend: OfficeBackend, book: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Elige el libro entre los que Excel YA tiene abiertos. Sin nombre: el unico abierto, o pide elegir. Un nombre que no esta abierto se rechaza (nunca se abre desde una ruta)."""
    books = backend.excel_books()
    if not books:
        raise errors.OfficeUserError(
            "Excel no tiene ningún libro abierto. Abrí el libro que querés usar y volvé a pedirlo."
        )
    names = [b["name"] for b in books]
    if book is None or not str(book).strip():
        if len(names) == 1:
            return names[0], books
        raise errors.OfficeUserError(
            f"Hay varios libros abiertos ({', '.join(names)}). Decime con cuál querés trabajar."
        )
    wanted = str(book).strip()
    # Solo el NOMBRE: una ruta (con barras o con unidad) no se abre desde aca; se compara con el nombre del archivo.
    if "/" in wanted or "\\" in wanted or ":" in wanted:
        tail = re.split(r"[\\/]", wanted)[-1]
        raise errors.OfficeUserError(
            f"Solo trabajo con libros que ya están abiertos en Excel: abrí «{tail}» en Excel y pedime su nombre ({', '.join(names)})."
        )
    for n in names:
        if n.lower() == wanted.lower():
            return n, books
    raise errors.OfficeUserError(
        f"El libro «{wanted}» no está abierto en Excel. Los abiertos son: {', '.join(names)}. Abrí el que necesitás y volvé a pedirlo."
    )


def _pick_sheet(book_info: dict[str, Any], sheet: str | None) -> str:
    sheets = book_info["sheets"]
    if sheet is None or not str(sheet).strip():
        if len(sheets) == 1:
            return sheets[0]
        raise errors.OfficeUserError(
            f"El libro «{book_info['name']}» tiene varias hojas ({', '.join(sheets)}). Decime cuál usar."
        )
    for s in sheets:
        if s.lower() == str(sheet).strip().lower():
            return s
    raise errors.OfficeUserError(
        f"La hoja «{sheet}» no existe en «{book_info['name']}». Las hojas son: {', '.join(sheets)}."
    )


def _check_values(values: Any, rows: int, cols: int) -> list[list[Any]]:
    """`values` tiene que ser una tabla (lista de filas) de texto, numeros, verdadero/falso o vacios, del mismo tamano que el rango."""
    if not isinstance(values, list) or not values or not all(isinstance(r, list) and r for r in values):
        raise errors.OfficeUserError(
            "Los valores a escribir tienen que ser una tabla: una lista de filas, cada una con sus celdas. Por ejemplo [[1, 2], [3, 4]]."
        )
    for row in values:
        for v in row:
            if v is not None and not isinstance(v, (str, int, float, bool)):
                raise errors.OfficeUserError(
                    "Cada celda tiene que ser texto, un número o una fórmula. No puedo escribir listas ni objetos dentro de una celda."
                )
    if len(values) * max(len(r) for r in values) > MAX_CELLS_WRITE:
        raise errors.OfficeUserError(
            f"Son demasiadas celdas para escribir de una vez (máximo {MAX_CELLS_WRITE}). Pedime una parte por vez."
        )
    if len(values) != rows or any(len(r) != cols for r in values):
        raise errors.OfficeUserError(
            f"El tamaño de los valores ({len(values)} filas) no coincide con el rango ({rows} filas por {cols} columnas). Ajustá el rango o los valores."
        )
    return values


def list_workbooks(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Los libros que Excel tiene abiertos, con sus hojas."""
    return _with_backend(lambda b: b.excel_books(), timeout)


def read_range(
    book: str | None, sheet: str | None, rng: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Lee un rango del libro abierto. Devuelve los valores y nombra libro, hoja y rango."""
    normalized, rows, cols = normalize_range(rng)
    if rows * cols > MAX_CELLS_READ:
        raise errors.OfficeUserError(
            f"Son demasiadas celdas para leer de una vez ({rows * cols}; el máximo es {MAX_CELLS_READ}). Pedime un rango más chico."
        )

    def work(backend: OfficeBackend) -> dict[str, Any]:
        name, books = _pick_book(backend, book)
        info = next(b for b in books if b["name"] == name)
        sheet_name = _pick_sheet(info, sheet)
        return {
            "book": name,
            "sheet": sheet_name,
            "range": normalized,
            "values": backend.excel_read(name, sheet_name, normalized),
        }

    return _with_backend(work, timeout)


def write_range(
    book: str | None, sheet: str | None, rng: str, values: Any, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Escribe valores o formulas en el libro abierto y VUELVE A LEER lo que quedo. Nunca guarda el libro. `matches` dice si lo leido coincide con lo escrito."""
    normalized, rows, cols = normalize_range(rng)
    checked = _check_values(values, rows, cols)

    def work(backend: OfficeBackend) -> dict[str, Any]:
        name, books = _pick_book(backend, book)
        info = next(b for b in books if b["name"] == name)
        sheet_name = _pick_sheet(info, sheet)
        backend.excel_write(name, sheet_name, normalized, checked)
        reread = backend.excel_read(name, sheet_name, normalized)
        return {
            "book": name,
            "sheet": sheet_name,
            "range": normalized,
            "written": checked,
            "reread": reread,
            "matches": _same(checked, reread),
            "saved": False,  # decision del usuario: nunca se guarda solo
        }

    return _with_backend(work, timeout)


def _same(written: list[list[Any]], reread: list[list[Any]]) -> bool:
    """Lo escrito y lo leido coinciden. Una formula (`=A1+1`) se evalua y vuelve como numero: eso NO es una diferencia, asi que solo se comparan las celdas que no son formulas."""
    flat_w = [v for row in written for v in row]
    flat_r = [v for row in reread for v in row]
    if len(flat_w) != len(flat_r):
        return False
    for w, r in zip(flat_w, flat_r, strict=True):
        if isinstance(w, str) and w.startswith("="):
            continue
        if _norm(w) != _norm(r):
            return False
    return True


def _norm(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
