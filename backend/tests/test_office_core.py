"""Nucleo de la automatizacion de Office (spec office-com-automatizacion REQ-002, REQ-003, REQ-010).

Se prueba contra un Office FALSO (`FakeOffice`, un backend inyectable, mismo patron que `audio_control.set_backend`): la suite no abre Excel ni Word de nadie. Lo que se vigila:
1. Excel: listar libros y hojas, leer un rango y devolver lo que hay; escribir valores y formulas y VOLVER A LEER para informar lo que quedo (no un «ok» a ciegas).
2. NUNCA se guarda un libro por su cuenta; NUNCA se lanza Excel ni se abre un libro que no estaba abierto.
3. Excel cerrado, sin libros, un dialogo abierto o pywin32 ausente: frases claras para la persona, sin colgarse (plazo de 15 s) y sin romper nada.
4. Un rango invalido, enorme o de una hoja inexistente se rechaza antes de tocar Excel.
5. Un libro no se identifica por una ruta arbitraria: solo por los que Excel ya tiene abiertos."""

from __future__ import annotations

import time

import pytest

from app.office import core, errors


class FakeOffice:
    """Excel falso: libros abiertos con hojas y celdas en memoria. Registra todo lo que se le pide (para saber que NO se guardo ni se lanzo nada)."""

    def __init__(self):
        self.running = True
        self.books = {
            "Stock.xlsx": {
                "Hoja1": {"A1": "Parte", "B1": "Cant", "A2": "P-100", "B2": 5, "A3": "P-200", "B3": 7},
                "Resumen": {"A1": "Total"},
            },
        }
        self.calls: list[tuple] = []
        self.saved = False
        self.launched = False
        self.dialog_open = False
        self.delay = 0.0

    def _check(self):
        self.calls.append(("check",))
        if self.delay:
            time.sleep(self.delay)
        if not self.running:
            raise errors.OfficeNotRunning("Excel")
        if self.dialog_open:
            raise errors.OfficeBusy("Excel")

    def excel_books(self):
        self._check()
        return [{"name": n, "sheets": list(s)} for n, s in self.books.items()]

    def excel_read(self, book, sheet, rng):
        self._check()
        self.calls.append(("read", book, sheet, rng))
        cells = self.books[book][sheet]
        return [[cells.get(ref)] for ref in _expand(rng)]

    def excel_write(self, book, sheet, rng, values):
        self._check()
        self.calls.append(("write", book, sheet, rng))
        cells = self.books[book][sheet]
        for ref, row in zip(_expand(rng), [v for r in values for v in r], strict=False):
            cells[ref] = row

    def save(self, *a, **k):  # si alguien lo llama, la prueba lo detecta
        self.saved = True

    def launch(self, *a, **k):
        self.launched = True


def _expand(rng):
    """A1:B3 -> ['A1','B1','A2','B2','A3','B3'] (solo lo que usan los tests)."""
    if ":" not in rng:
        return [rng]
    a, b = rng.split(":")
    c1, r1, c2, r2 = a[0], int(a[1:]), b[0], int(b[1:])
    return [f"{chr(c)}{r}" for r in range(r1, r2 + 1) for c in range(ord(c1), ord(c2) + 1)]


@pytest.fixture
def office():
    fake = FakeOffice()
    core.set_backend(fake)
    yield fake
    core.set_backend(None)


# ------------------------------------------------------------------------------------------------------------------ lectura
def test_listing_the_open_books_names_each_workbook_and_its_sheets(office):
    out = core.list_workbooks()

    assert out == [{"name": "Stock.xlsx", "sheets": ["Hoja1", "Resumen"]}]


def test_reading_a_range_returns_the_values_and_names_book_and_sheet(office):
    out = core.read_range("Stock.xlsx", "Hoja1", "A1:B3")

    assert out["book"] == "Stock.xlsx" and out["sheet"] == "Hoja1" and out["range"] == "A1:B3"
    # El Excel falso devuelve una celda por fila en el orden A1,B1,A2,B2,A3,B3. Lo que se comprueba es que llegan TODAS, con su contenido real (no «algo»).
    assert out["values"] == [["Parte"], ["Cant"], ["P-100"], [5], ["P-200"], [7]]


def test_the_default_book_is_the_only_open_one_so_the_person_does_not_have_to_name_it(office):
    out = core.read_range(None, "Hoja1", "A1")

    assert out["book"] == "Stock.xlsx"


def test_with_two_books_open_and_none_named_it_asks_which_instead_of_guessing(office):
    office.books["Otro.xlsx"] = {"Hoja1": {"A1": "x"}}

    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range(None, "Hoja1", "A1")

    assert "Stock.xlsx" in str(caught.value) and "Otro.xlsx" in str(caught.value)


# ------------------------------------------------------------------------------------------------------------------ escritura
def test_writing_rereads_the_cells_and_reports_what_is_really_there(office):
    out = core.write_range("Stock.xlsx", "Hoja1", "B2", [[99]])

    assert out["written"] == [[99]] and out["reread"] == [[99]]
    assert out["matches"] is True
    assert ("write", "Stock.xlsx", "Hoja1", "B2") in office.calls and (
        "read",
        "Stock.xlsx",
        "Hoja1",
        "B2",
    ) in office.calls


def test_writing_never_saves_the_workbook_on_its_own(office):
    core.write_range("Stock.xlsx", "Hoja1", "B2", [[99]])

    assert office.saved is False


def test_a_difference_between_what_was_written_and_what_was_reread_is_reported_not_hidden(office):
    original = office.excel_write

    def corrupting(book, sheet, rng, values):
        original(book, sheet, rng, [["otra cosa"]])

    office.excel_write = corrupting

    out = core.write_range("Stock.xlsx", "Hoja1", "B2", [[99]])

    assert out["matches"] is False, "si Excel dejo otra cosa, hay que decirlo"
    assert out["reread"] == [["otra cosa"]]


# ------------------------------------------------------------------------------------------------------------------ no lanzar, no abrir
def test_with_excel_closed_nothing_is_launched_and_the_person_is_told_to_open_it(office):
    office.running = False

    with pytest.raises(errors.OfficeNotRunning) as caught:
        core.list_workbooks()

    assert office.launched is False
    assert "Excel" in errors.describe(caught.value).message and "abrí" in errors.describe(caught.value).message.lower()


def test_a_book_that_is_not_open_is_never_opened_from_a_path(office):
    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range("C:/Users/otro/Secreto.xlsx", "Hoja1", "A1")

    assert "abierto" in str(caught.value).lower()
    assert office.launched is False


def test_excel_with_no_open_books_says_so(office):
    office.books.clear()

    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range(None, "Hoja1", "A1")

    assert "libro" in str(caught.value).lower()


def test_a_sheet_that_does_not_exist_lists_the_ones_that_do(office):
    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range("Stock.xlsx", "Inexistente", "A1")

    assert "Hoja1" in str(caught.value) and "Resumen" in str(caught.value)


# ------------------------------------------------------------------------------------------------------------------ validaciones
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "A",
        "1",
        "ZZZZ99999999",
        "A1:",
        ":B2",
        "A1;B2",
        "A1:B2:C3",
        "=SUM(A1)",
        "A1:B2'",
        "Hoja1!A1",
        "A0",
        "A1048577",
    ],
)
def test_a_range_that_is_not_a_plain_range_is_rejected_before_touching_excel(office, bad):
    with pytest.raises(errors.OfficeUserError):
        core.read_range("Stock.xlsx", "Hoja1", bad)

    assert ("read", "Stock.xlsx", "Hoja1", bad) not in office.calls


def test_a_huge_range_is_rejected_so_the_model_does_not_pull_a_million_cells(office):
    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range("Stock.xlsx", "Hoja1", "A1:Z100000")

    assert "celdas" in str(caught.value).lower()


def test_writing_more_values_than_the_limit_is_rejected(office):
    with pytest.raises(errors.OfficeUserError):
        core.write_range("Stock.xlsx", "Hoja1", "A1:B2", [[1] * 50 for _ in range(50)])

    assert not any(c[0] == "write" for c in office.calls)


def test_the_values_to_write_must_be_a_table_of_text_numbers_or_formulas(office):
    for bad in ("hola", [1, 2], [[{"a": 1}]], [[object()]], None):
        with pytest.raises(errors.OfficeUserError):
            core.write_range("Stock.xlsx", "Hoja1", "A1", bad)  # type: ignore[arg-type]

    assert not any(c[0] == "write" for c in office.calls)


def test_the_size_of_the_values_must_match_the_range(office):
    with pytest.raises(errors.OfficeUserError) as caught:
        core.write_range("Stock.xlsx", "Hoja1", "A1:B2", [[1, 2, 3]])

    assert "rango" in str(caught.value).lower()


# ------------------------------------------------------------------------------------------------------------------ plazo y errores
def test_a_call_that_takes_longer_than_the_limit_is_cut_with_a_friendly_message(office):
    office.delay = 2.0

    with pytest.raises(errors.OfficeTimeout) as caught:
        core.list_workbooks(timeout=0.3)

    assert "15" in errors.describe(caught.value).message or "tard" in errors.describe(caught.value).message.lower()


def test_excel_with_an_open_dialog_is_reported_as_busy_not_as_a_crash(office):
    office.dialog_open = True

    with pytest.raises(errors.OfficeBusy) as caught:
        core.list_workbooks()

    assert (
        "cuadro" in errors.describe(caught.value).message.lower()
        or "ocupad" in errors.describe(caught.value).message.lower()
    )


def test_without_pywin32_the_error_says_what_is_missing_and_nothing_else_breaks(monkeypatch):
    core.set_backend(None)
    monkeypatch.setattr(core, "_import_com", lambda: (_ for _ in ()).throw(ImportError("No module named 'win32com'")))

    with pytest.raises(errors.OfficeUnavailable) as caught:
        core.list_workbooks()

    assert (
        "pywin32" in errors.describe(caught.value).message.lower()
        or "office" in errors.describe(caught.value).message.lower()
    )
    assert (
        "ImportError" not in errors.describe(caught.value).message
        and "win32com" not in errors.describe(caught.value).message
    )


def test_the_error_messages_never_carry_internal_text(office):
    from tests.test_friendly_errors import _ACTION, _JARGON

    for exc in (
        errors.OfficeNotRunning("Excel"),
        errors.OfficeBusy("Excel"),
        errors.OfficeTimeout(),
        errors.OfficeUnavailable("falta pywin32"),
    ):
        message = errors.describe(exc).message
        assert not _JARGON.search(message), message
        assert _ACTION.search(message), message


def test_an_inverted_range_and_lowercase_are_normalized_before_reaching_excel(office):
    out = core.read_range("Stock.xlsx", "Hoja1", "b2:a1")

    assert out["range"] == "B2:A1".upper()
    assert any(c[0] == "read" and c[3] == "B2:A1" for c in office.calls)


def test_the_book_name_is_matched_ignoring_case_and_returns_the_real_name(office):
    out = core.read_range("stock.XLSX", "hoja1", "A1")

    assert out["book"] == "Stock.xlsx" and out["sheet"] == "Hoja1"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\x\Stock.xlsx",
        "C:/Users/x/Stock.xlsx",
        "D:Stock.xlsx",
        r"\\servidor\compartida\Stock.xlsx",
        "../Stock.xlsx",
    ],
)
def test_a_path_is_never_used_to_open_a_workbook_even_when_it_ends_in_an_open_name(office, path):
    """Aunque el archivo se llame igual que uno abierto, una RUTA no se acepta: Vision solo trabaja con lo que la persona ya abrio en Excel."""
    with pytest.raises(errors.OfficeUserError) as caught:
        core.read_range(path, "Hoja1", "A1")

    assert "Stock.xlsx" in str(caught.value)
    assert not any(c[0] == "read" for c in office.calls)


def test_the_write_limit_counts_cells_not_rows(office):
    with pytest.raises(errors.OfficeUserError):
        core.write_range("Stock.xlsx", "Hoja1", "A1:A501", [[1] for _ in range(501)])


def test_a_formula_that_excel_evaluated_is_not_reported_as_a_difference(office):
    original = office.excel_write

    def evaluating(book, sheet, rng, values):
        original(book, sheet, rng, [[12]])  # Excel guardo la formula y al leerla devuelve el RESULTADO

    office.excel_write = evaluating

    out = core.write_range("Stock.xlsx", "Hoja1", "C1", [["=A1+B1"]])

    assert out["matches"] is True, "una formula evaluada no es una diferencia"
