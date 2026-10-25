"""spec adjuntar-archivos-chat REQ-003 e: editar o reenviar un mensaje CONSERVA sus adjuntos.

Hay tres caminos de edicion (`PATCH /api/chat/{sid}/message/{id}`) y los tres arman un turno o
reemplazan el mensaje con SOLO el texto nuevo: el aviso de rutas de los archivos y las imagenes se perdian,
y el agente editaba "a ciegas" respecto de lo que el usuario habia adjuntado."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.session.events import SessionState, UserTurn, get_session_store

NOTICE_HEAD = "[Archivos adjuntos en la carpeta de la sesión"
ATT = [{"name": "stock.csv", "path": "attachment/stock.csv", "size": 8, "kind": "csv", "inplace": False}]
IMG = ["data:image/jpeg;base64,AAAA"]


# ---------------------------------------------------------------------------
# Camino 1 (turno ACTIVO): replace_active_user_message conserva imagenes y aviso de archivos
# ---------------------------------------------------------------------------
def make_session_with_user_message(content):
    s = SessionState(session_id="edit-activo")
    s.messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": content}]
    return s


def test_editing_the_active_turn_keeps_the_attachment_notice():
    old = "analizalo\n\n" + NOTICE_HEAD + " — leelos con la tool `read` usando estas rutas]\n- attachment/stock.csv (CSV, 8 B)"
    s = make_session_with_user_message(old)

    assert s.replace_active_user_message("analizalo mejor") is True

    new = s.messages[-1]["content"]
    assert new.startswith("analizalo mejor") and NOTICE_HEAD in new and "attachment/stock.csv" in new
    assert "analizalo\n" not in new  # el texto viejo no queda


def test_editing_the_active_turn_keeps_the_images():
    content = [
        {"type": "text", "text": "mirá esto"},
        {"type": "image_url", "image_url": {"url": IMG[0]}},
    ]
    s = make_session_with_user_message(content)

    s.replace_active_user_message("mirá bien")

    new = s.messages[-1]["content"]
    assert isinstance(new, list)
    assert new[0] == {"type": "text", "text": "mirá bien"}
    assert [b["type"] for b in new] == ["text", "image_url"]
    assert new[1]["image_url"]["url"] == IMG[0]


def test_editing_keeps_both_images_and_the_files_notice():
    notice = NOTICE_HEAD + " — leelos con la tool `read` usando estas rutas]\n- attachment/stock.csv (CSV, 8 B)"
    content = [
        {"type": "text", "text": "mirá\n\n" + notice},
        {"type": "image_url", "image_url": {"url": IMG[0]}},
    ]
    s = make_session_with_user_message(content)

    s.replace_active_user_message("mirá todo")

    new = s.messages[-1]["content"]
    assert new[0]["text"].startswith("mirá todo") and "attachment/stock.csv" in new[0]["text"]
    assert new[1]["type"] == "image_url"


def test_editing_a_plain_text_message_still_works_as_before():
    s = make_session_with_user_message("hola")
    s.replace_active_user_message("chau")
    assert s.messages[-1] == {"role": "user", "content": "chau"}


def test_the_edited_message_still_moves_to_the_end():
    s = SessionState(session_id="edit-orden")
    s.messages = [
        {"role": "user", "content": "pedido"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "c", "content": "resultado"},
    ]
    s.replace_active_user_message("pedido editado")
    assert [m["role"] for m in s.messages] == ["assistant", "tool", "user"]  # comportamiento existente (REQ-004)


# ---------------------------------------------------------------------------
# Camino 2 (turno EN COLA): editar el texto no toca sus adjuntos
# ---------------------------------------------------------------------------
def test_editing_a_queued_turn_keeps_attachments_and_images():
    s = SessionState(session_id="edit-cola")
    s.enqueue_turn(UserTurn(id="t1", text="viejo", images=IMG, attachments=ATT))

    assert s.edit_queued_turn("t1", "nuevo") is True

    turn = s.turns["t1"]
    assert turn.text == "nuevo" and turn.images == IMG and turn.attachments == ATT


# ---------------------------------------------------------------------------
# Camino 3 (historial CERRADO): se relanza el turno y deben volver adjuntos e imagenes
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(main.app)


def test_resending_a_closed_message_relaunches_it_with_its_attachments(client, monkeypatch):
    sid = "edit-cerrado"
    s = get_session_store().get_or_create(sid)
    s.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "analizalo\n\n" + NOTICE_HEAD + " — x]\n- attachment/stock.csv (CSV, 8 B)"},
        {"role": "assistant", "content": "listo"},
    ]
    s.turns["m1"] = UserTurn(id="m1", text="analizalo", images=IMG, attachments=ATT, messages_start_index=1)
    launched: list[UserTurn] = []
    monkeypatch.setattr(main, "_launch_turn", lambda session, session_id, turn: launched.append(turn))
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)

    r = client.patch(f"/api/chat/{sid}/message/m1", json={"text": "analizalo con más detalle"})

    assert r.status_code == 200 and r.json()["case"] == "history_truncated_and_relaunched"
    [turn] = launched
    assert turn.text == "analizalo con más detalle"
    assert turn.attachments == ATT, "el reenvio perdio los archivos adjuntos"
    assert turn.images == IMG, "el reenvio perdio las imagenes"
