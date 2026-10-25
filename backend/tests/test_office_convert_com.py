"""El convertidor COM REAL, con un Word/Excel/PowerPoint FALSOS (spec office-com-automatizacion REQ-004, REQ-010).

`test_office_convert.py` prueba las reglas con un convertidor inyectado, que no pasa por el codigo que corre en la PC de cada persona. Aca se reemplaza solo `convert._import_com`, de modo que SI se ejecuta
`_ComConverter`. Lo que se vigila:
1. Se abre una instancia PROPIA e invisible (`DispatchEx`); jamas se engancha al Word o Excel que la persona tiene abierto (`GetActiveObject`) ni se le cierra.
2. El original se abre de SOLO LECTURA y se escribe la copia en otro lado: el original no cambia.
3. Sin cuadros de dialogo (`DisplayAlerts = False`): un dialogo dejaria la llamada colgada.
4. La instancia propia se cierra SIEMPRE (`Quit`), aunque la conversion falle.
5. Cada par origen/destino usa su codigo de formato de Office."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.office import convert, errors


class FakeDoc:
    def __init__(self, app):
        self.app = app

    def SaveAs2(self, target, FileFormat):  # noqa: N802, N803 -- los nombres son los de COM
        self.app.saved.append((target, FileFormat))
        if self.app.fail_save:
            raise self.app.fail_save
        Path(target).write_bytes(b"WORD-CONVERTIDO")

    SaveAs = SaveAs2

    def Close(self, *args):  # noqa: N802
        self.app.closed_docs += 1


class FakeApp:
    def __init__(self, fail_save=None):
        self.Visible = True
        self.DisplayAlerts = True
        self.saved: list[tuple] = []
        self.open_kwargs: dict = {}
        self.closed_docs = 0
        self.quit_called = 0
        self.fail_save = fail_save
        outer = self

        class Collection:
            def Open(self, path, *a, **kw):  # noqa: N802
                outer.open_kwargs = {"path": path, **kw}
                return FakeBook(outer) if outer.kind == "excel" else FakeDoc(outer)

        self.Documents = self.Workbooks = self.Presentations = Collection()
        self.kind = "word"

    def Quit(self):  # noqa: N802
        self.quit_called += 1


class FakeBook(FakeDoc):
    def ExportAsFixedFormat(self, code, target):  # noqa: N802
        self.app.saved.append((target, ("pdf", code)))
        Path(target).write_bytes(b"PDF-CONVERTIDO")


class FakeCom:
    def __init__(self, app):
        self.app = app
        self.dispatch_ex: list[str] = []
        self.get_active: list[str] = []
        self.initialized = self.uninitialized = 0
        outer = self

        class PyCom:
            @staticmethod
            def CoInitialize():  # noqa: N802
                outer.initialized += 1

            @staticmethod
            def CoUninitialize():  # noqa: N802
                outer.uninitialized += 1

        class Client:
            @staticmethod
            def DispatchEx(prog):  # noqa: N802
                outer.dispatch_ex.append(prog)
                return outer.app

            @staticmethod
            def GetActiveObject(prog):  # noqa: N802
                outer.get_active.append(prog)  # engancharse al de la persona: NO debe pasar
                return outer.app

            @staticmethod
            def Dispatch(prog):  # noqa: N802
                outer.get_active.append("Dispatch:" + prog)
                return outer.app

        self.pythoncom, self.client = PyCom, Client

    def __call__(self):
        return self.pythoncom, self.client


@pytest.fixture
def setup(monkeypatch, tmp_path):
    convert.set_backend(None)  # el convertidor REAL de COM
    app = FakeApp()
    com = FakeCom(app)
    monkeypatch.setattr(convert, "_import_com", com)
    project = tmp_path / "proyecto"
    project.mkdir()

    def make(name, fmt):
        src = tmp_path / name
        src.write_bytes(b"ORIGINAL")
        app.kind = convert._SOURCES[src.suffix.lower()][0]
        return convert.convert_file(str(src), fmt, project_root=project), src

    yield app, com, make
    convert.set_backend(None)


def test_it_opens_its_own_invisible_instance_and_never_attaches_to_the_one_the_person_uses(setup):
    app, com, make = setup

    make("a.doc", "docx")

    assert com.dispatch_ex == ["Word.Application"], "una instancia PROPIA"
    assert com.get_active == [], "jamas se engancha al Word que la persona tiene abierto (ni se lo cerraria)"
    assert app.Visible is False


def test_it_never_shows_dialogs_that_would_hang_the_call(setup):
    app, _, make = setup

    make("a.doc", "docx")

    assert app.DisplayAlerts is False


def test_the_original_is_opened_read_only_and_the_copy_goes_elsewhere(setup):
    app, _, make = setup

    out, src = make("a.doc", "docx")

    assert app.open_kwargs["ReadOnly"] is True, "el original se abre de solo lectura"
    assert app.saved[0][0] != str(src), "la copia nunca se escribe sobre el original"
    assert src.read_bytes() == b"ORIGINAL"


@pytest.mark.parametrize(("name", "fmt"), [("a.doc", "docx"), ("a.xls", "xlsx"), ("a.xls", "pdf"), ("a.ppt", "pptx")])
def test_the_original_is_opened_read_only_in_every_application_not_just_word(setup, name, fmt):
    """Word, Excel y PowerPoint se abren por caminos distintos: la regla «el original no se toca» tiene que cumplirse en los TRES (antes solo se verificaba Word)."""
    app, _, make = setup

    make(name, fmt)

    assert app.open_kwargs["ReadOnly"] is True, f"{name}: el original se abrio para escritura"


def test_its_own_instance_is_closed_after_a_successful_conversion(setup):
    app, com, make = setup

    make("a.doc", "docx")

    assert app.quit_called == 1 and app.closed_docs == 1
    assert com.initialized == com.uninitialized == 1


def test_its_own_instance_is_closed_even_when_the_conversion_fails(setup):
    app, com, make = setup
    app.fail_save = RuntimeError("fallo interno con CONTENIDO-DEL-DOCUMENTO")

    with pytest.raises(errors.OfficeError) as caught:
        make("a.doc", "docx")

    assert app.quit_called == 1, (
        "un Word propio que queda abierto y oculto es un proceso huerfano en la PC de la persona"
    )
    assert app.closed_docs == 1
    assert com.initialized == com.uninitialized == 1
    assert "CONTENIDO-DEL-DOCUMENTO" not in errors.describe(caught.value).message


@pytest.mark.parametrize(
    ("name", "fmt", "prog", "code"),
    [
        ("a.doc", "docx", "Word.Application", 12),
        ("a.doc", "pdf", "Word.Application", 17),
        ("a.xls", "xlsx", "Excel.Application", 51),
        ("a.xls", "pdf", "Excel.Application", 0),
        ("a.ppt", "pptx", "PowerPoint.Application", 24),
        ("a.ppt", "pdf", "PowerPoint.Application", 32),
    ],
)
def test_each_pair_uses_its_application_and_its_office_format_code(setup, name, fmt, prog, code):
    app, com, make = setup

    out, _ = make(name, fmt)

    assert com.dispatch_ex == [prog]
    saved = app.saved[0][1]
    assert saved == code or saved == ("pdf", code), f"{name}->{fmt}: Office recibio {saved!r}, se esperaba {code}"
    assert Path(out["path"]).read_bytes() in (b"WORD-CONVERTIDO", b"PDF-CONVERTIDO") or Path(out["path"]).exists()


def test_a_com_error_during_the_conversion_is_translated_not_shown_raw(setup):
    app, _, make = setup
    exc = type("com_error", (Exception,), {})("detalle crudo con CONTENIDO-DEL-DOCUMENTO")
    exc.hresult = -2147418111
    app.fail_save = exc

    with pytest.raises(errors.OfficeBusy) as caught:
        make("a.doc", "docx")

    assert "CONTENIDO-DEL-DOCUMENTO" not in errors.describe(caught.value).message


def test_a_hung_office_is_cut_at_the_time_limit_and_the_thread_does_not_keep_vision_alive(monkeypatch, tmp_path):
    import threading
    import time

    convert.set_backend(None)
    release = threading.Event()

    class HangingApp(FakeApp):
        @property
        def Documents(self):  # noqa: N802
            release.wait(5)
            return None

        @Documents.setter
        def Documents(self, value):  # noqa: N802
            pass

    monkeypatch.setattr(convert, "_import_com", FakeCom(HangingApp()))
    src = tmp_path / "a.doc"
    src.write_bytes(b"x")
    project = tmp_path / "p"
    project.mkdir()
    started = time.monotonic()

    with pytest.raises(errors.OfficeTimeout):
        convert.convert_file(str(src), "docx", project_root=project, timeout=0.4)

    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 3, "el plazo corta: no se espera a que Office responda"
    assert all(t.daemon for t in threading.enumerate() if t.name == "office-convert"), (
        "un hilo colgado no puede impedir que ULTRON cierre"
    )
