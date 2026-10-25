"""spec adjuntar-archivos-chat REQ-003 c, REQ-009 (capa de datos) y REQ-010:
parts `file` en SQLite -- display, historial del LLM, export, busqueda,
tombstones y compactacion. Todo en `tmp_path`."""

from __future__ import annotations

import sqlite3

import pytest

from app.persistence import db, transcript

NOW = 1_700_000_000.0


@pytest.fixture
def path(tmp_path):
    return tmp_path / "proyecto" / "session" / "vision.db"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proyecto"
    (root / "attachment").mkdir(parents=True)
    return root


def file_data(name="reporte.pdf", **over):
    d = {
        "name": name,
        "path": f"attachment/{name}",
        "size": 2_411_724,
        "mime": "application/pdf",
        "kind": "pdf",
        "inplace": False,
        "deleted": False,
    }
    d.update(over)
    return d


def add(path, mid, role="user", text=None, parts=(), sid="s1", at=NOW):
    db.ensure_session(path, sid, at)
    db.insert_message(path, mid, sid, role, at)
    if text is not None:
        db.insert_part(path, f"{mid}-t", mid, sid, "text", {"text": text}, at)
    for i, (ptype, data) in enumerate(parts):
        db.insert_part(path, f"{mid}-p{i}", mid, sid, ptype, data, at)


# ---------------------------------------------------------------------------
# REQ-010 a: display
# ---------------------------------------------------------------------------
def test_display_returns_attachments_with_exists_computed_now(path, project):
    (project / "attachment" / "reporte.pdf").write_bytes(b"%PDF")
    add(
        path,
        "m1",
        text="mirá esto",
        parts=[("file", file_data("reporte.pdf")), ("file", file_data("falta.xlsx"))],
    )

    [msg] = db.load_messages_for_display(path, "s1", project)

    assert msg["text"] == "mirá esto"
    by_name = {a["name"]: a for a in msg["attachments"]}
    assert by_name["reporte.pdf"]["exists"] is True
    assert by_name["falta.xlsx"]["exists"] is False  # "no encontrado", sin romper
    assert set(by_name["reporte.pdf"]) >= {
        "name",
        "path",
        "size",
        "mime",
        "inplace",
        "exists",
        "deleted",
    }


def test_display_includes_messages_that_only_have_attachments(path, project):
    # Hoy un mensaje sin texto se descarta (`if not text: continue`).
    add(path, "m1", text="", parts=[("file", file_data())])
    add(path, "m2", text=None, parts=[("file", file_data("b.xlsx"))])

    msgs = db.load_messages_for_display(path, "s1", project)

    assert [m["messageId"] for m in msgs] == ["m1", "m2"]
    assert all(m["text"] == "" and m["attachments"] for m in msgs)


def test_display_still_skips_empty_messages_without_attachments(path, project):
    add(path, "m1", role="assistant", text="")
    assert db.load_messages_for_display(path, "s1", project) == []


def test_display_has_no_attachments_key_for_old_messages(path, project):
    add(path, "m1", text="hola")
    [msg] = db.load_messages_for_display(path, "s1", project)
    assert "attachments" not in msg


def test_display_without_project_root_reports_missing_not_error(path):
    add(path, "m1", text="x", parts=[("file", file_data())])
    [msg] = db.load_messages_for_display(path, "s1")
    assert msg["attachments"][0]["exists"] is False


def test_display_inplace_absolute_path_is_checked_as_is(path, project, tmp_path):
    big = tmp_path / "enorme.csv"
    big.write_text("a,b\n")
    add(
        path,
        "m1",
        text="",
        parts=[("file", file_data("enorme.csv", path=str(big), inplace=True))],
    )
    [msg] = db.load_messages_for_display(path, "s1", project)
    assert (
        msg["attachments"][0]["exists"] is True
        and msg["attachments"][0]["inplace"] is True
    )


def test_display_tombstone_is_deleted_not_missing(path, project):
    add(path, "m1", text="x", parts=[("file", file_data(deleted=True))])
    [a] = db.load_messages_for_display(path, "s1", project)[0]["attachments"]
    assert a["deleted"] is True and a["exists"] is False


# ---------------------------------------------------------------------------
# REQ-003 c: el aviso se rearma para el LLM
# ---------------------------------------------------------------------------
def test_llm_history_rebuilds_the_notice_from_file_parts(path):
    add(path, "m1", text="analizalo", parts=[("file", file_data())])

    [msg] = db.load_messages(path, "s1")

    assert msg["role"] == "user"
    assert msg["content"].startswith(
        "analizalo\n\n[Archivos adjuntos en la carpeta de la sesión"
    )
    assert "- attachment/reporte.pdf (PDF, 2,3 MB)" in msg["content"]


def test_llm_history_with_only_attachments_has_just_the_notice(path):
    add(
        path,
        "m1",
        text="",
        parts=[("file", file_data("s.xlsx", kind="excel", size=1024))],
    )
    [msg] = db.load_messages(path, "s1")
    assert (
        msg["content"].startswith("[Archivos adjuntos") and "s.xlsx" in msg["content"]
    )


def test_llm_history_does_not_offer_deleted_files(path):
    add(path, "m1", text="hola", parts=[("file", file_data(deleted=True))])
    [msg] = db.load_messages(path, "s1")
    assert msg["content"] == "hola"  # sin aviso: no hay nada vigente para leer


def test_old_sessions_without_file_parts_rebuild_exactly_as_before(path):
    add(path, "m1", text="hola")
    add(path, "m2", role="assistant", text="chau")
    assert db.load_messages(path, "s1") == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "chau"},
    ]


def test_persisted_user_text_stays_clean(path):
    add(path, "m1", text="analizalo", parts=[("file", file_data())])
    [msg] = db.load_messages_for_display(path, "s1")
    assert msg["text"] == "analizalo" and "Archivos adjuntos" not in msg["text"]


def test_images_and_files_coexist_in_the_same_message(path):
    add(
        path,
        "m1",
        text="mirá",
        parts=[("image", {"url": "data:image/jpeg;base64,AAA"}), ("file", file_data())],
    )
    [msg] = db.load_messages(path, "s1")
    kinds = [b["type"] for b in msg["content"]]
    assert (
        kinds == ["text", "image_url"]
        and "attachment/reporte.pdf" in msg["content"][0]["text"]
    )


# ---------------------------------------------------------------------------
# REQ-009 c: imagen eliminada => texto, nunca image_url roto
# ---------------------------------------------------------------------------
def test_deleted_image_is_rebuilt_as_text_never_a_broken_image_url(path):
    add(
        path,
        "m1",
        text="mirá",
        parts=[("image", {"url": "data:image/jpeg;base64,AAA"})],
    )
    db.tombstone_image_part(path, "m1-p0", NOW + 1)

    [msg] = db.load_messages(path, "s1")

    assert isinstance(msg["content"], str)
    assert "[imagen eliminada]" in msg["content"] and "image_url" not in str(msg)


def test_one_of_two_images_deleted_keeps_the_other(path):
    add(
        path,
        "m1",
        text="dos",
        parts=[
            ("image", {"url": "data:image/jpeg;base64,AAA"}),
            ("image", {"url": "data:image/jpeg;base64,BBB"}),
        ],
    )
    db.tombstone_image_part(path, "m1-p0", NOW + 1)

    [msg] = db.load_messages(path, "s1")

    blocks = msg["content"]
    assert [b["type"] for b in blocks] == ["text", "image_url"]
    assert "[imagen eliminada]" in blocks[0]["text"] and blocks[1]["image_url"][
        "url"
    ].endswith("BBB")


# ---------------------------------------------------------------------------
# REQ-010 d / e: export y busqueda
# ---------------------------------------------------------------------------
def test_export_shows_a_placeholder_for_files():
    assert (
        transcript.format_part("file", file_data())
        == "*[archivo adjunto: attachment/reporte.pdf (2,3 MB)]*\n\n"
    )
    assert "(eliminado)" in transcript.format_part("file", file_data(deleted=True))


def test_search_finds_a_message_by_its_attachment_name(path):
    add(
        path, "m1", text="revisá esto", parts=[("file", file_data("stock_playas.xlsx"))]
    )
    add(path, "m2", text="otra cosa")

    hits = db.search_sessions(path, "stock_playas")

    assert len(hits) == 1 and hits[0]["id"] == "s1"


# ---------------------------------------------------------------------------
# REQ-009: consultas, tombstones, VACUUM
# ---------------------------------------------------------------------------
def test_list_file_parts_returns_ids_and_metadata(path):
    add(path, "m1", text="a", parts=[("file", file_data("a.pdf"))])
    add(path, "m2", text="b", parts=[("file", file_data("b.pdf"))], sid="s2")

    all_parts = db.list_file_parts(path)
    only_s1 = db.list_file_parts(path, "s1")

    assert {p["name"] for p in all_parts} == {"a.pdf", "b.pdf"}
    assert [p["name"] for p in only_s1] == ["a.pdf"]
    assert only_s1[0]["part_id"] == "m1-p0" and only_s1[0]["session_id"] == "s1"


def test_tombstone_file_part_keeps_metadata_and_marks_deleted(path):
    add(path, "m1", text="a", parts=[("file", file_data("a.pdf"))])

    db.tombstone_file_part(path, "m1-p0", NOW + 5)

    [p] = db.list_file_parts(path)
    assert p["deleted"] is True and p["deleted_at"] == NOW + 5 and p["name"] == "a.pdf"


def test_tombstone_of_an_unknown_part_is_a_noop(path):
    add(path, "m1", text="a")
    db.tombstone_file_part(path, "no-existe", NOW)  # no debe lanzar


def test_list_image_parts_reports_size_without_returning_the_base64(path):
    add(
        path,
        "m1",
        text="x",
        parts=[("image", {"url": "data:image/jpeg;base64," + "A" * 5000})],
    )
    [img] = db.list_image_parts(path, "s1")
    assert img["bytes"] > 5000 and "url" not in img and img["deleted"] is False
    db.tombstone_image_part(path, "m1-p0", NOW)
    assert db.list_image_parts(path, "s1")[0]["deleted"] is True


def test_compact_database_really_shrinks_the_file(path):
    big = "data:image/jpeg;base64," + "A" * 400_000
    add(path, "m1", text="x", parts=[("image", {"url": big}), ("image", {"url": big})])
    before = db.database_size(path)

    db.tombstone_image_part(path, "m1-p0", NOW)
    db.tombstone_image_part(path, "m1-p1", NOW)
    db.compact_database(path)

    after = db.database_size(path)
    assert after < before - 500_000  # sin VACUUM el archivo no achica


def test_compact_database_on_a_missing_db_is_a_noop(tmp_path):
    db.compact_database(tmp_path / "no-existe.db")  # no debe lanzar


def test_database_size_counts_wal(path):
    add(path, "m1", text="x")
    assert db.database_size(path) > 0
    conn = sqlite3.connect(path)
    conn.close()
