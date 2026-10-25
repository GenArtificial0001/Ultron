"""spec adjuntar-archivos-chat REQ-005/REQ-006: casos de borde de
`app/attachment_readers.py` (lo que los tests de aceptacion de `read` no cubren)."""

from __future__ import annotations

import re
import zipfile

import pytest

from app import attachment_readers as ar
from test_read_attachment_formats import _pdf_with_text, read

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------------------------------------------------------------------------
# format_size: es-AR
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0 B"),
        (900, "900 B"),
        (2048, "2,0 KB"),
        (2_411_724, "2,3 MB"),
        (5 * 1024**3, "5,0 GB"),
    ],
)
def test_format_size_uses_comma_decimal(n, text):
    assert ar.format_size(n) == text


# ---------------------------------------------------------------------------
# parse_pages
# ---------------------------------------------------------------------------
def test_parse_pages_defaults_to_the_first_twenty():
    assert ar.parse_pages(None, 50) == list(range(1, 21))
    assert ar.parse_pages("", 5) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("spec", "total", "expected"),
    [
        ("3", 10, [3]),
        ("1-5", 10, [1, 2, 3, 4, 5]),
        ("2,4-6", 10, [2, 4, 5, 6]),
        ("5-3", 10, [3, 4, 5]),  # rango invertido se normaliza
        ("8-20", 10, [8, 9, 10]),  # se acota al total
        ("2,2,2", 10, [2]),  # sin repetidos
        ("99", 10, []),
    ],
)
def test_parse_pages(spec, total, expected):
    assert ar.parse_pages(spec, total) == expected


@pytest.mark.parametrize("bad", ["abc", "1-", "-3", "1;2", "1..3"])
def test_parse_pages_rejects_garbage_with_a_friendly_message(bad):
    with pytest.raises(ValueError, match="rango de páginas"):
        ar.parse_pages(bad, 10)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def test_pdf_header_and_default_limit_note(tmp_path):
    f = tmp_path / "largo.pdf"
    f.write_bytes(
        _pdf_with_text([f"Hoja numero {i} contenido suficiente" for i in range(1, 26)])
    )

    out = read({"path": str(f)})

    assert out.startswith("PDF: largo.pdf — 25 páginas")
    assert "mostrando 20: 1-20" in out and "--- Página 20 ---" in out
    assert "--- Página 21 ---" not in out


def test_pdf_pages_out_of_range_reports_instead_of_failing(tmp_path):
    f = tmp_path / "corto.pdf"
    f.write_bytes(_pdf_with_text(["Una sola pagina con texto suficiente"]))
    assert "Ninguna de las páginas pedidas existe" in read(
        {"path": str(f), "pages": "9"}
    )


def test_pdf_invalid_pages_spec_is_a_friendly_error(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(_pdf_with_text(["Una sola pagina con texto suficiente"]))
    out = read({"path": str(f), "pages": "abc"})
    assert out.startswith("Error:") and "rango de páginas" in out


def test_pdf_offset_and_limit_are_lines_of_what_is_returned(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(
        _pdf_with_text(
            ["Primera pagina con texto de sobra", "Segunda pagina con texto de sobra"]
        )
    )
    out = read({"path": str(f), "limit": 1})
    assert "--- Página 1 ---" in out and "--- Página 2 ---" not in out


def test_broken_pdf_reports_without_raising(tmp_path):
    f = tmp_path / "roto.pdf"
    f.write_bytes(b"esto no es un pdf")
    out = read({"path": str(f)})
    assert out.startswith("Error:") and "PDF" in out


def test_encrypted_pdf_says_protected_and_does_not_raise(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(
        str(
            _write(
                tmp_path / "plano.pdf", _pdf_with_text(["Texto secreto del informe"])
            )
        )
    )
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="clave123")
    f = tmp_path / "cifrado.pdf"
    with f.open("wb") as fh:
        writer.write(fh)

    out = read({"path": str(f)})

    assert "protegido con contraseña" in out and "Texto secreto" not in out


def _write(path, data):
    path.write_bytes(data)
    return path


def test_scanned_pages_without_a_transcriber_are_reported_not_invented(tmp_path):
    f = tmp_path / "escaneado.pdf"
    f.write_bytes(_pdf_with_text(["", "Pagina con texto real suficiente para leer"]))

    out = ar.read_pdf(f)

    assert (
        "no tienen texto extraíble" in out
        and "1" in out.split("no tienen")[0].splitlines()[-1]
    )
    assert "Pagina con texto real suficiente" in out


def test_scanned_pages_are_delegated_to_the_transcriber(tmp_path):
    f = tmp_path / "mixto.pdf"
    f.write_bytes(_pdf_with_text(["", "Pagina con texto real suficiente para leer"]))
    seen = {}

    def fake(path, pages):
        seen["pages"] = pages
        return {
            1: "TEXTO TRANSCRIPTO POR VISION"
        }, "(transcripción por visión: puede tener errores)"

    out = ar.read_pdf(f, transcribe_pages=fake)

    assert seen["pages"] == [1]
    assert "TEXTO TRANSCRIPTO POR VISION" in out and "transcripción por visión" in out
    assert "no tienen texto extraíble" not in out


def test_pdf_progress_reports_page_k_of_n(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(
        _pdf_with_text(
            ["Pagina uno con texto de sobra", "Pagina dos con texto de sobra"]
        )
    )
    seen: list[int] = []

    ar.read_pdf(f, on_progress=seen.append)

    assert seen == [50, 100]


def test_new_formats_are_capped_at_60k_chars(tmp_path):
    f = tmp_path / "enorme.pdf"
    f.write_bytes(_pdf_with_text(["x" * 4000 + f" fin{i}" for i in range(1, 20)]))  # 19 x 4000 > 60000
    out = ar.read_pdf(f, limit=100_000)
    assert len(out) <= ar.MAX_NEW_FORMAT_CHARS + 200
    assert "resultado truncado" in out and "pages" in out


def test_page_has_text_threshold():
    assert not ar.page_has_text("   \n  ab  ")
    assert ar.page_has_text("a" * 20)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def test_broken_docx_reports_without_raising(tmp_path):
    f = tmp_path / "roto.docx"
    f.write_bytes(b"no es un docx")
    assert read({"path": str(f)}).startswith("Error:")


def test_docx_table_is_markdown_and_escapes_pipes(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "A|B"
    t.cell(0, 1).text = "Cant"
    t.cell(1, 0).text = "x"
    t.cell(1, 1).text = "1"
    f = tmp_path / "t.docx"
    doc.save(str(f))

    out = read({"path": str(f)})

    assert "| A\\|B | Cant |" in out and "| --- | --- |" in out


# ---------------------------------------------------------------------------
# Binarios y no soportados
# ---------------------------------------------------------------------------
def test_zip_lists_its_entries(tmp_path):
    f = tmp_path / "p.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("datos.csv", "a,b\n")
        z.writestr("sub/otro.txt", "hola")
    out = read({"path": str(f)})
    assert "Contiene 2 entradas" in out and "datos.csv" in out and "sub/otro.txt" in out
    assert not _CONTROL.search(out)


def test_zip_listing_is_capped_at_200(tmp_path):
    f = tmp_path / "grande.zip"
    with zipfile.ZipFile(f, "w") as z:
        for i in range(250):
            z.writestr(f"f{i}.txt", "x")
    out = read({"path": str(f)})
    assert "y 50 más" in out and out.count("\n- ") == 200


def test_corrupt_zip_says_so(tmp_path):
    f = tmp_path / "malo.zip"
    f.write_bytes(b"PK\x03\x04 basura")
    assert "dañado" in read({"path": str(f)})


def test_unknown_extension_with_nul_byte_is_binary(tmp_path):
    f = tmp_path / "raro.xyz"
    f.write_bytes(b"cabecera\x00\x01\x02datos")
    out = read({"path": str(f)})
    assert out.startswith("[binario] raro.xyz") and not _CONTROL.search(out)


def test_binary_notice_never_contains_control_or_replacement_chars(tmp_path):
    f = tmp_path / "a.exe"
    f.write_bytes(b"MZ\x90\x00" + bytes(range(256)))
    out = read({"path": str(f)})
    assert not _CONTROL.search(out) and "�" not in out


@pytest.mark.parametrize("ext", [".doc", ".ppt", ".pptx", ".msg", ".xlsb"])
def test_out_of_scope_formats_say_how_to_convert(tmp_path, ext):
    f = tmp_path / f"algo{ext}"
    f.write_bytes(b"\xd0\xcf\x11\xe0 contenido")
    out = read({"path": str(f)})
    assert out.startswith("[formato no soportado]") and "adjuntalo de nuevo" in out


# ---------------------------------------------------------------------------
# Codificacion y separador (QA-033)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sample", "sep"),
    [
        ("a,b,c\n1,2,3\n", ","),
        ("a;b;c\n1;2;3\n", ";"),
        ("a\tb\tc\n1\t2\t3\n", "\t"),
        ("a|b|c\n1|2|3\n", "|"),
        ("solo una columna\notra fila\n", ","),
        ("", ","),
        ('"a;b",c\n"1;2",3\n', ","),  # el ; entre comillas no cuenta
    ],
)
def test_detect_separator(sample, sep):
    assert ar.detect_separator(sample) == sep


def test_tsv_is_always_tab(tmp_path):
    f = tmp_path / "d.tsv"
    f.write_text("a;b\tc\n1;2\t3\n", encoding="utf-8")
    assert ar.csv_read_options(f, ".tsv")["sep"] == "\t"


def test_utf8_comma_csv_options_are_the_same_as_before(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    assert ar.csv_read_options(f, ".csv") == {"sep": ",", "encoding": "utf-8"}


def test_bom_is_stripped_from_text(tmp_path):
    f = tmp_path / "b.txt"
    f.write_bytes(b"\xef\xbb\xbfhola")
    assert ar.read_text_auto(f) == "hola"


def test_undecodable_bytes_never_raise(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"ok \x81\x8d\x8f\x90\x9d fin")  # bytes sin mapeo en cp1252
    assert "ok" in ar.read_text_auto(f)


def test_semicolon_csv_reads_columns_and_types(tmp_path):
    f = tmp_path / "s.csv"
    f.write_bytes("Material;Cantidad\nÁ-1;12\nB-2;3\n".encode("cp1252"))
    out = read({"path": str(f)})
    assert "2 filas x 2 columnas" in out and "Á-1" in out and "Cantidad (int64)" in out
