"""Spec comando-update-sesiones-publicas (0.36.2): `/update` resume SOLO lo nuevo de las conversaciones publicas de otras personas, etiquetado con su CSID,
y lista los archivos que crearon o editaron (`write`/`edit` y adjuntos). `bash` queda como limite documentado: se cuenta, nunca se muestra su texto.

Todo en `tmp_path` con bases SQLite REALES; el indice maestro va a un temporal por la fixture autouse `tmp_master_db`. El modelo es un FAKE: nunca hay red
ni login del proveedor de IA."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index
from app.persistence import service as svc
from app.session import compaction as cp

ME = "jorge"
MARTA = "marta"
LUIS = "luis"
NOW = 1_800_000_000.0
MINE = "sesion-mia"
PUB_M = "publica-de-marta"
PUB_L = "publica-de-luis"
PRIV_M = "privada-de-marta"
MAIL = "m365_search_messages"


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    monkeypatch.setenv("USERNAME", ME)
    monkeypatch.setattr(svc, "current_windows_user", lambda: ME)


@pytest.fixture(autouse=True)
def sync_writer(monkeypatch):
    monkeypatch.setattr(svc, "submit", lambda job: job())


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "proyecto"
    f.mkdir()
    return f


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def summaries(monkeypatch):
    """Reemplaza al modelo: devuelve `RESUMEN(<n>)` y registra cada transcripcion que recibio."""
    seen: list[str] = []

    async def fake(transcript: str, model: str) -> str:
        seen.append(transcript)
        return f"RESUMEN({len(seen)})"

    monkeypatch.setattr(cp, "_summarize", fake)
    return seen


def make(folder, sid, *, user=MARTA, public=True, title="Charla", messages=0):
    """Sesion en la base de `folder`. Publica = sin dueño y `published_by = user`; privada = dueño `user`."""
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, NOW)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE session SET title = ?, title_manual = 1, windows_user_created = ?, is_public = ?, published_by = ?, updated_at = ? WHERE id = ?",
            (
                title,
                "" if public else f"{user}_20260101000000",
                1 if public else 0,
                user if public else "",
                NOW + messages,
                sid,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return path


_clock = {"t": NOW}


def say(path, sid, text, *, role="user", at=None):
    """Un mensaje con texto. Los ids salen del reloj para ser estrictamente crecientes."""
    _clock["t"] += 1
    t = at if at is not None else _clock["t"]
    mid = f"{sid}-m{int(t * 1000)}"
    db.insert_message(path, mid, sid, role, t)
    db.insert_part(path, f"{mid}-p", mid, sid, "text", {"text": text}, t)
    return mid, t


def tool(path, sid, mid, name, arguments, result="ok", seq=0):
    db.insert_part(
        path,
        f"{mid}-tool{name}{seq}",
        mid,
        sid,
        "tool",
        {"call_id": f"c{seq}", "name": name, "arguments": json.dumps(arguments), "result": result},
        NOW,
    )


def saved_notes(path) -> str:
    """El texto de TODAS las notas de `/update` guardadas en la sesion MINE: lo que de verdad ve la persona y el modelo."""
    rows = q(path, "SELECT data FROM part WHERE session_id = ? AND data LIKE '%Actualización%'", MINE)
    return chr(10).join(json.loads(r[0])["text"] for r in rows)


def q(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def mine(folder):
    """Mi sesion, registrada en el indice maestro a mi nombre, con una conversacion propia ya hablada."""
    path = make(folder, MINE, user=ME, public=False, title="Mia")
    master_index.set_project_path(MINE, str(folder), title="Mia", windows_user_created=ME)
    say(path, MINE, "hola, soy yo")
    return path


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: published_by
# ---------------------------------------------------------------------------------------------------------------------
def test_published_by_defaults_to_empty_for_every_session(folder):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, "x", NOW)
    assert q(path, "SELECT published_by FROM session WHERE id = 'x'") == [("",)]


def test_an_old_database_without_the_column_is_migrated(folder):
    path = db.db_path_for(str(folder))
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT, title_manual INTEGER NOT NULL DEFAULT 0,
            windows_user_created TEXT NOT NULL DEFAULT '', is_public INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO session (id, title, windows_user_created, created_at, updated_at) VALUES ('vieja', 'Vieja', 'jorge_1', 1, 1);
        """
    )
    conn.commit()
    conn.close()
    db.get_session_meta(path, "vieja")  # abrir la base la migra
    assert q(path, "SELECT published_by FROM session WHERE id = 'vieja'") == [("",)]


def test_making_it_public_records_who_published_it_and_keeps_no_owner(folder):
    path = make(folder, MINE, user=ME, public=False)
    db.set_public(path, MINE, True, ME, NOW)
    assert q(path, "SELECT windows_user_created, published_by, is_public FROM session WHERE id = ?", MINE) == [
        ("", ME, 1)
    ]


def test_making_it_private_again_clears_published_by_and_restores_the_owner(folder):
    path = make(folder, MINE, user=ME, public=True)
    db.set_public(path, MINE, False, ME, NOW)
    ((owner, by, flag),) = q(
        path, "SELECT windows_user_created, published_by, is_public FROM session WHERE id = ?", MINE
    )
    assert by == "" and flag == 0 and owner.startswith(f"{ME}_")


def test_published_by_grants_no_permission_it_is_only_a_label(folder):
    """Una sesion con `published_by` pero NO marcada publica sigue sin ofrecerse para importar."""
    path = make(folder, PRIV_M, user=MARTA, public=False)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE session SET published_by = ? WHERE id = ?", (MARTA, PRIV_M))
    conn.commit()
    conn.close()
    say(path, PRIV_M, "secreto")
    assert svc.folder_sessions(MINE, str(folder))["sessions"] == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: el listado trae quien la publico
# ---------------------------------------------------------------------------------------------------------------------
def test_the_folder_listing_says_who_published_each_public_session(folder):
    """Sesion NUEVA (sin mensajes) que elige la carpeta: el aviso de importar lista las publicas con quien las publico."""
    path = make(folder, PUB_M, user=MARTA, title="De Marta")
    say(path, PUB_M, "hola")
    sessions = {s["id"]: s for s in svc.folder_sessions("sesion-nueva", str(folder))["sessions"]}
    assert sessions[PUB_M]["published_by"] == MARTA


def test_an_old_public_session_without_label_is_listed_as_unknown(folder):
    path = make(folder, PUB_M, user="", title="Vieja")
    say(path, PUB_M, "hola")
    assert svc.folder_sessions("sesion-nueva", str(folder))["sessions"][0]["published_by"] == "desconocido"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003 / REQ-004: marca de agua y solo lo nuevo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_first_update_summarizes_the_whole_public_conversation(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA, title="De Marta")
    say(path, PUB_M, "primera pregunta")
    say(path, PUB_M, "primera respuesta", role="assistant")
    result = svc.update_from_public(MINE, str(folder))
    assert result["updated"] == 1 and len(summaries) == 1
    assert "primera pregunta" in summaries[0] and "primera respuesta" in summaries[0]


def test_the_second_update_sends_only_what_is_new(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "vieja pregunta")
    svc.update_from_public(MINE, str(folder))
    say(path, PUB_M, "pregunta nueva")
    svc.update_from_public(MINE, str(folder))
    assert len(summaries) == 2
    assert "pregunta nueva" in summaries[1] and "vieja pregunta" not in summaries[1]


def test_the_mark_is_stored_per_session_and_source(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, t = say(path, PUB_M, "hola")
    svc.update_from_public(MINE, str(folder))
    rows = q(path, "SELECT session_id, source_id, last_seen_message_id FROM public_follow")
    assert rows == [(MINE, PUB_M, mid)]


def test_two_updates_in_a_row_do_not_resend_anything(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    svc.update_from_public(MINE, str(folder))
    again = svc.update_from_public(MINE, str(folder))
    assert again["updated"] == 0 and len(summaries) == 1


def test_messages_with_the_same_timestamp_are_not_lost_or_repeated(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "uno", at=NOW + 50)
    svc.update_from_public(MINE, str(folder))
    # El id nuevo ordena DESPUES del anterior (`...-m50000...` < `...-zz-empate`): mismo `created_at`, id mayor, tiene que contarse como nuevo.
    db.insert_message(path, f"{PUB_M}-zz-empate", PUB_M, "user", NOW + 50)
    db.insert_part(path, f"{PUB_M}-zz-empate-p", f"{PUB_M}-zz-empate", PUB_M, "text", {"text": "empate"}, NOW + 50)
    svc.update_from_public(MINE, str(folder))
    assert len(summaries) == 2 and "empate" in summaries[1] and "uno" not in summaries[1]


def test_the_mark_does_not_advance_if_saving_the_summary_fails(folder, mine, summaries, monkeypatch):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    monkeypatch.setattr(svc, "_save_update_note", lambda *a, **k: (_ for _ in ()).throw(OSError("disco")))
    result = svc.update_from_public(MINE, str(folder))
    assert result["failed"] and q(path, "SELECT COUNT(*) FROM public_follow") == [(0,)]


def test_the_resulting_summary_does_not_include_tool_results_or_images(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "mira esto")
    tool(path, PUB_M, mid, MAIL, {"q": "x"}, result="CORREO CONFIDENCIAL de Luis")
    tool(path, PUB_M, mid, "grep", {"q": "x"}, result="RESULTADO DE GREP")
    db.insert_part(path, f"{mid}-img", mid, PUB_M, "image", {"media_type": "image/png", "data": "QUJD" * 20}, NOW)
    svc.update_from_public(MINE, str(folder))
    sent = summaries[0]
    assert "CORREO CONFIDENCIAL" not in sent and "RESULTADO DE GREP" not in sent and "QUJD" not in sent


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005: la nota con el CSID
# ---------------------------------------------------------------------------------------------------------------------
def test_the_note_carries_the_csid_the_title_the_count_and_the_summary(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA, title="Plan de stock")
    say(path, PUB_M, "uno")
    say(path, PUB_M, "dos", role="assistant")
    svc.update_from_public(MINE, str(folder))
    notes = [
        r[0]
        for r in q(
            path, "SELECT data FROM part WHERE session_id = ? AND type = 'text' AND data LIKE '%Actualización%'", MINE
        )
    ]
    text = json.loads(notes[0])["text"]
    assert f"CSID {MARTA}" in text and "Plan de stock" in text and "2 mensajes nuevos" in text and "RESUMEN(1)" in text


def test_the_note_is_a_user_message_in_my_session_and_in_my_memory(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    svc.update_from_public(MINE, str(folder))
    roles = q(path, "SELECT role FROM message WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", MINE)
    assert roles == [("user",)]
    assert any("Actualización" in str(m.get("content", "")) for m in svc.load_session_messages(MINE))


def test_one_note_per_conversation_with_news_each_with_its_own_csid(folder, mine, summaries):
    for sid, who in ((PUB_M, MARTA), (PUB_L, LUIS)):
        path = make(folder, sid, user=who, title=f"de {who}")
        say(path, sid, f"hola de {who}")
    result = svc.update_from_public(MINE, str(folder))
    text = json.dumps(q(path, "SELECT data FROM part WHERE session_id = ?", MINE))
    assert result["updated"] == 2 and f"CSID {MARTA}" in text and f"CSID {LUIS}" in text


def test_previous_notes_are_never_replaced_or_touched(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "uno")
    svc.update_from_public(MINE, str(folder))
    first = q(path, "SELECT id, data FROM part WHERE session_id = ? AND data LIKE '%Actualización%'", MINE)
    say(path, PUB_M, "dos")
    svc.update_from_public(MINE, str(folder))
    both = q(path, "SELECT id, data FROM part WHERE session_id = ? AND data LIKE '%Actualización%'", MINE)
    assert len(both) == 2 and first[0] in both


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006 / REQ-007 / REQ-008 / REQ-009: sin novedades, fallas, a quien sigue
# ---------------------------------------------------------------------------------------------------------------------
def test_without_news_there_is_no_model_call_no_note_and_no_mark(folder, mine, summaries):
    make(folder, PUB_M, user=MARTA)  # publica pero sin mensajes
    result = svc.update_from_public(MINE, str(folder))
    assert result["updated"] == 0 and summaries == [] and "No hay novedades" in result["message"]
    assert q(db.db_path_for(str(folder)), "SELECT COUNT(*) FROM public_follow") == [(0,)]


def test_a_failing_conversation_does_not_stop_the_others_and_keeps_its_mark(folder, mine, monkeypatch):
    for sid, who in ((PUB_M, MARTA), (PUB_L, LUIS)):
        path = make(folder, sid, user=who, title=f"de {who}")
        say(path, sid, f"hola de {who}")

    async def fake(transcript: str, model: str) -> str:
        if MARTA in transcript or "de marta" in transcript:
            raise RuntimeError("429 tope")
        return "RESUMEN-OK"

    monkeypatch.setattr(cp, "_summarize", fake)
    result = svc.update_from_public(MINE, str(folder))
    marks = {r[0] for r in q(path, "SELECT source_id FROM public_follow")}
    assert result["updated"] == 1 and len(result["failed"]) == 1 and PUB_L in marks and PUB_M not in marks


def test_a_spend_limit_failure_says_the_consumption_cap(folder, mine, monkeypatch):
    import openai

    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")

    async def fake(transcript: str, model: str) -> str:
        exc = openai.APIError("spend limit $3 per 600 seconds", None, body=None)  # type: ignore[arg-type]
        exc.status_code = 429  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(cp, "_summarize", fake)
    result = svc.update_from_public(MINE, str(folder))
    assert "tope de consumo" in result["failed"][0]["reason"]


def test_it_never_follows_my_own_sessions_the_current_one_or_other_peoples_private_ones(folder, mine, summaries):
    own_pub = make(folder, "mi-publica", user=ME, title="Mia publica")
    say(own_pub, "mi-publica", "yo")
    priv = make(folder, PRIV_M, user=MARTA, public=False)
    say(priv, PRIV_M, "secreto")
    assert svc.update_from_public(MINE, str(folder))["updated"] == 0 and summaries == []


def test_a_conversation_made_private_stops_being_followed_and_is_reported(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    svc.update_from_public(MINE, str(folder))
    db.set_public(path, PUB_M, False, MARTA, NOW + 99)
    say(path, PUB_M, "ahora privado")
    result = svc.update_from_public(MINE, str(folder))
    assert result["updated"] == 0 and len(summaries) == 1 and PUB_M in result["no_longer_public"]


def test_a_deleted_source_is_ignored_without_breaking(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    svc.update_from_public(MINE, str(folder))
    q(path, "SELECT 1")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM session WHERE id = ?", (PUB_M,))
    conn.commit()
    conn.close()
    assert svc.update_from_public(MINE, str(folder))["updated"] == 0


def test_updating_without_a_folder_with_public_sessions_says_no_news(folder, mine, summaries):
    assert svc.update_from_public(MINE, str(folder))["updated"] == 0


# ---------------------------------------------------------------------------------------------------------------------
# REQ-012 / REQ-013 / REQ-014 / REQ-015: el mapeo de archivos
# ---------------------------------------------------------------------------------------------------------------------
def test_files_written_and_edited_are_listed_with_operation_and_csid_without_content(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "armo el reporte")
    inside = folder / "exports" / "reporte.xlsx"
    inside.parent.mkdir()
    inside.write_text("x")
    tool(path, PUB_M, mid, "write", {"path": str(inside), "content": "CONTENIDO SECRETO DEL ARCHIVO"}, seq=0)
    tool(
        path,
        PUB_M,
        mid,
        "edit",
        {"path": str(folder / "plan.md"), "old_string": "ANTES", "new_string": "DESPUES"},
        seq=1,
    )
    (folder / "plan.md").write_text("x")
    svc.update_from_public(MINE, str(folder))
    text = json.dumps(
        q(path, "SELECT data FROM part WHERE session_id = ? AND data LIKE '%Actualización%'", MINE), ensure_ascii=False
    )
    assert "exports/reporte.xlsx" in text and "creó" in text and "plan.md" in text and "editó" in text
    assert f"CSID {MARTA}" in text
    assert "CONTENIDO SECRETO" not in text and "ANTES" not in text and "DESPUES" not in text


def test_a_path_inside_the_folder_is_relative_and_one_outside_is_marked_other_pc(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    tool(path, PUB_M, mid, "write", {"path": str(folder / "a" / "uno.txt"), "content": "c"}, seq=0)
    tool(path, PUB_M, mid, "write", {"path": r"C:\Users\marta\Desktop\otro.txt", "content": "c"}, seq=1)
    result = svc.update_from_public(MINE, str(folder))
    text = saved_notes(path) + json.dumps(result, ensure_ascii=False)
    assert "a/uno.txt" in text and "ruta de otra PC" in text
    assert str(folder) not in saved_notes(path), "la ruta absoluta de esta PC no debe aparecer en la nota"


def test_a_file_that_is_not_on_this_pc_is_marked_not_synced_yet(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    (folder / "esta.txt").write_text("c")
    tool(path, PUB_M, mid, "write", {"path": str(folder / "esta.txt"), "content": "c"}, seq=0)
    tool(path, PUB_M, mid, "write", {"path": str(folder / "falta.txt"), "content": "c"}, seq=1)
    files = {f["path"]: f for f in svc.update_from_public(MINE, str(folder))["files"]}
    assert files["esta.txt"]["exists"] is True and files["falta.txt"]["exists"] is False
    note = json.dumps(
        q(path, "SELECT data FROM part WHERE session_id = ? AND data LIKE '%Actualización%'", MINE), ensure_ascii=False
    )
    assert "aún no sincronizado" in note


def test_new_attachments_are_listed(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "adjunto")
    (folder / "attachment").mkdir()
    (folder / "attachment" / "factura.pdf").write_text("x")
    db.insert_part(path, f"{mid}-f", mid, PUB_M, "file", {"name": "factura.pdf", "path": "attachment/factura.pdf"}, NOW)
    text = json.dumps(svc.update_from_public(MINE, str(folder)), ensure_ascii=False)
    assert "factura.pdf" in text


def test_files_already_summarized_are_not_listed_again(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    tool(path, PUB_M, mid, "write", {"path": str(folder / "viejo.txt"), "content": "c"}, seq=0)
    svc.update_from_public(MINE, str(folder))
    mid2, _ = say(path, PUB_M, "y")
    tool(path, PUB_M, mid2, "write", {"path": str(folder / "nuevo.txt"), "content": "c"}, seq=0)
    paths = [f["path"] for f in svc.update_from_public(MINE, str(folder))["files"]]
    assert paths == ["nuevo.txt"]


def test_bash_commands_are_only_counted_and_their_text_never_appears(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "corro cosas")
    tool(path, PUB_M, mid, "bash", {"command": "curl -u marta:CLAVE-SECRETA https://x"}, seq=0)
    tool(path, PUB_M, mid, "bash", {"command": "python generar.py"}, seq=1)
    result = svc.update_from_public(MINE, str(folder))
    text = json.dumps(result, ensure_ascii=False) + json.dumps(
        q(path, "SELECT data FROM part WHERE session_id = ?", MINE), ensure_ascii=False
    )
    assert "ejecutó 2 comandos de consola; no sé qué archivos crearon" in text
    assert "CLAVE-SECRETA" not in text and "generar.py" not in text and "CLAVE-SECRETA" not in json.dumps(summaries)


def test_with_no_bash_commands_there_is_no_console_line(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "solo texto")
    assert "comandos de consola" not in json.dumps(svc.update_from_public(MINE, str(folder)), ensure_ascii=False)


def test_one_bash_command_is_singular(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    tool(path, PUB_M, mid, "bash", {"command": "dir"}, seq=0)
    svc.update_from_public(MINE, str(folder))
    assert "ejecutó 1 comando de consola" in saved_notes(path)


def test_files_only_news_make_a_note_without_calling_the_model(folder, mine, summaries):
    """REQ-015: si lo unico nuevo son archivos (sin texto), hay nota con el mapeo y NO se llama al modelo."""
    path = make(folder, PUB_M, user=MARTA)
    db.insert_message(path, f"{PUB_M}-solo", PUB_M, "assistant", NOW + 5)
    tool(path, PUB_M, f"{PUB_M}-solo", "write", {"path": str(folder / "solo.txt"), "content": "c"}, seq=0)
    result = svc.update_from_public(MINE, str(folder))
    assert result["updated"] == 1 and summaries == [] and [f["path"] for f in result["files"]] == ["solo.txt"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-008 / REQ-010 / REQ-011: endpoint, menu y textos
# ---------------------------------------------------------------------------------------------------------------------
def test_the_endpoint_returns_the_result_for_the_owner(folder, mine, summaries, client):
    path = make(folder, PUB_M, user=MARTA, title="De Marta")
    say(path, PUB_M, "hola")
    resp = client.post(f"/api/commands/update/{MINE}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] == 1 and "message" in data


def test_someone_else_cannot_run_it_403(folder, mine, summaries, client, monkeypatch):
    monkeypatch.setattr(svc, "current_windows_user", lambda: LUIS)
    assert client.post(f"/api/commands/update/{MINE}").status_code == 403


def test_without_a_folder_it_answers_400_with_a_sentence(client):
    resp = client.post("/api/commands/update/sin-carpeta-xyz")
    assert resp.status_code == 400 and "carpeta" in resp.json()["detail"].lower()


def test_the_command_is_offered_only_when_the_folder_has_foreign_public_sessions(folder, mine, client):
    names = lambda: [c["name"] for c in client.get("/api/commands", params={"session_id": MINE}).json()["system"]]  # noqa: E731
    assert "update" not in names()
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "hola")
    assert "update" in names()


def test_the_command_is_offered_to_sessions_without_a_folder_never(client):
    names = [c["name"] for c in client.get("/api/commands", params={"session_id": "sin-carpeta-xyz"}).json()["system"]]
    assert "update" not in names


def test_the_texts_have_no_jargon(folder, mine, summaries):
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    tool(path, PUB_M, mid, "bash", {"command": "dir"}, seq=0)
    text = json.dumps(svc.update_from_public(MINE, str(folder)), ensure_ascii=False)
    for jargon in ("token", "marca de agua", "sqlite", "public_follow", "last_seen", "Traceback", "HTTP"):
        assert jargon.lower() not in text.lower(), jargon


def test_the_plural_of_conversation_has_no_accent_and_the_singular_does(folder, mine, summaries):
    """«conversaciones» (no «conversaciónes»): error real de redaccion que vio un e2e."""
    for sid, who in ((PUB_M, MARTA), (PUB_L, LUIS)):
        path = make(folder, sid, user=who, title=f"de {who}")
        say(path, sid, f"hola de {who}")
    many = svc.update_from_public(MINE, str(folder))["message"]
    assert "2 conversaciones públicas" in many and "conversaciónes" not in many
    path = make(folder, "otra-mas", user=MARTA, title="una sola")
    say(path, "otra-mas", "nueva")
    one = svc.update_from_public(MINE, str(folder))["message"]
    assert "1 conversación pública" in one and "conversaciones" not in one


def test_the_no_longer_public_message_is_singular_and_plural(folder, mine, summaries):
    from app.persistence import service as s

    base = {"updated": 0, "notes": [], "failed": []}
    assert "1 conversación ya no es pública" in s._update_message({**base, "no_longer_public": ["a"]})
    assert "2 conversaciones ya no son públicas" in s._update_message({**base, "no_longer_public": ["a", "b"]})


# ---------------------------------------------------------------------------------------------------------------------
# Cierre de mutantes (7, 9, 13, 18, 24, 30 de la corrida del 2026-10-24)
# ---------------------------------------------------------------------------------------------------------------------
def test_only_user_and_assistant_text_is_summarized_not_other_roles(folder, mine, summaries):
    """Mutante «sin filtro de rol»: un mensaje de sistema o de herramienta con texto no debe llegar al resumen."""
    path = make(folder, PUB_M, user=MARTA)
    say(path, PUB_M, "pregunta real")
    db.insert_message(path, f"{PUB_M}-sys", PUB_M, "system", NOW + 500)
    db.insert_part(path, f"{PUB_M}-sys-p", f"{PUB_M}-sys", PUB_M, "text", {"text": "INSTRUCCIONES INTERNAS DEL SISTEMA"}, NOW + 500)
    svc.update_from_public(MINE, str(folder))
    assert "pregunta real" in summaries[0] and "INSTRUCCIONES INTERNAS" not in summaries[0]


def test_write_is_created_and_edit_is_edited_each_with_its_own_verb(folder, mine, summaries):
    """Mutante «verbos cruzados»: `write` crea y `edit` edita; la nota no los puede intercambiar."""
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    tool(path, PUB_M, mid, "write", {"path": str(folder / "nuevo.txt"), "content": "c"}, seq=0)
    tool(path, PUB_M, mid, "edit", {"path": str(folder / "viejo.txt"), "old_string": "a", "new_string": "b"}, seq=1)
    ops = {f["path"]: f["op"] for f in svc.update_from_public(MINE, str(folder))["files"]}
    assert ops == {"nuevo.txt": "creó", "viejo.txt": "editó"}


def test_the_menu_does_not_offer_it_when_the_only_public_sessions_have_no_messages(folder, mine):
    """Mutante «sin message_count»: una publica vacia no justifica ofrecer `/update`."""
    make(folder, PUB_M, user=MARTA)  # publica, sin ningun mensaje
    assert svc.has_foreign_public_sessions(MINE) is False
    say(db.db_path_for(str(folder)), PUB_M, "ahora si hay algo")
    assert svc.has_foreign_public_sessions(MINE) is True


def test_the_note_lists_files_under_a_label_with_the_csid_and_a_separator(folder, mine, summaries):
    """Mutante «sin etiqueta de archivos»: la lista dice de quien son y separa cada archivo con « · »."""
    path = make(folder, PUB_M, user=MARTA)
    mid, _ = say(path, PUB_M, "x")
    (folder / "a.txt").write_text("x")
    (folder / "b.txt").write_text("x")
    tool(path, PUB_M, mid, "write", {"path": str(folder / "a.txt"), "content": "c"}, seq=0)
    tool(path, PUB_M, mid, "write", {"path": str(folder / "b.txt"), "content": "c"}, seq=1)
    svc.update_from_public(MINE, str(folder))
    assert f"Archivos nuevos de CSID {MARTA}: a.txt (creó) · b.txt (creó)" in saved_notes(path)


def test_a_failure_in_one_conversation_still_lets_the_next_one_be_updated(folder, mine, monkeypatch):
    """Mutante «break en vez de continue»: la que falla se procesa PRIMERO y la siguiente igual se actualiza.
    El orden es por fecha y, a igual fecha, por id (`insert_message` pisa `updated_at` con la hora del mensaje): `aaa-falla` < `zzz-anda`."""
    first = make(folder, "aaa-falla", user=MARTA, title="falla")
    say(first, "aaa-falla", "texto que hace fallar", at=NOW + 5)
    second = make(folder, "zzz-anda", user=LUIS, title="anda")
    say(second, "zzz-anda", "texto que anda", at=NOW + 5)
    assert [s["id"] for s in svc._foreign_public_sources(MINE, str(folder))] == ["aaa-falla", "zzz-anda"]

    async def fake(transcript: str, model: str) -> str:
        if "hace fallar" in transcript:
            raise RuntimeError("boom")
        return "RESUMEN-OK"

    monkeypatch.setattr(cp, "_summarize", fake)
    result = svc.update_from_public(MINE, str(folder))

    assert [f["id"] for f in result["failed"]] == ["aaa-falla"] and result["updated"] == 1
    assert "RESUMEN-OK" in saved_notes(first)


def test_the_endpoint_reloads_the_live_memory_so_the_next_message_sees_the_notes(folder, mine, summaries, client):
    """Mutante «sin _reload_session_memory»: con la sesion YA en memoria, el endpoint le pone las notas nuevas."""
    from app.session.events import get_session_store

    store = get_session_store()
    store.delete(MINE)
    live = store.get_or_create(MINE)
    live.messages.append({"role": "user", "content": "hola, soy yo"})
    path = make(folder, PUB_M, user=MARTA, title="De Marta")
    say(path, PUB_M, "novedad importante")
    assert not any("Actualización" in str(m.get("content", "")) for m in live.messages)

    assert client.post(f"/api/commands/update/{MINE}").status_code == 200

    assert any("Actualización de CSID marta" in str(m.get("content", "")) for m in store.get(MINE).messages)
    store.delete(MINE)


def test_my_own_session_published_from_another_pc_is_not_followed_even_if_this_pc_does_not_know_it(folder, mine, summaries):
    """Mutante «sin published_by != user»: yo publique la conversacion desde OTRA PC mia. En esta PC el indice maestro no la conoce, asi que
    `_is_own_source` dice «no es mia»; solo la etiqueta `published_by == yo` evita que `/update` me resuma a mi mismo."""
    path = make(folder, "mia-desde-otra-pc", user=ME, title="Mia, otra PC")
    say(path, "mia-desde-otra-pc", "esto lo escribi yo en otra PC")
    assert master_index.get_owner("mia-desde-otra-pc") is None  # esta PC no la conoce
    assert svc._foreign_public_sources(MINE, str(folder)) == []
    assert svc.update_from_public(MINE, str(folder))["updated"] == 0 and summaries == []


def test_a_session_the_master_index_says_is_mine_is_not_followed_even_if_the_label_names_someone_else(folder, mine, summaries):
    """Mutante «sin _is_own_source»: el indice maestro de ESTA PC tiene la sesion a mi nombre, pero la etiqueta `published_by` dice otro (dato viejo o
    editado a mano). Manda el indice maestro: es mia y no se sigue."""
    path = make(folder, "mia-etiqueta-ajena", user=MARTA, title="Dice Marta")
    say(path, "mia-etiqueta-ajena", "hola")
    master_index.set_project_path("mia-etiqueta-ajena", str(folder), title="x", windows_user_created=ME)
    assert svc._foreign_public_sources(MINE, str(folder)) == []
    assert svc.update_from_public(MINE, str(folder))["updated"] == 0 and summaries == []
