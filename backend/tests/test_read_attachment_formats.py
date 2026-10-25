"""spec adjuntar-archivos-chat REQ-005 (tool `read` sobre PDF, DOCX y binarios)
y hallazgo QA-033 (CSV/TXT exportados de SAP o Excel en español).

Tests de aceptacion escritos ANTES de la implementacion, desde la spec y el
contrato observable de `read` (path/offset/limit y el parametro nuevo `pages`).
REQ-005 y QA-033 estan IMPLEMENTADOS (`app/attachment_readers.py`): los xfail
strict se retiraron y quedan como regresion permanente. Los guardianes del final
fijan que UTF-8 y CSV con coma se leen exactamente como antes (spec, "Criterio
de no regresion").

Los archivos se arman aca mismo en `tmp_path`: el PDF se escribe byte a byte
(fuente estandar Helvetica, sin dependencias) y el DOCX con python-docx."""

from __future__ import annotations

import asyncio
import inspect
import re
import zipfile

import pytest

from app.tools import filesystem as fs

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def read(args: dict) -> str:
    """Llama a la tool `read` como la llama el registry (sync o async)."""
    result = fs.read_tool.execute(args, None)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return str(result)


def _pdf_with_text(pages: list[str]) -> bytes:
    """PDF minimo valido con una linea de texto real por pagina (Helvetica,
    WinAnsi): cualquier extractor de texto de PDF la encuentra."""
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    for pid, text in zip(page_ids, pages, strict=True):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        objs[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {pid + 1} 0 R >>"
        ).encode()
        objs[pid + 1] = (
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objs[oid] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for oid in range(1, size):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# Fixtures validas (sin esto, un xfail podria "fallar por el fixture" y no por
# la falta de implementacion)
# ---------------------------------------------------------------------------
def test_the_pdf_fixture_has_real_extractable_text(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    f = tmp_path / "control.pdf"
    f.write_bytes(_pdf_with_text(["Inventario del deposito 2026", "Pagina dos 7L1Z-1234"]))

    reader = pypdf.PdfReader(str(f))

    assert len(reader.pages) == 2
    assert "Inventario del deposito 2026" in reader.pages[0].extract_text()
    assert "7L1Z-1234" in reader.pages[1].extract_text()


# ---------------------------------------------------------------------------
# REQ-005 a: PDF digital
# ---------------------------------------------------------------------------
def test_read_pdf_returns_its_text_with_page_marks(tmp_path):
    f = tmp_path / "reporte.pdf"
    f.write_bytes(_pdf_with_text(["Inventario del deposito 2026", "Pagina dos 7L1Z-1234"]))

    out = read({"path": str(f)})

    assert "Inventario del deposito 2026" in out and "7L1Z-1234" in out
    assert re.search(r"P[aá]gina\s*1\b", out) and re.search(r"P[aá]gina\s*2\b", out)
    assert "%PDF" not in out and not _CONTROL_CHARS.search(out)  # nada de binario crudo


def test_read_pdf_pages_parameter_returns_only_the_requested_pages(tmp_path):
    f = tmp_path / "largo.pdf"
    f.write_bytes(_pdf_with_text(["Primera hoja AAA", "Segunda hoja BBB", "Tercera hoja CCC"]))

    out = read({"path": str(f), "pages": "2"})

    assert "Segunda hoja BBB" in out
    assert "Primera hoja AAA" not in out and "Tercera hoja CCC" not in out


# ---------------------------------------------------------------------------
# REQ-005 b: DOCX con tablas en el orden del documento
# ---------------------------------------------------------------------------
def test_read_docx_returns_paragraphs_and_tables_in_document_order(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Informe de recepción del depósito")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Material"
    table.cell(0, 1).text = "Cantidad"
    table.cell(1, 0).text = "7L1Z-1234"
    table.cell(1, 1).text = "12"
    doc.add_paragraph("Fin del informe")
    f = tmp_path / "informe.docx"
    doc.save(str(f))

    out = read({"path": str(f)})

    assert "Informe de recepción del depósito" in out
    assert out.index("Informe de recepción") < out.index("7L1Z-1234") < out.index("Fin del informe")
    assert "Cantidad" in out and not _CONTROL_CHARS.search(out)


# ---------------------------------------------------------------------------
# REQ-005 e: binario desconocido -> aviso explicito, nunca mojibake
# ---------------------------------------------------------------------------
def _zip_file(tmp_path):
    f = tmp_path / "paquete.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("datos.csv", "a,b\n1,2\n")
    return f


def _exe_file(tmp_path):
    f = tmp_path / "herramienta.exe"
    f.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + bytes(range(256)) * 4)
    return f


@pytest.mark.parametrize("make", [_zip_file, _exe_file], ids=["zip", "exe"])
def test_read_binary_files_return_an_explicit_notice_without_garbage(tmp_path, make):
    f = make(tmp_path)

    out = read({"path": str(f)})

    assert "binario" in out.lower()
    assert not _CONTROL_CHARS.search(out)
    assert "�" not in out  # sin caracteres de reemplazo (mojibake)


# ---------------------------------------------------------------------------
# REQ-005 c/d + QA-033: exports de SAP/Excel en español
# ---------------------------------------------------------------------------
SAP_CSV = (
    "Material;Descripción;Cantidad\n"
    "7L1Z-1234;Paragolpe delantero;12\n"
    "7L1Z-5678;Óptica izquierda;3\n"
)


def test_read_sap_style_csv_with_semicolons_in_cp1252_splits_columns(tmp_path):
    f = tmp_path / "stock_sap.csv"
    f.write_bytes(SAP_CSV.encode("cp1252"))

    out = read({"path": str(f)})

    assert "2 filas x 3 columnas" in out
    assert "Descripción" in out and "Óptica izquierda" in out


def test_read_semicolon_csv_in_utf8_splits_columns(tmp_path):
    f = tmp_path / "stock_utf8.csv"
    f.write_text(SAP_CSV, encoding="utf-8")

    out = read({"path": str(f)})

    assert "2 filas x 3 columnas" in out


def test_read_ansi_text_file_keeps_its_accents(tmp_path):
    f = tmp_path / "notas.txt"
    f.write_bytes("Recepción de camión: 3 pallets dañados\n".encode("cp1252"))

    out = read({"path": str(f)})

    assert out == "1: Recepción de camión: 3 pallets dañados"


# ---------------------------------------------------------------------------
# Guardianes de NO REGRESION (pasan hoy y tienen que seguir pasando)
# ---------------------------------------------------------------------------
def test_utf8_comma_csv_is_read_exactly_as_today(tmp_path):
    f = tmp_path / "comas.csv"
    f.write_text("Material,Cantidad\nA-1,12\nB-2,3\n", encoding="utf-8")

    out = read({"path": str(f)})

    assert out.splitlines()[:3] == [
        "Archivo: comas.csv (2 filas x 2 columnas)",
        "Columnas: Material (str), Cantidad (int64)",
        "Filas 1-2 de 2:",
    ]
    assert "A-1" in out and "B-2" in out


def test_utf8_text_file_is_read_exactly_as_today(tmp_path):
    f = tmp_path / "utf8.txt"
    f.write_text("Recepción de camión\nsegunda línea\n", encoding="utf-8")

    assert read({"path": str(f)}) == "1: Recepción de camión\n2: segunda línea"
