"""Hallazgo de la revision (spec fix-detener-y-progreso-visible REQ-016, revisor de seguridad de datos): una sesion que chateo SIN
carpeta y despues recibio una perdia esos mensajes al recargar.

Sin carpeta, los mensajes viven solo en la memoria del backend (spec carpeta-por-sesion REQ-008: no hay ningun vision.db donde
escribir). Asignar la carpeta solo la registraba en el indice maestro, asi que SQLite arrancaba con el primer mensaje POSTERIOR. Desde
REQ-016 la pagina lee el chat de SQLite tambien con F5 y borra su copia en cuanto SQLite tiene mensajes de la sesion: lo de antes de la
carpeta desaparecia de la pantalla (y se perdia del todo con el proximo reinicio de Vision, aunque el modelo lo siguiera viendo).

Contrato que se prueba: al asignar la carpeta por PRIMERA vez, lo que la sesion ya hablo en memoria se guarda en su vision.db, en orden,
con los mismos ids de mensaje que usa la pagina (para que F5, ↻ y 🗑 lo encuentren) y antes de cualquier escritura nueva; nunca se pisa
una base que ya tenga mensajes de esa sesion y cambiar la carpeta despues no copia nada (como siempre). El e2e con el navegador real
esta en `test_e2e_chat_from_sqlite.py`.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, writer
from app.persistence import service as svc
from app.session.events import UserTurn, get_session_store

USER = "jorge"
IMG = "data:image/png;base64,iVBORw0KGgo="


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    """Mismo criterio que `test_persistence_service.py`: el usuario que filtra y el que registra al creador tienen que coincidir."""
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)


@pytest.fixture
def sync_writer(monkeypatch):
    """`submit` ejecuta el trabajo al instante (sin hilo escritor ni tiempos)."""
    monkeypatch.setattr(svc, "submit", lambda job: job())


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "proyecto"
    root.mkdir()
    return root


def count_messages(folder) -> int:
    with db._connect(db.db_path_for(str(folder))) as conn:
        return conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]


def chat_in_memory() -> list[dict]:
    """Lo que la sesion tiene en memoria (formato OpenAI, como `session.messages`): system prompt, una respuesta con una herramienta y
    un mensaje con imagen. Cada mensaje de usuario trae el id que usa la pagina y el texto que escribio la persona."""
    return [
        {"role": "system", "content": "prompt del sistema"},
        {"role": "user", "content": "hola [m1]", "message_id": "u-m1", "text": "hola [m1]"},
        {
            "role": "assistant",
            "content": "Dejame mirar",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.txt"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contenido de a.txt"},
        {"role": "assistant", "content": "eco [m1]"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "mirá esta [m2]"}, {"type": "image_url", "image_url": {"url": IMG}}],
            "message_id": "u-m2",
            "text": "mirá esta [m2]",
        },
        {"role": "assistant", "content": "eco [m2]"},
    ]


def shown(session_id: str) -> list[tuple[str, str]]:
    return [(m["role"], m["text"]) for m in svc.load_session_messages_for_display(session_id)]


EXPECTED_SHOWN = [
    ("user", "hola [m1]"),
    ("assistant", "Dejame mirar"),
    ("assistant", "eco [m1]"),
    ("user", "mirá esta [m2]"),
    ("assistant", "eco [m2]"),
]


def test_the_first_folder_saves_the_chat_held_in_memory_in_order_and_with_the_page_ids(folder, sync_writer):
    svc.assign_project_path("s1", str(folder), "Sesion", unsaved_history=chat_in_memory())

    display = svc.load_session_messages_for_display("s1")
    assert [(m["role"], m["text"]) for m in display] == EXPECTED_SHOWN
    assert display[0]["messageId"] == "u-m1" and display[3]["messageId"] == "u-m2", "↻/🗑 buscan el mensaje por el id de la pagina"
    # Lo que veria el modelo tras reiniciar Vision: lo mismo que tenia en memoria (sin el system prompt, que nunca se guarda).
    expected_model_view = [{k: v for k, v in m.items() if k not in ("message_id", "text")} for m in chat_in_memory()[1:]]
    assert svc.load_session_messages("s1") == expected_model_view
    assert [s["title"] for s in svc.list_persisted_sessions("s1")] == ["Sesion"], "el nombre elegido se sigue guardando"


def test_what_is_written_after_the_folder_comes_after_the_saved_chat(folder, sync_writer, monkeypatch):
    monkeypatch.setattr(svc.time, "time", lambda: 1_000.0)  # mismo instante para todo: el orden no depende de la resolucion del reloj
    svc.assign_project_path("s1", str(folder), unsaved_history=chat_in_memory())

    svc.record_message_start("s1", "u-m3", "user")
    svc.record_part_start("s1", "u-m3", "p-m3", "text", {"text": "despues [m3]"})

    assert shown("s1") == [*EXPECTED_SHOWN, ("user", "despues [m3]")]


def test_a_write_queued_right_after_assigning_never_skips_the_saved_chat(folder, monkeypatch):
    """Un turno que este corriendo escribe por la MISMA cola (en orden): lo guardado tiene que ir antes, o la base ya no estaria vacia y
    se saltaria todo lo de antes de la carpeta."""
    jobs: list = []
    monkeypatch.setattr(svc, "submit", jobs.append)
    svc.assign_project_path("s1", str(folder), unsaved_history=chat_in_memory())
    svc.record_message_start("s1", "u-m3", "user")
    svc.record_part_start("s1", "u-m3", "p-m3", "text", {"text": "despues [m3]"})

    for job in jobs:
        job()

    assert shown("s1") == [*EXPECTED_SHOWN, ("user", "despues [m3]")]


def test_changing_the_folder_later_copies_nothing(tmp_path, sync_writer):
    first, second = tmp_path / "primera", tmp_path / "segunda"
    first.mkdir()
    second.mkdir()
    svc.assign_project_path("s1", str(first))

    svc.assign_project_path("s1", str(second), unsaved_history=chat_in_memory())

    assert count_messages(second) == 0, "cambiar la carpeta despues no mueve ni copia nada (spec proyecto-por-sesion)"


def test_a_folder_that_already_has_messages_of_the_session_is_never_overwritten(folder, sync_writer):
    path = db.db_path_for(str(folder))
    db.ensure_session(path, "s1", 1.0)
    db.insert_message(path, "ya-estaba", "s1", "user", 1.0)
    db.insert_part(path, "p-ya", "ya-estaba", "s1", "text", {"text": "ya estaba [m0]"}, 1.0)

    svc.assign_project_path("s1", str(folder), unsaved_history=chat_in_memory())

    assert shown("s1") == [("user", "ya estaba [m0]")]


def test_a_session_without_chat_writes_no_message(folder, sync_writer):
    svc.assign_project_path("s1", str(folder), unsaved_history=[{"role": "system", "content": "prompt"}])

    assert count_messages(folder) == 0


def test_the_endpoint_saves_the_sessions_chat_with_the_ids_the_page_uses(folder, monkeypatch):
    """POST /api/sessions/{id}/project-path con la sesion en memoria: los mensajes de usuario llevan el id con que la pagina los mando
    (el del turno) y el texto que escribio la persona, sin las instrucciones que Vision le agrega al modelo (p. ej. el informe hablado)."""
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    sid = f"sin-carpeta-{uuid.uuid4().hex[:8]}"
    session = get_session_store().get_or_create(sid)
    session.messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "mensaje sin carpeta [m1]"},
        {"role": "assistant", "content": "eco [m1]"},
        {"role": "user", "content": "con voz [m2]\n\n[Informe técnico hablado] El usuario escucha en voz alta lo que hacés."},
        {"role": "assistant", "content": "eco [m2]"},
    ]
    for turn_id, text, index in (("id-m1", "mensaje sin carpeta [m1]", 1), ("id-m2", "con voz [m2]", 3)):
        turn = UserTurn(id=turn_id, text=text)
        turn.messages_start_index, turn.status = index, "done"
        session.turns[turn_id] = turn
    try:
        client = TestClient(main.app)
        r = client.post(f"/api/sessions/{sid}/project-path", json={"path": str(folder), "title": "Sesion"})
        assert r.status_code == 200, r.text
        writer.flush()

        got = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
        assert [(m["role"], m["text"]) for m in got] == [
            ("user", "mensaje sin carpeta [m1]"),
            ("assistant", "eco [m1]"),
            ("user", "con voz [m2]"),
            ("assistant", "eco [m2]"),
        ]
        assert [m["messageId"] for m in got if m["role"] == "user"] == ["id-m1", "id-m2"]
        assert svc.find_user_occurrence(sid, "id-m2") == 1, "tras un reinicio, editar/borrar ubica el mensaje por ese id"
    finally:
        get_session_store().delete(sid)
