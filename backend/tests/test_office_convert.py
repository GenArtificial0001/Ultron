"""Conversion de documentos de Office y exportacion a PDF (spec office-com-automatizacion REQ-004, REQ-012, REQ-010).

Se prueba contra un Word/Excel FALSO (un backend inyectable): la suite no abre Office de nadie. Lo que se vigila:
1. El archivo nuevo queda en `exports/` de la carpeta de proyecto de la SESION; el ORIGINAL no se modifica (ni su contenido ni su fecha) y se informa la ruta.
2. NUNCA se pisa un archivo que ya existe en `exports/`: si el nombre esta usado se agrega un numero.
3. Solo se convierte lo que se puede (`.doc`/`.docx`/`.xls`/`.xlsx`/`.ppt`/`.pptx` a su formato nuevo o a PDF); lo demas se rechaza ANTES de abrir Office.
4. El origen se valida: tiene que existir, ser un archivo y no ser enorme; no se sigue un enlace ni se sale de la carpeta.
5. La carpeta `exports/` se crea BAJO DEMANDA al guardar (no por adelantado: lo vigila `test_persistence_service.py`).
6. Sin carpeta de proyecto en la sesion, no se adivina un destino: se pide asignarla.
7. Office cerrado o ausente: frase clara; la conversion de un archivo NO exige que Excel/Word este abierto (se abre una instancia propia y se cierra), pero SI exige pywin32."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.office import convert, errors


class FakeConverter:
    """Word/Excel/PowerPoint falsos: 'convierten' escribiendo un archivo en el destino y registran lo que se les pidio."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None
        self.opened_read_only: list[bool] = []

    def convert(self, source: Path, target: Path, kind: str, fmt: str) -> None:
        self.calls.append((source, target, kind, fmt))
        if self.fail_with:
            raise self.fail_with
        target.write_bytes(b"CONTENIDO-CONVERTIDO")


@pytest.fixture
def converter():
    fake = FakeConverter()
    convert.set_backend(fake)
    yield fake
    convert.set_backend(None)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proyecto"
    root.mkdir()
    return root


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "docs" / "Informe.doc"
    src.parent.mkdir()
    src.write_bytes(b"ORIGINAL-INTACTO")
    return src


def _run(source, project, fmt="docx", **kw):
    return convert.convert_file(str(source), fmt, project_root=project, **kw)


# ------------------------------------------------------------------------------------------------------------------ resultado
def test_the_converted_file_lands_in_exports_of_the_session_project_and_the_path_is_reported(
    converter, source, project
):
    out = _run(source, project)

    target = Path(out["path"])
    assert target.parent == project / "exports" and target.name == "Informe.docx"
    assert target.read_bytes() == b"CONTENIDO-CONVERTIDO"
    assert out["source"] == "Informe.doc" and out["format"] == "docx"


def test_the_original_is_never_modified(converter, source, project):
    before = (source.read_bytes(), source.stat().st_mtime_ns)

    _run(source, project)

    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_the_original_is_never_the_target_even_when_the_format_is_the_same(converter, tmp_path, project):
    docx = tmp_path / "Informe.docx"
    docx.write_bytes(b"ORIGINAL")

    out = _run(docx, project, "pdf")

    assert Path(out["path"]) != docx and Path(out["path"]).parent == project / "exports"
    assert docx.read_bytes() == b"ORIGINAL"


def test_a_name_already_in_exports_is_never_overwritten_a_number_is_added(converter, source, project):
    first = Path(_run(source, project)["path"])
    second = Path(_run(source, project)["path"])
    third = Path(_run(source, project)["path"])

    assert first.name == "Informe.docx" and second.name == "Informe (2).docx" and third.name == "Informe (3).docx"
    assert first.read_bytes() == b"CONTENIDO-CONVERTIDO"


def test_a_file_the_person_already_has_in_exports_is_not_touched(converter, source, project):
    (project / "exports").mkdir()
    precious = project / "exports" / "Informe.docx"
    precious.write_bytes(b"LO-QUE-YA-TENIA")

    out = _run(source, project)

    assert precious.read_bytes() == b"LO-QUE-YA-TENIA"
    assert Path(out["path"]).name == "Informe (2).docx"


def test_exports_is_created_on_demand_only_when_something_is_saved(converter, source, project):
    assert not (project / "exports").exists()

    _run(source, project)

    assert (project / "exports").is_dir()


def test_a_failed_conversion_does_not_create_exports_nor_leave_a_partial_file(converter, source, project):
    converter.fail_with = errors.OfficeBusy("Word")

    with pytest.raises(errors.OfficeBusy):
        _run(source, project)

    assert not any((project / "exports").glob("*")) if (project / "exports").exists() else True


def test_a_conversion_that_produced_no_file_is_reported_not_taken_for_success(source, project):
    class Silent:
        def convert(self, source, target, kind, fmt):
            pass  # Office dijo que si pero no escribio nada

    convert.set_backend(Silent())
    try:
        with pytest.raises(errors.OfficeUserError) as caught:
            _run(source, project)
    finally:
        convert.set_backend(None)

    assert "no generó el archivo convertido" in str(caught.value) and "Informe.doc" in str(caught.value), (
        "tiene que decir que no se genero y a que archivo se refiere"
    )


# ------------------------------------------------------------------------------------------------------------------ formatos
@pytest.mark.parametrize(
    ("name", "fmt", "kind", "ext"),
    [
        ("a.doc", "docx", "word", ".docx"),
        ("a.doc", "pdf", "word", ".pdf"),
        ("a.docx", "pdf", "word", ".pdf"),
        ("a.xls", "xlsx", "excel", ".xlsx"),
        ("a.xls", "pdf", "excel", ".pdf"),
        ("a.xlsx", "pdf", "excel", ".pdf"),
        ("a.ppt", "pptx", "powerpoint", ".pptx"),
        ("a.pptx", "pdf", "powerpoint", ".pdf"),
        ("A.DOC", "DOCX", "word", ".docx"),
    ],
)
def test_each_supported_conversion_uses_the_right_application_and_extension(
    converter, tmp_path, project, name, fmt, kind, ext
):
    src = tmp_path / name
    src.write_bytes(b"x")

    out = _run(src, project, fmt)

    assert converter.calls[-1][2] == kind and Path(out["path"]).suffix == ext


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("a.doc", "xlsx"),
        ("a.xls", "docx"),
        ("a.xls", "pptx"),
        ("a.txt", "pdf"),
        ("a.exe", "pdf"),
        ("a.pdf", "docx"),
        ("a.doc", "html"),
        ("a.doc", ""),
        ("sin_extension", "pdf"),
        ("a.docm", "pdf"),
    ],
)
def test_a_conversion_that_makes_no_sense_is_rejected_before_opening_office(converter, tmp_path, project, name, fmt):
    src = tmp_path / name
    src.write_bytes(b"x")

    with pytest.raises(errors.OfficeUserError):
        _run(src, project, fmt)

    assert converter.calls == []


def test_the_rejection_lists_what_can_be_converted(converter, tmp_path, project):
    src = tmp_path / "a.exe"
    src.write_bytes(b"x")

    with pytest.raises(errors.OfficeUserError) as caught:
        _run(src, project, "pdf")

    assert ".doc" in str(caught.value) and ".xls" in str(caught.value)


# ------------------------------------------------------------------------------------------------------------------ origen
def test_a_source_that_does_not_exist_is_reported_with_its_name(converter, tmp_path, project):
    with pytest.raises(errors.OfficeUserError) as caught:
        _run(tmp_path / "No existe.doc", project)

    assert "No existe.doc" in str(caught.value) and converter.calls == []


def test_a_folder_is_not_a_source(converter, tmp_path, project):
    folder = tmp_path / "Carpeta.doc"
    folder.mkdir()

    with pytest.raises(errors.OfficeUserError):
        _run(folder, project)

    assert converter.calls == []


def test_a_huge_source_is_rejected(converter, tmp_path, project, monkeypatch):
    src = tmp_path / "Enorme.doc"
    src.write_bytes(b"x")
    monkeypatch.setattr(convert, "MAX_SOURCE_BYTES", 0)

    with pytest.raises(errors.OfficeUserError) as caught:
        _run(src, project)

    assert "grande" in str(caught.value).lower() and converter.calls == []


def test_a_relative_or_empty_source_is_rejected(converter, project):
    for bad in ("", "   ", "Informe.doc", "../Informe.doc", "~/Informe.doc"):
        with pytest.raises(errors.OfficeUserError):
            convert.convert_file(bad, "docx", project_root=project)

    assert converter.calls == []


@pytest.mark.parametrize("bad", ["a|b.doc", "a?.doc", "a*.doc", "\0.doc"])
def test_a_source_name_with_characters_windows_does_not_allow_is_rejected(converter, tmp_path, project, bad):
    with pytest.raises(errors.OfficeUserError):
        _run(tmp_path / bad, project)

    assert converter.calls == []


# ------------------------------------------------------------------------------------------------------------------ destino
def test_without_a_project_folder_it_asks_to_assign_one_instead_of_guessing_a_place(converter, source):
    with pytest.raises(errors.OfficeUserError) as caught:
        convert.convert_file(str(source), "docx", project_root=None)

    assert "carpeta" in str(caught.value).lower() and converter.calls == []


def test_the_name_of_the_new_file_comes_from_the_source_name_and_is_made_safe(converter, tmp_path, project):
    src = tmp_path / "Informe  final. v2 .doc"
    src.write_bytes(b"x")

    out = _run(src, project)

    name = Path(out["path"]).name
    assert name.endswith(".docx") and "/" not in name and "\\" not in name and not name.startswith(".")


def test_a_reserved_windows_name_is_not_used_for_the_new_file(converter, tmp_path, project):
    src = tmp_path / "CON.doc"
    src.write_bytes(b"x")

    out = _run(src, project)

    assert Path(out["path"]).stem.upper() != "CON"


def test_the_download_link_is_built_from_the_session_and_the_file_name(converter, source, project):
    out = _run(source, project, session_id="s-123")

    assert out["download_url"] == "/api/sessions/s-123/exports/Informe.docx"


# ------------------------------------------------------------------------------------------------------------------ Office ausente
def test_without_pywin32_the_error_names_what_is_missing(source, project, monkeypatch):
    convert.set_backend(None)
    monkeypatch.setattr(
        convert, "_import_com", lambda: (_ for _ in ()).throw(ImportError("No module named 'win32com'"))
    )

    with pytest.raises(errors.OfficeUnavailable):
        _run(source, project)

    assert not (project / "exports").exists() or not any((project / "exports").glob("*"))


def test_an_office_error_during_the_conversion_keeps_its_friendly_message(converter, source, project):
    converter.fail_with = errors.OfficeBusy("Word")

    with pytest.raises(errors.OfficeBusy) as caught:
        _run(source, project)

    assert "Word" in errors.describe(caught.value).message


# ------------------------------------------------------------------------------------------------------------------
# Tests que antes NO discriminaban (hallazgo por mutacion): el rechazo ocurria igual por otro camino, pero con un mensaje equivocado
# ------------------------------------------------------------------------------------------------------------------
def test_a_source_that_does_not_exist_says_it_cannot_find_it_not_that_it_is_a_folder(converter, tmp_path, project):
    with pytest.raises(errors.OfficeUserError) as caught:
        _run(tmp_path / "No existe.doc", project)

    assert "No encuentro el archivo" in str(caught.value)


@pytest.mark.parametrize("how", ["relative", "dotdot"])
def test_a_relative_or_dotdot_source_that_really_exists_is_still_rejected(
    converter, tmp_path, project, monkeypatch, how
):
    """Si el archivo EXISTE, solo la regla de la ruta lo frena: un `..` dejaria escapar a otra carpeta."""
    (tmp_path / "Informe.doc").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    text = "Informe.doc" if how == "relative" else str(tmp_path / "sub" / ".." / "Informe.doc")

    with pytest.raises(errors.OfficeUserError) as caught:
        convert.convert_file(text, "docx", project_root=project)

    assert "ruta completa" in str(caught.value).lower()
    assert converter.calls == []


@pytest.mark.parametrize("bad", ["a|b.doc", "a?.doc", "a*.doc"])
def test_a_source_name_with_forbidden_characters_gets_the_message_that_says_so(converter, tmp_path, project, bad):
    with pytest.raises(errors.OfficeUserError) as caught:
        _run(tmp_path / bad, project)

    assert "caracteres que Windows no permite" in str(caught.value)


def test_a_conversion_that_fails_after_writing_leaves_no_partial_file_in_exports(source, project):
    """Office alcanza a escribir el archivo y despues falla: lo escrito no puede quedar en `exports/` tomado por un resultado."""

    class WritesThenFails:
        def convert(self, source, target, kind, fmt):
            target.write_bytes(b"A-MEDIAS")
            raise errors.OfficeBusy("Word")

    convert.set_backend(WritesThenFails())
    try:
        with pytest.raises(errors.OfficeBusy):
            _run(source, project)
    finally:
        convert.set_backend(None)

    assert list((project / "exports").glob("*")) == []


def test_a_conversion_that_takes_longer_than_the_limit_is_cut_with_a_friendly_message(source, project):
    import time

    class Slow:
        def convert(self, source, target, kind, fmt):
            time.sleep(2)

    convert.set_backend(Slow())
    started = time.monotonic()
    try:
        with pytest.raises(errors.OfficeTimeout):
            _run(source, project, timeout=0.3)
    finally:
        convert.set_backend(None)

    assert time.monotonic() - started < 1.5, "el plazo corta: no se espera a que Office termine"


def test_the_new_file_name_is_cleaned_of_trailing_spaces_and_dots(converter, tmp_path, project):
    src = tmp_path / "Informe  final. v2 .doc"
    src.write_bytes(b"x")

    out = _run(src, project)

    assert Path(out["path"]).stem == "Informe  final. v2", "Windows no admite espacios ni puntos al final de un nombre"
