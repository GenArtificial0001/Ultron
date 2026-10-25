"""spec adjuntar-archivos-chat: reglas puras de `app/attachments.py`
(REQ-002 c/d, REQ-003, REQ-004, REQ-008 c)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app import attachments as at


# ---------------------------------------------------------------------------
# REQ-002 c: nombre saneado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reporte.xlsx", "reporte.xlsx"),
        ("C:\\Users\\x\\Desktop\\reporte.xlsx", "reporte.xlsx"),  # descarta la ruta
        ("../../etc/passwd", "passwd"),
        ('a<b>c:d"e|f?g*.txt', "a_b_c_d_e_f_g_.txt"),
        ("informe.pdf. ", "informe.pdf"),  # puntos y espacios finales
        ("   ", "archivo"),
        ("", "archivo"),
        ("...", "archivo"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("COM1.csv", "_COM1.csv"),
        ("LPT9", "_LPT9"),
        ("concon.txt", "concon.txt"),  # solo el nombre EXACTO es reservado
        ("con\x00trol.txt", "con_trol.txt"),
    ],
)
def test_sanitize_name(raw, expected):
    assert at.sanitize_name(raw) == expected


def test_sanitize_name_caps_length_and_keeps_extension():
    out = at.sanitize_name("a" * 400 + ".xlsx")
    assert len(out) <= at.MAX_NAME_LEN and out.endswith(".xlsx")


def test_sanitized_name_has_no_path_separators():
    for raw in ("..\\..\\x.txt", "/abs/x.txt", "a/b/c.txt", "..", "."):
        out = at.sanitize_name(raw)
        assert "/" not in out and "\\" not in out and out not in ("..", ".")


# ---------------------------------------------------------------------------
# REQ-002 d/e: nombre unico exclusivo + .part
# ---------------------------------------------------------------------------
def test_open_exclusive_disambiguates_and_never_overwrites(tmp_path):
    a, fa = at.open_exclusive(tmp_path, "reporte.xlsx")
    fa.close()
    b, fb = at.open_exclusive(tmp_path, "reporte.xlsx")
    fb.close()
    c, fc = at.open_exclusive(tmp_path, "reporte.xlsx")
    fc.close()

    assert [a.name, b.name, c.name] == [
        "reporte.xlsx",
        "reporte (1).xlsx",
        "reporte (2).xlsx",
    ]


def test_open_exclusive_writes_to_a_part_file_not_the_final_one(tmp_path):
    final, f = at.open_exclusive(tmp_path, "datos.csv")
    f.write(b"a,b\n")
    f.close()

    assert (
        at.part_path_for(final).exists()
        and at.part_path_for(final).name == ".datos.csv.part"
    )
    assert final.read_bytes() == b""  # el final esta reservado y vacio hasta el rename


def test_open_exclusive_is_safe_under_concurrency(tmp_path):
    names: list[str] = []
    lock = threading.Lock()

    def worker():
        final, f = at.open_exclusive(tmp_path, "mismo.txt")
        f.close()
        with lock:
            names.append(final.name)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(names)) == 12  # ninguno se pisó


def test_open_exclusive_creates_the_folder(tmp_path):
    final, f = at.open_exclusive(tmp_path / "attachment", "x.txt")
    f.close()
    assert final.parent.is_dir()


# ---------------------------------------------------------------------------
# REQ-004 b / d / e / f
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.xlsx", "excel"),
        ("a.XLSM", "excel"),
        ("a.csv", "csv"),
        ("a.parquet", "csv"),
        ("a.pdf", "pdf"),
        ("a.docx", "word"),
        ("a.py", "text"),
        ("a.env", "text"),
        ("a.zip", "archive"),
        ("a.xyz", "file"),
        ("sinext", "file"),
    ],
)
def test_kind_for(name, kind):
    assert at.kind_for(name) == kind


@pytest.mark.parametrize(
    "name",
    [
        "a.exe",
        "a.BAT",
        "a.cmd",
        "a.ps1",
        "a.vbs",
        "a.js",
        "a.msi",
        "a.com",
        "a.scr",
        "a.lnk",
        "a.hta",
        "a.wsf",
        "a.jar",
    ],
)
def test_executables_are_never_opened(name):
    assert at.is_executable_name(name)


@pytest.mark.parametrize("name", ["a.xlsx", "a.pdf", "a.txt", "a.docx", "a.csv"])
def test_documents_are_not_executables(name):
    assert not at.is_executable_name(name)


@pytest.mark.parametrize("name", [".env", "prod.env", "server.pem", "id.key"])
def test_secret_looking_files_are_flagged(name):
    assert at.is_secret_name(name)


def test_regular_files_are_not_flagged_as_secret():
    assert not at.is_secret_name("reporte.xlsx")


# ---------------------------------------------------------------------------
# REQ-003: aviso
# ---------------------------------------------------------------------------
def test_notice_matches_the_spec_format():
    out = at.build_notice(
        [
            {
                "name": "reporte.pdf",
                "path": "attachment/reporte.pdf",
                "kind": "pdf",
                "size": 2_411_724,
                "pages": 14,
            },
            {"name": "stock.xlsx", "path": "attachment/stock.xlsx", "size": 870_400},
            {
                "name": "export_sap.csv",
                "path": "C:\\Datos\\export_sap.csv",
                "size": int(1.2 * 1024**3),
                "inplace": True,
            },
        ]
    )
    assert out.splitlines() == [
        "[Archivos adjuntos en la carpeta de la sesión — leelos con la tool `read` usando estas rutas]",
        "- attachment/reporte.pdf (PDF, 2,3 MB, 14 páginas)",
        "- attachment/stock.xlsx (Excel, 850,0 KB)",
        "- C:\\Datos\\export_sap.csv (CSV, 1,2 GB, se lee en su lugar)",
    ]


def test_notice_is_empty_without_attachments_and_skips_deleted():
    assert at.build_notice([]) == ""
    assert at.build_notice([{"name": "x.pdf", "deleted": True}]) == ""
    out = at.build_notice(
        [{"name": "x.pdf", "deleted": True}, {"name": "y.pdf", "size": 10}]
    )
    assert "y.pdf" in out and "x.pdf" not in out


def test_notice_warns_when_the_file_is_huge_in_tokens():
    out = at.build_notice([{"name": "g.csv", "size": 10, "token_estimate": 250_000}])
    assert "≈ 250000 tokens: leelo por partes" in out
    small = at.build_notice([{"name": "g.csv", "size": 10, "token_estimate": 5_000}])
    assert "leelo por partes" not in small


def test_with_notice_keeps_user_text_and_handles_empty_text():
    atts = [{"name": "x.pdf", "size": 1}]
    assert at.with_notice("mirá esto", atts).startswith(
        "mirá esto\n\n[Archivos adjuntos"
    )
    assert at.with_notice("", atts).startswith("[Archivos adjuntos")
    assert at.with_notice("hola", []) == "hola"


def test_autoname_uses_the_first_file_name():
    assert at.autoname_from([{"name": "stock.xlsx"}, {"name": "b.pdf"}]) == "stock.xlsx"
    assert at.autoname_from([]) == ""


# ---------------------------------------------------------------------------
# Tokens (REQ-008 c)
# ---------------------------------------------------------------------------
def test_format_tokens():
    assert at.format_tokens(850) == "≈ 850 tokens"
    assert at.format_tokens(35_400) == "≈ 35 k tokens"


def test_token_estimate_of_a_small_text_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x" * 4000, encoding="utf-8")
    assert at.estimate_tokens_for_file(f) == 1000


def test_token_estimate_extrapolates_big_files_from_a_sample(tmp_path):
    f = tmp_path / "grande.csv"
    f.write_text(("a,b,c\n" * 200_000), encoding="utf-8")  # ~1.2 MB
    est = at.estimate_tokens_for_file(f)
    assert 250_000 < est <= 350_000  # 1.2M bytes / 4 = 300k


def test_token_estimate_is_zero_for_archives_and_binaries(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    assert at.estimate_tokens_for_file(z) == 0


def test_token_estimate_for_pdf_and_excel_uses_size(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF" + b"\x00" * 4000)
    assert at.estimate_tokens_for_file(f) > 0


# ---------------------------------------------------------------------------
# Validacion de rutas
# ---------------------------------------------------------------------------
def test_resolve_attachment_path_accepts_only_inside_attachment(tmp_path):
    root = tmp_path / "proj"
    (root / "attachment").mkdir(parents=True)
    ok = root / "attachment" / "a.pdf"
    ok.write_text("x")
    other = root / "otra" / "b.pdf"
    other.parent.mkdir()
    other.write_text("x")

    assert at.resolve_attachment_path(root, "attachment/a.pdf") == ok.resolve()
    assert at.resolve_attachment_path(root, str(ok)) == ok.resolve()
    assert at.resolve_attachment_path(root, "otra/b.pdf") is None
    assert at.resolve_attachment_path(root, str(other)) is None


@pytest.mark.parametrize(
    "bad",
    [
        "../secreto.txt",
        "attachment/../../secreto.txt",
        "attachment/..\\..\\x",
        "",
        "a\x00b",
    ],
)
def test_resolve_attachment_path_blocks_traversal(tmp_path, bad):
    root = tmp_path / "proj"
    (root / "attachment").mkdir(parents=True)
    (tmp_path / "secreto.txt").write_text("x")
    assert at.resolve_attachment_path(root, bad) is None


def test_resolve_attachment_path_allows_registered_inplace_only(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    big = tmp_path / "enorme.csv"
    big.write_text("x")

    assert at.resolve_attachment_path(root, str(big)) is None
    assert (
        at.resolve_attachment_path(root, str(big), {str(big.resolve())})
        == big.resolve()
    )


def test_is_within_follows_real_paths(tmp_path):
    base = tmp_path / "a"
    base.mkdir()
    assert at.is_within(base, base / "x")
    assert not at.is_within(base, tmp_path / "b")
    assert not at.is_within(base, base / ".." / "b")


def test_is_onedrive_path():
    assert at.is_onedrive_path(Path("C:/Users/x/OneDrive - la empresa/proyecto"))
    assert not at.is_onedrive_path(Path("C:/Proyectos/x"))
