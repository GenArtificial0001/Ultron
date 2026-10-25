"""El backend COM REAL de Excel, con un `win32com` FALSO (spec office-com-automatizacion REQ-002, REQ-003, REQ-010).

`test_office_core.py` prueba las reglas con un Excel falso inyectado, que no pasa por el codigo que corre en la PC de cada persona. Aca se reemplaza solo `core._import_com` por un `pythoncom` y un `win32com.client`
de mentira, de modo que SI se ejecuta `_ComBackend` y el hilo COM real. Lo que se vigila:
1. Se engancha al Excel que YA corre (`GetActiveObject`); `Dispatch` lanzaria Excel y jamas se llama.
2. COM se inicializa y se libera en cada llamada, incluso si falla (no queda ningun objeto vivo entre llamadas).
3. Escribir manda UNA matriz por la propiedad `Formula` (`None` viaja como celda vacia) y nunca llama a `Save`.
4. Un Excel colgado se corta en el plazo, y el hilo es daemon: no impide que Vision cierre.
5. Un error de COM se traduce a una frase; ni el mensaje ni el detalle tecnico llevan contenido de celdas."""

from __future__ import annotations

import threading
import time

import pytest

from app.office import core, errors
from tests.test_office_core import FakeOffice


class FakeSheet:
    def __init__(self, name):
        self.Name = name
        self.formulas = []
        self.value2 = ((1.0, "x"), (2.0, None))

    def Range(self, rng):
        sheet = self

        class R:
            @property
            def Value2(self):
                return sheet.value2

            @property
            def Formula(self):
                return None

            @Formula.setter
            def Formula(self, v):
                sheet.formulas.append((rng, v))

        return R()


class _Seq(list):
    """Como las colecciones de COM: se recorren Y se llaman por nombre."""

    def __call__(self, name):
        return next(x for x in self if x.Name == name)


class FakeBook:
    def __init__(self, name, sheets):
        self.Name = name
        self.Worksheets = _Seq([FakeSheet(n) for n in sheets])
        self.saved = False

    def Save(self):
        self.saved = True


class FakeExcelApp:
    def __init__(self, books):
        self.Workbooks = _Seq(books)


def _com_error(hresult, text=""):
    exc = type("com_error", (Exception,), {})(text)
    exc.hresult = hresult
    return exc


class FakeCom:
    """Reemplaza a `core._import_com()`: devuelve un `pythoncom` y un `win32com.client` de mentira que registran lo que se les pidio."""

    def __init__(self, app=None, no_excel=False):
        self.app = app
        self.no_excel = no_excel
        self.dispatched = []
        self.initialized = 0
        self.uninitialized = 0
        outer = self

        class PyCom:
            @staticmethod
            def CoInitialize():
                outer.initialized += 1

            @staticmethod
            def CoUninitialize():
                outer.uninitialized += 1

        class Client:
            @staticmethod
            def GetActiveObject(prog):
                if outer.no_excel:
                    raise _com_error(-2147221021)
                return outer.app

            @staticmethod
            def Dispatch(prog):
                outer.dispatched.append(prog)  # lanzaria Excel: NO debe llamarse nunca
                return outer.app

        self.pythoncom, self.client = PyCom, Client

    def __call__(self):
        return self.pythoncom, self.client


@pytest.fixture
def com(monkeypatch):
    core.set_backend(None)  # el backend REAL de COM, no el inyectado
    fake = FakeCom(FakeExcelApp([FakeBook("Stock.xlsx", ["Hoja1", "Resumen"])]))
    monkeypatch.setattr(core, "_import_com", fake)
    yield fake
    core.set_backend(None)


def test_the_real_backend_attaches_to_the_running_excel_and_never_launches_one(com):
    core.list_workbooks()

    assert com.dispatched == [], "Dispatch lanzaria Excel si no estuviera abierto: solo se puede GetActiveObject"


def test_the_real_backend_lists_the_open_books_and_their_sheets(com):
    assert core.list_workbooks() == [{"name": "Stock.xlsx", "sheets": ["Hoja1", "Resumen"]}]


def test_com_is_initialized_and_released_around_each_call(com):
    core.list_workbooks()
    core.list_workbooks()

    assert com.initialized == 2 and com.uninitialized == 2, (
        "cada llamada inicializa y libera COM: no queda nada vivo entre llamadas"
    )


def test_with_no_excel_running_the_real_backend_asks_the_person_to_open_it_and_launches_nothing(monkeypatch):
    core.set_backend(None)
    fake = FakeCom(no_excel=True)
    monkeypatch.setattr(core, "_import_com", fake)

    with pytest.raises(errors.OfficeNotRunning):
        core.list_workbooks()

    assert fake.dispatched == []
    assert fake.initialized == fake.uninitialized == 1, "aunque falle, COM se libera"


def test_a_range_of_several_cells_comes_back_as_a_table_and_a_single_cell_as_a_one_cell_table(com):
    out = core.read_range("Stock.xlsx", "Hoja1", "A1:B2")
    assert out["values"] == [[1.0, "x"], [2.0, None]]

    com.app.Workbooks[0].Worksheets[0].value2 = 7  # Excel devuelve un valor suelto (no una tupla) para UNA celda
    assert core.read_range("Stock.xlsx", "Hoja1", "A1")["values"] == [[7]]


def test_writing_sends_one_matrix_through_the_formula_property_and_never_calls_save(com):
    core.write_range("Stock.xlsx", "Hoja1", "A1:B2", [[1, "=A1+1"], ["texto", None]])

    book = com.app.Workbooks[0]
    assert book.Worksheets[0].formulas == [("A1:B2", ((1, "=A1+1"), ("texto", "")))], (
        "una sola asignacion, y None viaja como celda vacia"
    )
    assert book.saved is False


def test_a_hung_excel_is_cut_at_the_time_limit_and_the_thread_does_not_keep_vision_alive(monkeypatch):
    core.set_backend(None)
    release = threading.Event()

    class HangingApp:
        @property
        def Workbooks(self):
            release.wait(5)  # Excel colgado con un cuadro de dialogo
            return []

    monkeypatch.setattr(core, "_import_com", FakeCom(HangingApp()))
    started = time.monotonic()

    with pytest.raises(errors.OfficeTimeout):
        core.list_workbooks(timeout=0.4)

    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 3, "el plazo corta: no se espera a que Excel responda"
    live = [t for t in threading.enumerate() if t.name == "office-com"]
    assert all(t.daemon for t in live), "un hilo colgado no puede impedir que Vision cierre"


def test_a_com_error_from_excel_is_translated_and_never_shown_raw(monkeypatch):
    core.set_backend(None)

    class BusyApp:
        @property
        def Workbooks(self):
            raise _com_error(
                -2147418111, "detalle interno con CONTENIDO-DE-CELDA"
            )  # Excel rechazo la llamada: esta ocupado

    monkeypatch.setattr(core, "_import_com", FakeCom(BusyApp()))

    with pytest.raises(errors.OfficeBusy) as caught:
        core.list_workbooks()

    assert "CONTENIDO-DE-CELDA" not in errors.describe(caught.value).message


def test_the_technical_detail_never_carries_cell_content():
    exc = _com_error(-2146827284, "el valor de la celda era CLAVE-SECRETA-123")

    assert "CLAVE-SECRETA-123" not in errors.technical_detail(exc)
    assert "hresult=-2146827284" in errors.technical_detail(exc)


def test_an_unknown_com_error_becomes_a_generic_friendly_failure_without_the_raw_text(monkeypatch):
    core.set_backend(None)

    class BrokenApp:
        @property
        def Workbooks(self):
            raise _com_error(-12345, "texto crudo con CONTENIDO-DE-CELDA")

    monkeypatch.setattr(core, "_import_com", FakeCom(BrokenApp()))

    with pytest.raises(errors.OfficeError) as caught:
        core.list_workbooks()

    message = errors.describe(caught.value).message
    assert "CONTENIDO-DE-CELDA" not in message and "inesperado" in message


def test_writing_reports_that_nothing_was_saved():
    fake = FakeOffice()
    core.set_backend(fake)
    try:
        out = core.write_range("Stock.xlsx", "Hoja1", "B2", [[99]])
    finally:
        core.set_backend(None)

    assert out["saved"] is False


@pytest.mark.parametrize("path", [r"C:\Users\x\Stock.xlsx", "C:/Users/x/Stock.xlsx", "../Stock.xlsx"])
def test_a_path_gets_its_own_clear_message_that_names_the_rule(path):
    """Aunque igual se rechazaria mas abajo (no coincide con un libro abierto), el mensaje tiene que explicar LA REGLA: solo se usan libros que ya estan abiertos en Excel."""
    core.set_backend(FakeOffice())
    try:
        with pytest.raises(errors.OfficeUserError) as caught:
            core.read_range(path, "Hoja1", "A1")
    finally:
        core.set_backend(None)

    assert "solo trabajo con libros que ya están abiertos" in str(caught.value).lower()


@pytest.mark.parametrize(
    ("hresult", "expected_code"),
    [
        (-2147221021, "OFFICE-NO-ABIERTO"),
        (-2147221005, "OFFICE-NO-ABIERTO"),
        (-2147418111, "OFFICE-OCUPADO"),
        (-2147417846, "OFFICE-OCUPADO"),
        (-2147417836, "OFFICE-OCUPADO"),
        (-2146827284, "OFFICE-PEDIDO"),
        (-1, "OFFICE-FALLO"),
    ],
)
def test_each_com_error_the_person_can_act_on_has_its_own_phrase(hresult, expected_code):
    """Cada HRESULT que significa algo concreto (Excel cerrado, ocupado, hoja protegida) tiene SU frase; el resto cae en un fallo generico. Una tabla mal copiada mandaria a la persona a hacer lo equivocado."""
    assert errors.from_com_error(_com_error(hresult)).friendly.code == expected_code


def test_a_protected_sheet_tells_the_person_what_to_check_in_excel():
    message = errors.from_com_error(_com_error(-2146827284)).friendly.message

    assert "protegida" in message and "Excel" in message


def test_the_error_code_can_come_in_the_first_argument_like_pywin32_often_does():
    exc = Exception(-2147221021, "Operacion no disponible")  # sin atributo hresult: el codigo viaja en args[0]

    assert errors.from_com_error(exc).friendly.code == "OFFICE-NO-ABIERTO"
