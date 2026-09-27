"""Unit de `app/persistence/db.py` (SQLite por carpeta de proyecto): esquema,
migraciones, sesiones, mensajes/parts (cascada), reconstruccion del historial
para el LLM, todos, busqueda y utilidades. Todo en `tmp_path`."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.persistence import db

NOW = 1_700_000_000.0


@pytest.fixture
def path(tmp_path):
    return tmp_path / "proyecto" / "db" / "vision.db"


def add_message(path, mid, sid="s1", role="user", text=None, at=NOW, parts=()):
    db.ensure_session(path, sid, at)
    db.insert_message(path, mid, sid, role, at)
    if text is not None:
        db.insert_part(path, f"{mid}-t", mid, sid, "text", {"text": text}, at)
    for i, (ptype, data) in enumerate(parts):
        db.insert_part(path, f"{mid}-p{i}", mid, sid, ptype, data, at)


def raw(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---- infraestructura --------------------------------------------------------
def test_db_path_for_is_inside_project_folder(tmp_path):
    assert db.db_path_for(str(tmp_path)) == tmp_path / db.SESSION_SUBDIR / db.DB_FILENAME


def test_windows_user_tag_format(monkeypatch):
    monkeypatch.setenv("USERNAME", "usuario1")
    tag = db.windows_user_tag()
    user, stamp = tag.rsplit("_", 1)
    assert user == "usuario1" and len(stamp) == 14 and stamp.isdigit()


def test_windows_user_tag_falls_back(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert db.windows_user_tag().startswith("desconocido_")


def test_connect_creates_folder_schema_and_wal(path):
    db.ensure_session(path, "s1", NOW)
    tables = {r[0] for r in raw(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"session", "message", "part", "todo"} <= tables
    assert raw(path, "PRAGMA journal_mode")[0][0] == "wal"


def test_migrate_adds_missing_columns_to_legacy_db(path):
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT,
                              created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                              created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE todo (session_id TEXT NOT NULL, position INTEGER NOT NULL, content TEXT NOT NULL,
                           status TEXT NOT NULL, priority TEXT NOT NULL, PRIMARY KEY (session_id, position));
        INSERT INTO session VALUES ('vieja', 'Sesion legada', NULL, 1, 1);
        """
    )
    conn.commit()
    conn.close()
    assert db.session_exists(path, "vieja")  # abrir dispara la migracion
    cols = lambda t: {r[1] for r in raw(path, f"PRAGMA table_info({t})")}
    assert {"title_manual", "windows_user_created"} <= cols("session")
    assert "response_type" in cols("message") and "subtasks_json" in cols("todo")


def test_connect_rolls_back_on_error(path):
    db.ensure_session(path, "s1", NOW)
    with pytest.raises(RuntimeError), db._connect(path) as conn:
        conn.execute("UPDATE session SET title = 'MUTADO' WHERE id = 's1'")
        raise RuntimeError("falla a mitad")
    assert raw(path, "SELECT title FROM session")[0][0] != "MUTADO"


# ---- sesiones ----------------------------------------------------------------
def test_ensure_session_creates_placeholder_and_is_idempotent(path):
    db.ensure_session(path, "s1", NOW, model="m1")
    meta = db.get_session_meta(path, "s1")
    assert meta["title"].startswith(db.DEFAULT_TITLE_PREFIX) and meta["model"] == "m1"
    title = meta["title"]
    db.ensure_session(path, "s1", NOW + 5, model="m2")
    meta = db.get_session_meta(path, "s1")
    assert meta["title"] == title and meta["model"] == "m2" and meta["updated_at"] == NOW + 5
    db.ensure_session(path, "s1", NOW + 9)  # sin modelo: no toca nada
    assert db.get_session_meta(path, "s1")["updated_at"] == NOW + 5


def test_missing_db_reads_are_safe_and_dont_create_files(path):
    assert db.get_session_meta(path, "x") is None and db.session_exists(path, "x") is False
    assert db.list_sessions(path) == [] and db.sessions_older_than(path, 1) == []
    assert db.load_messages(path, "x") == [] and db.load_todos(path, "x") == []
    assert db.load_messages_for_display(path, "x") == [] and db.search_sessions(path, "a") == []
    assert db.find_tool_result(path, "x", None) is None
    assert db.find_user_occurrence(path, "x", "m") is None
    assert db.clear_session_messages(path, "x") == 0
    assert db.delete_assistant_response(path, "x", 0) is False
    assert not path.exists()


def test_touch_and_list_sessions_order_and_counts(path):
    add_message(path, "m1", sid="vieja", at=100)
    add_message(path, "m2", sid="nueva", at=200)
    add_message(path, "m3", sid="nueva", at=201)
    listed = db.list_sessions(path)
    assert [s["id"] for s in listed] == ["nueva", "vieja"]
    assert {s["id"]: s["message_count"] for s in listed} == {"nueva": 2, "vieja": 1}
    db.touch_session(path, "vieja", 999)
    assert db.list_sessions(path)[0]["id"] == "vieja"


def test_list_sessions_filters_by_windows_user(path, monkeypatch):
    monkeypatch.setattr(db, "windows_user_tag", lambda: "ana_20260101000000")
    db.ensure_session(path, "de-ana", NOW)
    monkeypatch.setattr(db, "windows_user_tag", lambda: "beto_20260101000000")
    db.ensure_session(path, "de-beto", NOW)
    assert [s["id"] for s in db.list_sessions(path, current_user="ana")] == ["de-ana"]
    assert len(db.list_sessions(path)) == 2


def test_list_sessions_user_filter_does_not_leak_across_similar_usernames(path, monkeypatch):
    """Regresion QA-005: `_` es comodin de LIKE -- sin ESCAPE, "an" veia las
    sesiones de "ana" y se rompia el aislamiento por usuario."""
    monkeypatch.setattr(db, "windows_user_tag", lambda: "ana_20260101000000")
    db.ensure_session(path, "de-ana", NOW)
    assert db.list_sessions(path, current_user="an") == []
    assert db.list_sessions(path, current_user="anx") == []
    assert [s["id"] for s in db.list_sessions(path, current_user="ana")] == ["de-ana"]


@pytest.mark.parametrize("user", ["ana", "a_b", "100%user", r"back\slash", "a%b_c"])
def test_user_filter_matches_only_its_own_sessions_for_tricky_names(path, monkeypatch, user):
    r"""QA-005: un nombre con metacaracteres de LIKE (`%`, `_`, `\`) debe matchear
    literal -- ni traer de mas ni dejar de encontrar lo propio."""
    monkeypatch.setattr(db, "windows_user_tag", lambda: f"{user}_20260101000000")
    db.ensure_session(path, "propia", NOW)
    monkeypatch.setattr(db, "windows_user_tag", lambda: "otro_20260101000000")
    db.ensure_session(path, "ajena", NOW)
    assert [s["id"] for s in db.list_sessions(path, current_user=user)] == ["propia"]


def test_user_filter_escape_helper():
    assert db._user_prefix_like("ana") == r"ana\_%"
    assert db._user_prefix_like("a_b") == r"a\_b\_%"
    assert db._user_prefix_like("100%u") == r"100\%u\_%"
    assert db._user_prefix_like(r"c:\x") == r"c:\\x\_%"


def test_rename_marks_manual_and_autoname_never_overwrites_it(path):
    db.ensure_session(path, "s1", NOW)
    assert db.autoname_session(path, "s1", "Auto uno", NOW + 1) is True  # placeholder -> nombra
    assert db.autoname_session(path, "s1", "Auto dos", NOW + 2) is False  # ya no es placeholder
    assert db.get_session_meta(path, "s1")["title"] == "Auto uno"
    assert db.rename_session(path, "s1", "Manual", NOW + 3) is True
    assert db.autoname_session(path, "s1", "Auto tres", NOW + 4) is False
    assert db.get_session_meta(path, "s1")["title"] == "Manual"
    assert db.rename_session(path, "no-existe", "x", NOW) is False


def test_autoname_skips_manual_title_even_if_it_looks_like_placeholder(path):
    db.ensure_session(path, "s1", NOW)
    db.rename_session(path, "s1", db.DEFAULT_TITLE_PREFIX + "yo lo puse", NOW)
    assert db.autoname_session(path, "s1", "Auto", NOW + 1) is False


def test_sessions_older_than_and_delete_cascades(path):
    add_message(path, "m1", sid="vieja", text="hola", at=100)
    add_message(path, "m2", sid="nueva", text="chau", at=10_000)
    assert [s["id"] for s in db.sessions_older_than(path, 5_000)] == ["vieja"]
    assert db.delete_session(path, "vieja") is True and db.delete_session(path, "vieja") is False
    assert raw(path, "SELECT COUNT(*) FROM message WHERE session_id='vieja'")[0][0] == 0
    assert raw(path, "SELECT COUNT(*) FROM part WHERE session_id='vieja'")[0][0] == 0  # cascada
    assert raw(path, "SELECT COUNT(*) FROM part WHERE session_id='nueva'")[0][0] == 1


# ---- mensajes / parts --------------------------------------------------------
def test_insert_message_is_idempotent_and_touches_session(path):
    db.ensure_session(path, "s1", NOW)
    db.insert_message(path, "m1", "s1", "user", NOW + 10)
    db.insert_message(path, "m1", "s1", "assistant", NOW + 20)  # mismo id: INSERT OR IGNORE
    assert raw(path, "SELECT role FROM message WHERE id='m1'") == [("user",)]
    assert db.get_session_meta(path, "s1")["updated_at"] == NOW + 20


def test_parts_get_increasing_seq_and_ignore_duplicate_ids(path):
    add_message(path, "m1", text="uno")
    db.insert_part(path, "m1-x", "m1", "s1", "text", {"text": "dos"}, NOW)
    db.insert_part(path, "m1-x", "m1", "s1", "text", {"text": "duplicado"}, NOW)
    assert raw(path, "SELECT id, seq FROM part WHERE message_id='m1' ORDER BY seq") == [("m1-t", 0), ("m1-x", 1)]


def test_update_part_replaces_data(path):
    add_message(path, "m1", text="parcial")
    db.update_part(path, "m1-t", {"text": "final ñ"}, NOW + 1)
    assert json.loads(raw(path, "SELECT data FROM part")[0][0]) == {"text": "final ñ"}
    assert "ñ" in raw(path, "SELECT data FROM part")[0][0]  # ensure_ascii=False


def test_delete_message_cascades_to_parts(path):
    add_message(path, "m1", text="x")
    assert db.delete_message(path, "m1") is True and db.delete_message(path, "m1") is False
    assert raw(path, "SELECT COUNT(*) FROM part")[0][0] == 0


def test_find_user_occurrence(path):
    add_message(path, "u1", role="user", at=1)
    add_message(path, "a1", role="assistant", at=2)
    add_message(path, "u2", role="user", at=3)
    assert db.find_user_occurrence(path, "s1", "u1") == 0
    assert db.find_user_occurrence(path, "s1", "u2") == 1
    assert db.find_user_occurrence(path, "s1", "a1") is None  # no es 'user'
    assert db.find_user_occurrence(path, "s1", "nada") is None


def test_clear_session_messages_only_touches_that_session(path):
    add_message(path, "m1", sid="s1", text="a")
    add_message(path, "m2", sid="s1", text="b")
    add_message(path, "m3", sid="s2", text="c")
    assert db.clear_session_messages(path, "s1") == 2
    assert raw(path, "SELECT id FROM message") == [("m3",)]


def test_delete_assistant_response_is_surgical(path):
    add_message(path, "u1", role="user", text="p1", at=1)
    add_message(path, "a1", role="assistant", text="r1", at=2)
    add_message(path, "u2", role="user", text="p2", at=3)
    add_message(path, "a2", role="assistant", text="r2", at=4)
    assert db.delete_assistant_response(path, "s1", 0) is True
    assert {r[0] for r in raw(path, "SELECT id FROM message")} == {"u1", "u2", "a2"}
    assert db.delete_assistant_response(path, "s1", 9) is False  # ocurrencia fuera de rango
    assert raw(path, "SELECT COUNT(*) FROM message")[0][0] == 3  # el intento fuera de rango no borro nada


def test_delete_assistant_response_never_crosses_into_next_turn(path):
    """Regresion QA-006 (perdida de datos): si el turno N quedo sin respuesta,
    borrar "la respuesta del turno N" se llevaba la del turno N+1."""
    add_message(path, "u1", role="user", text="p1 (sin respuesta)", at=1)
    add_message(path, "u2", role="user", text="p2", at=3)
    add_message(path, "a2", role="assistant", text="r2", at=4)
    assert db.delete_assistant_response(path, "s1", 0) is False
    assert "a2" in {r[0] for r in raw(path, "SELECT id FROM message")}


def test_delete_assistant_response_without_reply_returns_false(path):
    add_message(path, "u1", role="user", text="sin respuesta", at=1)
    assert db.delete_assistant_response(path, "s1", 0) is False


def test_load_messages_for_display_text_only_ordered_with_ms_and_response_type(path):
    add_message(path, "u1", role="user", text="hola", at=10)
    add_message(path, "a1", role="assistant", text="que tal", at=11, parts=[("tool", {"name": "bash"})])
    add_message(path, "a2", role="assistant", at=12, parts=[("tool", {"name": "solo-tool"})])  # sin texto: se omite
    db.update_message_response_type(path, "a1", "s1", "analisis", 12)
    out = db.load_messages_for_display(path, "s1")
    assert [m["messageId"] for m in out] == ["u1", "a1"]
    assert out[0] == {"role": "user", "text": "hola", "messageId": "u1", "time": 10_000}
    assert out[1]["responseType"] == "analisis" and "responseType" not in out[0]


def test_update_response_type_requires_matching_session(path):
    add_message(path, "a1", role="assistant", text="x")
    db.update_message_response_type(path, "a1", "otra-sesion", "x", NOW)
    assert raw(path, "SELECT response_type FROM message WHERE id='a1'") == [(None,)]


# ---- load_messages (historial para el LLM) ----------------------------------
def test_load_messages_rebuilds_openai_format_with_tools(path):
    add_message(path, "sys", role="system", text="reglas", at=1)
    add_message(path, "u1", role="user", text="corre ls", at=2)
    add_message(
        path,
        "a1",
        role="assistant",
        text="ok",
        at=3,
        parts=[("tool", {"name": "bash", "call_id": "call_1", "arguments": '{"command":"ls"}', "result": "a.txt"})],
    )
    msgs = db.load_messages(path, "s1")
    assert msgs[0] == {"role": "system", "content": "reglas"}
    assert msgs[1] == {"role": "user", "content": "corre ls"}
    assert msgs[2]["content"] == "ok"
    assert msgs[2]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ]
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}


def test_load_messages_user_with_images_builds_content_blocks(path):
    add_message(path, "u1", role="user", text="mira", parts=[("image", {"url": "data:image/png;base64,AAA"})])
    add_message(path, "u2", role="user", parts=[("image", {"url": "data:image/png;base64,BBB"})])
    msgs = db.load_messages(path, "s1")
    assert msgs[0]["content"] == [
        {"type": "text", "text": "mira"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    assert msgs[1]["content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}}]  # sin bloque de texto vacio


def test_load_messages_drops_incomplete_or_invalid_tool_calls(path):
    add_message(
        path,
        "a1",
        role="assistant",
        text="hecho",
        parts=[
            ("tool", {"name": "bash", "call_id": "ok_1", "arguments": "{}", "result": "r"}),
            ("tool", {"name": "bash", "call_id": "interrumpida", "arguments": "{}"}),  # sin result
            ("tool", {"name": "bash", "call_id": "id con espacios!", "arguments": "{}", "result": "r"}),
            ("tool", {"call_id": "sin_nombre", "arguments": "{}", "result": "r"}),
        ],
    )
    msgs = db.load_messages(path, "s1")
    assert [tc["id"] for tc in msgs[0]["tool_calls"]] == ["ok_1"]
    assert len(msgs) == 2  # assistant + 1 tool result: nunca queda un tool_call huerfano


def test_load_messages_skips_assistant_with_nothing_usable_and_unknown_roles(path):
    add_message(path, "a1", role="assistant", parts=[("tool", {"name": "bash", "call_id": "x"})])
    add_message(path, "r1", role="extraterrestre", text="?")
    add_message(path, "u1", role="user", text="hola")
    assert db.load_messages(path, "s1") == [{"role": "user", "content": "hola"}]


# ---- todos -------------------------------------------------------------------
def test_todos_roundtrip_replace_and_order(path):
    db.ensure_session(path, "s1", NOW)
    db.replace_todos(path, "s1", [
        {"content": "A", "status": "completed", "priority": "high", "subtasks": [{"content": "a1"}]},
        {"content": "B"},
    ])
    todos = db.load_todos(path, "s1")
    assert [t["content"] for t in todos] == ["A", "B"]
    assert todos[0]["subtasks"] == [{"content": "a1"}]
    assert todos[1] == {"content": "B", "status": "pending", "priority": "medium", "subtasks": []}
    db.replace_todos(path, "s1", [{"content": "Solo"}])  # reemplazo completo
    assert [t["content"] for t in db.load_todos(path, "s1")] == ["Solo"]
    db.replace_todos(path, "s1", [])
    assert db.load_todos(path, "s1") == []


def test_load_todos_survives_corrupt_subtasks_json(path):
    db.ensure_session(path, "s1", NOW)
    db.replace_todos(path, "s1", [{"content": "A"}, {"content": "B"}])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE todo SET subtasks_json = '{roto' WHERE content = 'A'")
    conn.commit()
    conn.close()
    todos = db.load_todos(path, "s1")
    assert [t["subtasks"] for t in todos] == [[], []] and len(todos) == 2


# ---- find_tool_result ---------------------------------------------------------
def test_find_tool_result_by_call_id_by_name_and_latest(path):
    add_message(path, "a1", role="assistant", at=1, parts=[("tool", {"name": "oracle_query", "call_id": "c1", "result": "viejo"})])
    add_message(path, "a2", role="assistant", at=2, parts=[("tool", {"name": "bash", "call_id": "c2", "result": "b"})])
    add_message(path, "a3", role="assistant", at=3, parts=[("tool", {"name": "oracle_query", "call_id": "c3", "result": "nuevo"})])
    assert db.find_tool_result(path, "s1", "c1")["result"] == "viejo"
    assert db.find_tool_result(path, "s1", "zzz") is None
    assert db.find_tool_result(path, "s1", None, ["oracle_query"])["result"] == "nuevo"  # la mas reciente
    assert db.find_tool_result(path, "s1", None)["result"] == "nuevo"  # cualquier tool
    assert db.find_tool_result(path, "s1", None, ["sap_query"]) is None


# ---- busqueda ----------------------------------------------------------------
def test_snippet_helpers():
    assert db._extract_text("text", {"text": "hola"}) == "hola"
    assert db._extract_text("tool", {"name": "bash", "arguments": "ls", "result": "a.txt"}) == "bash ls a.txt"
    assert db._extract_text("image", {"url": "x"}) == ""
    long = "x" * 200 + "AGUJA" + "y" * 200
    snippet = db._build_snippet(long, "aguja")
    assert snippet.startswith("…") and snippet.endswith("…") and "AGUJA" in snippet and len(snippet) < 200
    assert db._build_snippet("corto aguja fin", "aguja") == "corto aguja fin"  # sin elipsis si no se corta
    assert db._build_snippet("sin coincidencia", "zzz") == "sin coincidencia"


def test_search_finds_one_result_per_session_with_snippet_and_limit(path):
    add_message(path, "m1", sid="s1", text="El pedido de la empresa llego", at=1)
    add_message(path, "m2", sid="s1", text="otro pedido mas", at=2)
    add_message(path, "m3", sid="s2", text="nada relevante", at=3)
    add_message(path, "m4", sid="s3", text="PEDIDO urgente", at=4)
    out = db.search_sessions(path, "pedido")
    assert [r["id"] for r in out] == ["s3", "s1"]  # mas reciente primero, 1 por sesion
    assert "PEDIDO" in out[0]["snippet"]
    assert len(db.search_sessions(path, "pedido", limit=1)) == 1
    assert db.search_sessions(path, "   ") == []


def test_search_covers_tool_results_and_filters_by_user(path, monkeypatch):
    monkeypatch.setattr(db, "windows_user_tag", lambda: "ana_1")
    add_message(path, "a1", sid="de-ana", role="assistant", parts=[("tool", {"name": "oracle_query", "result": "tabla ZZ_STOCK"})])
    monkeypatch.setattr(db, "windows_user_tag", lambda: "beto_1")
    add_message(path, "a2", sid="de-beto", role="assistant", parts=[("tool", {"name": "oracle_query", "result": "tabla ZZ_STOCK"})])
    assert {r["id"] for r in db.search_sessions(path, "zz_stock")} == {"de-ana", "de-beto"}
    assert [r["id"] for r in db.search_sessions(path, "zz_stock", current_user="ana")] == ["de-ana"]


def test_search_skips_corrupt_part_rows(path):
    add_message(path, "m1", text="hallazgo valido")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO part VALUES ('roto','m1','s1',9,'text','{hallazgo roto',1,1)")
    conn.commit()
    conn.close()
    assert [r["id"] for r in db.search_sessions(path, "hallazgo")] == ["s1"]


def test_search_is_case_insensitive_for_accented_text(path):
    """Regresion QA-003: LOWER()/LIKE de SQLite solo pliegan ASCII -- roto justo
    en español, el idioma de todo el contenido del proyecto."""
    add_message(path, "m1", text="LA CANCIÓN DEL AÑO")
    assert [r["id"] for r in db.search_sessions(path, "canción")] == ["s1"]
    assert [r["id"] for r in db.search_sessions(path, "AÑO")] == ["s1"]
    assert db.search_sessions(path, "cancion") == []  # sin tilde NO es el mismo termino


def test_search_finds_terms_containing_quotes(path):
    """Regresion QA-004: el prefiltro corria sobre el JSON CRUDO, donde `"` va
    escapado como `\\"` -- ningun termino con comillas matcheaba."""
    add_message(path, "m1", text='el usuario dijo "hola mundo" ayer')
    assert [r["id"] for r in db.search_sessions(path, 'dijo "hola mundo"')] == ["s1"]


@pytest.mark.parametrize(
    "stored",
    [
        r"ruta C:\datos\stock.xlsx",
        "linea uno\nlinea dos",
        "100% del stock",
        "tabla_de_partes",
    ],
)
def test_search_treats_term_as_literal_substring(path, stored):
    """QA-004: `%`/`_`/backslash/salto de linea son texto literal, no comodines."""
    add_message(path, "m1", text=stored)
    assert [r["id"] for r in db.search_sessions(path, stored)] == ["s1"]
    assert db.search_sessions(path, "zzz" + stored) == []


def test_search_wildcards_do_not_match_anything(path):
    add_message(path, "m1", text="stock de partes")
    assert db.search_sessions(path, "%") == []  # antes: LIKE '%%%' matcheaba TODO
    assert db.search_sessions(path, "stoc_") == []
