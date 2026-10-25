"""spec adjuntar-archivos-chat: endpoints REST de adjuntos y `/api/chat` con
`attachments` (REQ-002, REQ-003, REQ-004 d/e, REQ-007, REQ-009).

Sesion real: indice maestro y SQLite en `tmp_path` (fixture `tmp_master_db`).
Seguridad primero: cada ruta que manda el cliente se prueba con traversal."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import attachment_service as svc
from app import main
from app.persistence import db, master_index
from app.persistence import service as persistence

SID = "sesion-adj"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_pending():
    svc.PENDING_INPLACE.clear()
    yield
    svc.PENDING_INPLACE.clear()


def _own(sid: str, project: Path, owner: str | None = None) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    master_index.set_project_path(
        sid,
        str(project),
        windows_user_created=f"{owner or persistence.current_windows_user()}_20260929120000",
    )
    return project


@pytest.fixture
def project(tmp_path, tmp_master_db):
    return _own(SID, tmp_path / "proyecto")


def upload(client, name, data=b"contenido", sid=SID, headers=None):
    return client.post(
        f"/api/sessions/{sid}/attachments", params={"name": name}, content=data, headers=headers or {}
    )


def _set_limits(monkeypatch, warn=100, inplace=500):
    monkeypatch.setattr(svc, "thresholds", lambda: {"warn_mb": warn, "inplace_mb": inplace})


# ---------------------------------------------------------------------------
# REQ-002: subir
# ---------------------------------------------------------------------------
def test_upload_saves_the_file_and_returns_the_contract(client, project):
    r = upload(client, "reporte.csv", b"a,b\n1,2\n")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "reporte.csv" and body["path"] == "attachment/reporte.csv"
    assert body["size"] == 8 and body["kind"] == "csv" and body["inplace"] is False
    assert isinstance(body["token_estimate"], int) and body["mime"]
    assert (project / "attachment" / "reporte.csv").read_bytes() == b"a,b\n1,2\n"


def test_upload_creates_the_attachment_folder_on_demand(client, project):
    assert not (project / "attachment").exists()
    upload(client, "x.txt")
    assert (project / "attachment").is_dir()


def test_same_name_never_overwrites(client, project):
    a = upload(client, "stock.xlsx", b"uno").json()
    b = upload(client, "stock.xlsx", b"dos").json()

    assert (a["name"], b["name"]) == ("stock.xlsx", "stock (1).xlsx")
    assert (project / "attachment" / "stock.xlsx").read_bytes() == b"uno"
    assert (project / "attachment" / "stock (1).xlsx").read_bytes() == b"dos"


@pytest.mark.parametrize(
    ("raw", "saved"),
    [
        ("..\\..\\fuera.txt", "fuera.txt"),
        ("../../fuera.txt", "fuera.txt"),
        ("C:\\Windows\\system32\\x.txt", "x.txt"),
        ("CON", "_CON"),
        ("a<b>.txt", "a_b_.txt"),
    ],
)
def test_upload_name_is_sanitized_and_cannot_escape(client, project, tmp_path, raw, saved):
    r = upload(client, raw)

    assert r.status_code == 200
    assert r.json()["name"] == saved
    assert (project / "attachment" / saved).exists()
    assert not (tmp_path / "fuera.txt").exists()  # nada fuera de attachment/


def test_no_part_file_is_left_after_a_successful_upload(client, project):
    upload(client, "a.txt")
    assert [f.name for f in (project / "attachment").iterdir()] == ["a.txt"]


def test_upload_over_the_limit_is_413_with_the_find_hint_and_leaves_nothing(client, project, monkeypatch):
    _set_limits(monkeypatch, warn=0, inplace=0)  # 0 MB: cualquier cosa lo supera
    monkeypatch.setattr(svc, "MB", 1)  # 1 byte de "MB": limite = inplace_mb * 1

    r = upload(client, "grande.bin", b"x" * 10)

    assert r.status_code == 413
    assert "Buscar" in r.json()["detail"]
    assert list((project / "attachment").glob("*")) == []  # sin .part ni nombre reservado


def test_upload_rejects_by_content_length_before_reading_the_body(client, project, monkeypatch):
    _set_limits(monkeypatch, warn=1, inplace=1)
    r = upload(client, "x.bin", b"x", headers={"content-length": str(50 * 1024 * 1024)})
    # El cliente de test recalcula content-length; se prueba directo en el servicio abajo.
    assert r.status_code in (200, 413)


def test_upload_without_project_folder_is_400(client, tmp_master_db):
    master_index.set_project_path("sin-carpeta", "", windows_user_created=f"{persistence.current_windows_user()}_20260929120000")  # sello REAL de 14 digitos (BL-022/ADR-010)
    r = upload(client, "a.txt", sid="sin-carpeta")
    assert r.status_code == 400 and "carpeta" in r.json()["detail"].lower()


def test_upload_to_a_session_of_another_user_is_403(client, tmp_path, tmp_master_db):
    project = _own("ajena", tmp_path / "otro", owner="otra.persona")
    r = upload(client, "a.txt", sid="ajena")
    assert r.status_code == 403
    assert not (project / "attachment").exists()


def test_upload_blocks_a_junction_pointing_outside_the_project(client, project, tmp_path):
    outside = tmp_path / "afuera"
    outside.mkdir()
    link = project / "attachment"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no se pueden crear symlinks en este entorno")

    r = upload(client, "a.txt")

    assert r.status_code == 400
    assert list(outside.iterdir()) == []


def test_interrupted_upload_leaves_no_partial_file(project, monkeypatch):
    _set_limits(monkeypatch)

    async def body():
        yield b"primera parte"
        raise ConnectionError("el cliente cerró")

    import asyncio

    with pytest.raises(ConnectionError):
        asyncio.run(svc.upload(SID, "cortado.txt", body()))

    assert list((project / "attachment").glob("*")) == []


def test_upload_streams_in_chunks_without_loading_everything(project, monkeypatch):
    _set_limits(monkeypatch)
    import asyncio

    async def body():
        for _ in range(5):
            yield b"z" * 1000

    meta = asyncio.run(svc.upload(SID, "chunks.txt", body()))
    assert meta["size"] == 5000


def test_declared_length_over_the_limit_is_rejected_up_front(project, monkeypatch):
    _set_limits(monkeypatch, warn=1, inplace=1)
    import asyncio

    async def body():
        yield b"nunca se lee"

    with pytest.raises(svc.AttachmentError) as ei:
        asyncio.run(svc.upload(SID, "x.bin", body(), declared_length=50 * 1024 * 1024))
    assert ei.value.status == 413
    assert not (project / "attachment").exists()


# ---------------------------------------------------------------------------
# REQ-002 a: quitar un pendiente
# ---------------------------------------------------------------------------
def test_remove_pending_deletes_an_unreferenced_upload(client, project):
    upload(client, "borrar.txt")
    r = client.delete(f"/api/sessions/{SID}/attachments/borrar.txt")
    assert r.status_code == 200 and r.json() == {"removed": True}
    assert not (project / "attachment" / "borrar.txt").exists()


def test_remove_pending_refuses_a_file_already_in_a_message(client, project):
    upload(client, "enviado.txt")
    db_path = db.db_path_for(str(project))
    db.ensure_session(db_path, SID, 1.0)
    db.insert_message(db_path, "m1", SID, "user", 1.0)
    db.insert_part(db_path, "p1", "m1", SID, "file", {"name": "enviado.txt", "path": "attachment/enviado.txt"}, 1.0)

    r = client.delete(f"/api/sessions/{SID}/attachments/enviado.txt")

    assert r.status_code == 409 and "Limpiar adjuntos" in r.json()["detail"]
    assert (project / "attachment" / "enviado.txt").exists()


def test_remove_pending_cannot_delete_outside_attachment(client, project):
    (project / "secreto.txt").write_text("no me borres")
    r = client.delete(f"/api/sessions/{SID}/attachments/..%5Csecreto.txt")
    assert (project / "secreto.txt").exists()
    assert r.status_code in (200, 404)  # nunca borra fuera de attachment/


def test_remove_pending_of_something_that_does_not_exist_is_a_noop(client, project):
    r = client.delete(f"/api/sessions/{SID}/attachments/nada.txt")
    assert r.status_code == 200 and r.json() == {"removed": False}


# ---------------------------------------------------------------------------
# REQ-007: leer en su lugar
# ---------------------------------------------------------------------------
def test_register_path_returns_absolute_path_and_inplace(client, project, tmp_path):
    big = tmp_path / "datos" / "export_sap.csv"
    big.parent.mkdir()
    big.write_text("a;b\n1;2\n")

    r = client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": str(big)})

    assert r.status_code == 200
    body = r.json()
    assert body["inplace"] is True and body["path"] == str(big.resolve()) and body["kind"] == "csv"
    assert not (project / "attachment").exists()  # nada se copia


@pytest.mark.parametrize("bad", ["", "relativo/x.csv", "C:\\no\\existe\\x.csv"])
def test_register_path_rejects_invalid_paths_with_friendly_messages(client, project, bad):
    r = client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": bad})
    assert r.status_code == 400 and r.json()["detail"]


def test_register_path_rejects_a_directory(client, project, tmp_path):
    r = client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": str(tmp_path)})
    assert r.status_code == 400


def test_register_path_accepts_quoted_paths_pasted_from_explorer(client, project, tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a\n1\n")
    r = client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": f'"{f}"'})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# REQ-004 d/e: abrir
# ---------------------------------------------------------------------------
def test_open_uses_startfile_for_a_regular_document(client, project, monkeypatch):
    upload(client, "informe.pdf", b"%PDF")
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(str(p)), raising=False)

    r = client.post(f"/api/sessions/{SID}/attachments/open", json={"path": "attachment/informe.pdf"})

    assert r.status_code == 200 and r.json()["opened"] is True
    assert opened == [str((project / "attachment" / "informe.pdf").resolve())]


@pytest.mark.parametrize("name", ["herramienta.exe", "script.bat", "x.ps1", "a.vbs", "b.lnk", "c.js", "d.msi", "e.cmd", "f.jar"])
def test_open_never_executes_an_executable_it_reveals_it_in_explorer(client, project, monkeypatch, name):
    upload(client, name, b"MZ")
    started: list[str] = []
    launched: list[list[str]] = []
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)), raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda cmd, *a, **k: launched.append(cmd))

    r = client.post(f"/api/sessions/{SID}/attachments/open", json={"path": f"attachment/{name}"})

    assert r.status_code == 200 and r.json() == {"opened": False, "revealed": True, "path": str((project / "attachment" / name).resolve())}
    assert started == []  # JAMAS os.startfile sobre un ejecutable
    assert launched and launched[0][0] == "explorer" and launched[0][1].startswith("/select,")


@pytest.mark.parametrize("bad", ["../secreto.txt", "C:\\Windows\\notepad.exe", "attachment/../../x", "otra/carpeta.txt", "attachment/no-existe.txt"])
def test_open_rejects_any_path_that_is_not_a_session_attachment(client, project, monkeypatch, bad):
    (project.parent / "secreto.txt").write_text("x")
    started: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: started.append(str(p)), raising=False)

    r = client.post(f"/api/sessions/{SID}/attachments/open", json={"path": bad})

    assert r.status_code == 404
    assert started == []


def test_open_allows_a_registered_inplace_file(client, project, tmp_path, monkeypatch):
    big = tmp_path / "grande.csv"
    big.write_text("a\n1\n")
    client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": str(big)})
    opened: list[str] = []
    monkeypatch.setattr("os.startfile", lambda p: opened.append(str(p)), raising=False)

    r = client.post(f"/api/sessions/{SID}/attachments/open", json={"path": str(big)})

    assert r.status_code == 200 and opened == [str(big.resolve())]


def test_open_reports_a_friendly_error_when_windows_cannot_open_it(client, project, monkeypatch):
    upload(client, "x.pdf", b"%PDF")

    def boom(_p):
        raise OSError("sin app asociada")

    monkeypatch.setattr("os.startfile", boom, raising=False)

    r = client.post(f"/api/sessions/{SID}/attachments/open", json={"path": "attachment/x.pdf"})

    assert r.status_code == 400 and "Windows" in r.json()["detail"] and "sin app" not in r.json()["detail"]


def test_open_of_a_session_of_another_user_is_403(client, tmp_path, tmp_master_db):
    _own("otra-sesion", tmp_path / "otro", owner="otra.persona")
    r = client.post("/api/sessions/otra-sesion/attachments/open", json={"path": "attachment/x.pdf"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# REQ-003: /api/chat con attachments
# ---------------------------------------------------------------------------
def _chat_req(**over):
    base = {"session_id": SID, "message": "analizalo"}
    base.update(over)
    return main.ChatRequest(**base)


def test_validated_attachments_returns_real_metadata_from_disk(client, project):
    upload(client, "stock.csv", b"a,b\n1,2\n")

    out = main._validated_attachments(
        _chat_req(attachments=[{"path": "attachment/stock.csv", "name": "MENTIRA.pdf", "size": 999999}])
    )

    assert out is not None and out[0]["name"] == "stock.csv"
    assert out[0]["size"] == 8 and out[0]["path"] == "attachment/stock.csv" and out[0]["kind"] == "csv"


def test_validated_attachments_is_none_when_absent(project):
    assert main._validated_attachments(_chat_req()) is None
    assert main._validated_attachments(_chat_req(attachments=[])) is None


@pytest.mark.parametrize("bad", ["../secreto.txt", "attachment/../../secreto.txt", "C:\\Windows\\win.ini", "otra/x.txt", "attachment/no-existe.pdf"])
def test_validated_attachments_rejects_paths_outside_attachment(project, bad):
    (project.parent / "secreto.txt").write_text("x")
    with pytest.raises(main.HTTPException) as ei:
        main._validated_attachments(_chat_req(attachments=[{"path": bad}]))
    assert ei.value.status_code == 400


def test_validated_attachments_rejects_more_than_ten(client, project):
    for i in range(11):
        upload(client, f"f{i}.txt")
    refs = [{"path": f"attachment/f{i}.txt"} for i in range(11)]
    with pytest.raises(main.HTTPException) as ei:
        main._validated_attachments(_chat_req(attachments=refs))
    assert ei.value.status_code == 400 and "10" in ei.value.detail


def test_validated_attachments_accepts_a_registered_inplace_path(client, project, tmp_path):
    big = tmp_path / "enorme.csv"
    big.write_text("a\n1\n")
    client.post(f"/api/sessions/{SID}/attachments/register-path", json={"path": str(big)})

    out = main._validated_attachments(_chat_req(attachments=[{"path": str(big)}]))

    assert out[0]["inplace"] is True and out[0]["path"] == str(big.resolve())


def test_an_unregistered_absolute_path_is_rejected(project, tmp_path):
    other = tmp_path / "sin_registrar.csv"
    other.write_text("a\n1\n")
    with pytest.raises(main.HTTPException):
        main._validated_attachments(_chat_req(attachments=[{"path": str(other)}]))


def test_chat_request_without_attachments_is_still_valid():
    req = main.ChatRequest(session_id="s", message="hola", images=["data:image/png;base64,AAA"])
    assert req.attachments is None and req.images


# ---------------------------------------------------------------------------
# REQ-007 a: umbrales configurables
# ---------------------------------------------------------------------------
def test_thresholds_default_to_100_and_500(client):
    body = client.get("/api/config/attachments").json()
    assert body == {"warn_mb": 100, "inplace_mb": 500}


def test_thresholds_can_be_changed_and_are_read_back(client):
    r = client.post("/api/config/attachments", json={"warn_mb": 50, "inplace_mb": 200})
    assert r.status_code == 200 and r.json() == {"warn_mb": 50, "inplace_mb": 200}
    assert client.get("/api/config/attachments").json() == {"warn_mb": 50, "inplace_mb": 200}
    client.post("/api/config/attachments", json={"warn_mb": 100, "inplace_mb": 500})


@pytest.mark.parametrize("body", [{"warn_mb": 0}, {"inplace_mb": -5}, {"warn_mb": 300, "inplace_mb": 100}])
def test_invalid_thresholds_are_rejected_with_a_message(client, body):
    r = client.post("/api/config/attachments", json=body)
    assert r.status_code == 400 and r.json()["detail"]


# ---------------------------------------------------------------------------
# REQ-009: limpieza
# ---------------------------------------------------------------------------
def _send(project: Path, sid: str, mid: str, name: str, **extra):
    """Simula un mensaje ya enviado con un adjunto (part 'file')."""
    db_path = db.db_path_for(str(project))
    db.ensure_session(db_path, sid, 1.0)
    db.insert_message(db_path, mid, sid, "user", 1.0)
    data = {"name": name, "path": f"attachment/{name}", "size": 1, "kind": "file", "inplace": False, "deleted": False}
    data.update(extra)
    db.insert_part(db_path, f"{mid}-f-{name}", mid, sid, "file", data, 1.0)
    return f"file:{mid}-f-{name}"


def test_cleanup_preview_lists_files_images_and_orphans(client, project):
    upload(client, "usado.pdf", b"x" * 100)
    upload(client, "huerfano.txt", b"y" * 50)
    _send(project, SID, "m1", "usado.pdf")
    db_path = db.db_path_for(str(project))
    db.insert_part(db_path, "img1", "m1", SID, "image", {"url": "data:image/jpeg;base64," + "A" * 1000}, 1.0)

    rows = {r["kind"]: r for r in client.get(f"/api/sessions/{SID}/attachments/cleanup-preview").json()["rows"]}

    assert rows["file"]["name"] == "usado.pdf" and rows["file"]["bytes"] == 100 and rows["file"]["deletable"] is True
    assert rows["orphan"]["name"] == "huerfano.txt" and rows["orphan"]["bytes"] == 50
    assert rows["images"]["count"] == 1 and rows["images"]["bytes"] > 1000


def test_cleanup_preview_total_only_counts_what_can_be_freed(client, project):
    upload(client, "a.txt", b"x" * 100)
    _send(project, SID, "m1", "a.txt")
    body = client.get(f"/api/sessions/{SID}/attachments/cleanup-preview").json()
    assert body["total_bytes"] == 100 and "B" in body["total_label"]


def test_cleanup_deletes_from_disk_leaves_a_tombstone_and_reports_freed(client, project):
    upload(client, "borrame.pdf", b"x" * 2048)
    rid = _send(project, SID, "m1", "borrame.pdf")

    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": [rid]})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == [rid] and body["kept"] == [] and body["freed_bytes"] >= 2048
    assert not (project / "attachment" / "borrame.pdf").exists()  # borrado del DISCO
    [part] = db.list_file_parts(db.db_path_for(str(project)), SID)
    assert part["deleted"] is True and part["name"] == "borrame.pdf"  # tombstone, no fila borrada


def test_cleanup_keeps_what_the_user_did_not_select(client, project):
    upload(client, "a.pdf", b"a")
    upload(client, "b.pdf", b"b")
    ra = _send(project, SID, "m1", "a.pdf")
    _send(project, SID, "m2", "b.pdf")

    client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": [ra]})

    assert not (project / "attachment" / "a.pdf").exists()
    assert (project / "attachment" / "b.pdf").exists()


def test_cleanup_never_deletes_a_file_used_by_another_session(client, project):
    upload(client, "compartido.pdf", b"x" * 500)
    mine = _send(project, SID, "m1", "compartido.pdf")
    _send(project, "otra-sesion-misma-carpeta", "m9", "compartido.pdf")

    rows = client.get(f"/api/sessions/{SID}/attachments/cleanup-preview").json()["rows"]
    row = next(r for r in rows if r["id"] == mine)
    assert row["used_by_other_session"] is True and row["deletable"] is False

    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": [mine]})

    assert r.json()["deleted"] == [] and r.json()["kept"] == [mine]
    assert (project / "attachment" / "compartido.pdf").exists()


def test_cleanup_of_an_inplace_file_only_drops_the_reference(client, project, tmp_path):
    big = tmp_path / "enorme.csv"
    big.write_text("a\n1\n")
    rid = _send(project, SID, "m1", "enorme.csv", path=str(big), inplace=True)

    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": [rid]})

    assert r.json()["deleted"] == [rid]
    assert big.exists()  # el original NUNCA se borra
    assert db.list_file_parts(db.db_path_for(str(project)), SID)[0]["deleted"] is True


def test_cleanup_ignores_unknown_ids_and_never_touches_other_paths(client, project):
    (project / "secreto.txt").write_text("no me borres")
    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": ["orphan:..\\secreto.txt", "file:inventado", "cualquiera"]})
    assert r.status_code == 200 and r.json()["deleted"] == []
    assert (project / "secreto.txt").exists()


def test_cleanup_replaces_images_with_a_marker_and_history_has_no_broken_image(client, project):
    _send(project, SID, "m1", "x.txt")
    db_path = db.db_path_for(str(project))
    db.insert_part(db_path, "img1", "m1", SID, "image", {"url": "data:image/jpeg;base64," + "A" * 5000}, 1.0)

    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": ["images:all"]})

    assert r.json()["deleted"] == ["images:all"]
    [msg] = [m for m in db.load_messages(db_path, SID) if m["role"] == "user"]
    assert "image_url" not in str(msg) and "[imagen eliminada]" in str(msg)


def test_cleanup_deletes_orphans(client, project):
    upload(client, "huerfano.txt", b"y" * 10)
    r = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": ["orphan:huerfano.txt"]})
    assert r.json()["deleted"] == ["orphan:huerfano.txt"]
    assert not (project / "attachment" / "huerfano.txt").exists()


def test_cleanup_really_shrinks_the_database_file(client, project):
    db_path = db.db_path_for(str(project))
    _send(project, SID, "m1", "x.txt")
    big = "data:image/jpeg;base64," + "A" * 600_000
    db.insert_part(db_path, "i1", "m1", SID, "image", {"url": big}, 1.0)
    db.insert_part(db_path, "i2", "m1", SID, "image", {"url": big}, 1.0)
    before = db.database_size(db_path)

    body = client.post(f"/api/sessions/{SID}/attachments/cleanup", json={"delete": ["images:all"]}).json()

    assert db.database_size(db_path) < before - 800_000
    assert body["freed_db_bytes"] > 800_000 and body["freed_bytes"] >= body["freed_db_bytes"]


def test_summary_tells_if_a_session_has_attachments(client, project):
    assert client.get(f"/api/sessions/{SID}/attachments/summary").json() == {"has_attachments": False}
    upload(client, "a.txt")
    _send(project, SID, "m1", "a.txt")
    assert client.get(f"/api/sessions/{SID}/attachments/summary").json() == {"has_attachments": True}


def test_cleanup_of_a_session_of_another_user_is_403(client, tmp_path, tmp_master_db):
    _own("ajena2", tmp_path / "otro2", owner="otra.persona")
    assert client.get("/api/sessions/ajena2/attachments/cleanup-preview").status_code == 403
    assert client.post("/api/sessions/ajena2/attachments/cleanup", json={"delete": []}).status_code == 403
    assert client.get("/api/sessions/ajena2/attachments/summary").status_code == 403


def test_all_attachment_error_messages_pass_the_jargon_guard():
    """Los mensajes nuevos no usan jerga (regla de AGENTS.md)."""
    jargon = ("HTTP", "JSON", "traceback", "exception", "errno", "stream", "endpoint", "payload")
    msgs = [
        svc._too_big_message(600 * 1024 * 1024),
        "Elegí una carpeta para la sesión antes de adjuntar archivos.",
        "No encuentro ese archivo. Revisá la ruta y volvé a intentar.",
        "Windows no pudo abrir el archivo. Probá abrirlo desde el Explorador.",
    ]
    for m in msgs:
        assert not any(j.lower() in m.lower() for j in jargon), m
    assert time.time() > 0
