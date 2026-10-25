"""Eliminar una respuesta puntual del asistente (`DELETE /api/chat/{session_id}/response/{message_id}`, spec eliminar-respuestas, BL-006).

Es un borrado QUIRURGICO: saca solo el bloque assistant+tool que sigue a UN mensaje del usuario y deja el resto de la conversacion intacta (a diferencia de borrar un mensaje,
que trunca todo lo posterior). Tiene cuatro caminos con riesgo de perder datos ajenos si se equivocan: sesion viva, sesion reconstruida tras un reinicio (la memoria no trae ids),
turno que todavia se esta escribiendo, y mensajes inexistentes. Estaba en ~0 % de cobertura en la parte de memoria.

Sesion y SQLite reales en `tmp_path` (fixture `tmp_master_db`): se mira el estado FINAL de la base, sin llamar a ningun modelo.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import UserTurn, get_session_store

SID = "sesion-borrado"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_assert_session_owner", lambda session_id: None)
    return TestClient(main.app)


@pytest.fixture
def project(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(
        SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20260929120000"
    )
    return root


def seed_db(project):
    """Conversacion de 3 turnos en SQLite: u0 -> a0, u1 -> a1 (con una herramienta), u2 -> a2."""
    path = db.db_path_for(str(project))
    db.ensure_session(path, SID, 1.0)
    t = 1.0
    for i in range(3):
        db.insert_message(path, f"u{i}", SID, "user", t)
        db.insert_part(path, f"u{i}-t", f"u{i}", SID, "text", {"text": f"pregunta {i}"}, t)
        db.insert_message(path, f"a{i}", SID, "assistant", t + 0.5)
        db.insert_part(path, f"a{i}-t", f"a{i}", SID, "text", {"text": f"respuesta {i}"}, t + 0.5)
        t += 1.0
    return path


def roles_and_text(path):
    out = []
    for m in db.load_messages(path, SID):
        text = m["content"] if isinstance(m["content"], str) else "<lista>"
        out.append((m["role"], text))
    return out


def live_session(with_turns=True):
    """Sesion EN MEMORIA con el mismo historial que la base. `with_turns=False` simula una sesion reconstruida: solo `messages`, sin ids de turno."""
    session = get_session_store().get_or_create(SID)
    session.messages.clear()
    session.turns.clear()
    msgs = []
    for i in range(3):
        msgs.append({"role": "user", "content": f"pregunta {i}"})
        if i == 1:
            msgs.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
                }
            )
            msgs.append({"role": "tool", "tool_call_id": "c1", "content": "resultado de la herramienta"})
        msgs.append({"role": "assistant", "content": f"respuesta {i}"})
    session.messages.extend(msgs)
    if with_turns:
        starts = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        for n, idx in enumerate(starts):
            turn = UserTurn(id=f"u{n}", text=f"pregunta {n}", status="done", messages_start_index=idx)
            session.turns[turn.id] = turn
    return session


@pytest.fixture(autouse=True)
def clean_session():
    yield
    get_session_store().delete(SID)


def delete(client, message_id):
    return client.delete(f"/api/chat/{SID}/response/{message_id}")


# ---------------------------------------------------------------------------
# Sesion viva: el turno se identifica por su id
# ---------------------------------------------------------------------------
class TestLiveSession:
    def test_it_removes_only_the_response_block_of_that_turn(self, client, project):
        path = seed_db(project)
        session = live_session()

        r = delete(client, "u0")
        writer.flush()

        assert r.status_code == 200 and r.json() == {"case": "response_deleted", "message_id": "u0"}
        assert [m["content"] for m in session.messages if m["role"] == "user"] == [
            "pregunta 0",
            "pregunta 1",
            "pregunta 2",
        ]  # ninguna pregunta se pierde
        assert "respuesta 0" not in [m.get("content") for m in session.messages]
        assert "respuesta 1" in [m.get("content") for m in session.messages] and "respuesta 2" in [
            m.get("content") for m in session.messages
        ]
        assert roles_and_text(path) == [
            ("user", "pregunta 0"),
            ("user", "pregunta 1"),
            ("assistant", "respuesta 1"),
            ("user", "pregunta 2"),
            ("assistant", "respuesta 2"),
        ]

    def test_the_whole_assistant_and_tool_block_goes_but_never_the_next_user_turn(self, client, project):
        seed_db(project)
        session = live_session()

        delete(client, "u1")  # u1 tiene assistant(tool_calls) + tool + assistant

        assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "user", "assistant"]
        assert [m["content"] for m in session.messages if m["role"] == "user"] == [
            "pregunta 0",
            "pregunta 1",
            "pregunta 2",
        ]

    def test_the_last_turn_can_be_deleted_too(self, client, project):
        seed_db(project)
        session = live_session()

        r = delete(client, "u2")

        assert r.status_code == 200
        assert session.messages[-1] == {"role": "user", "content": "pregunta 2"}

    def test_a_turn_without_a_response_is_a_404_and_changes_nothing(self, client, project):
        seed_db(project)
        session = live_session()
        del session.messages[-1]  # u2 queda sin respuesta
        before = list(session.messages)

        r = delete(client, "u2")

        assert r.status_code == 404 and "todavia no tiene ninguna respuesta" in r.json()["detail"]
        assert session.messages == before

    def test_deleting_twice_is_a_404_the_second_time(self, client, project):
        seed_db(project)
        live_session()
        assert delete(client, "u0").status_code == 200

        assert delete(client, "u0").status_code == 404

    def test_sqlite_and_memory_stay_consistent_after_several_deletions(self, client, project):
        path = seed_db(project)
        session = live_session()

        assert delete(client, "u2").status_code == 200
        # u0 no cambio de posicion porque el borrado anterior fue POSTERIOR a el.
        assert delete(client, "u0").status_code == 200
        writer.flush()

        # Memoria: las 3 preguntas siguen y solo queda la respuesta del turno del medio (con su herramienta).
        assert [m["role"] for m in session.messages] == ["user", "user", "assistant", "tool", "assistant", "user"]
        assert [m.get("content") for m in session.messages if m["role"] == "assistant" and m.get("content")] == ["respuesta 1"]
        # SQLite dice lo mismo que la memoria.
        assert roles_and_text(path) == [("user", "pregunta 0"), ("user", "pregunta 1"), ("assistant", "respuesta 1"), ("user", "pregunta 2")]


# ---------------------------------------------------------------------------
# Turno activo: se frena, no se borra
# ---------------------------------------------------------------------------
class TestActiveTurn:
    def test_the_turn_being_written_cannot_be_deleted(self, client, project, monkeypatch):
        seed_db(project)
        session = live_session()
        session.active_turn_id = "u2"
        monkeypatch.setattr(type(session), "is_busy", property(lambda self: True))
        before = list(session.messages)

        r = delete(client, "u2")

        assert r.status_code == 409 and "Detener" in r.json()["detail"]
        assert session.messages == before

    def test_the_active_id_without_being_busy_is_not_blocked(self, client, project, monkeypatch):
        """El 409 solo aplica si ADEMAS esta ocupada: un `active_turn_id` viejo en una sesion ociosa no impide borrar."""
        seed_db(project)
        session = live_session()
        session.active_turn_id = "u2"
        monkeypatch.setattr(type(session), "is_busy", property(lambda self: False))

        assert delete(client, "u2").status_code == 200

    def test_busy_but_another_turn_is_active_does_not_block_an_old_one(self, client, project, monkeypatch):
        seed_db(project)
        session = live_session()
        session.active_turn_id = "u2"
        monkeypatch.setattr(type(session), "is_busy", property(lambda self: True))

        assert delete(client, "u0").status_code == 200


# ---------------------------------------------------------------------------
# Sesion reconstruida tras un reinicio: la memoria no trae ids de turno
# ---------------------------------------------------------------------------
class TestRebuiltSession:
    def test_the_user_message_is_found_by_its_id_in_sqlite(self, client, project):
        path = seed_db(project)
        session = live_session(with_turns=False)  # sin session.turns, como despues de reiniciar

        r = delete(client, "u1")
        writer.flush()

        assert r.status_code == 200 and r.json()["case"] == "response_deleted"
        assert [m["content"] for m in session.messages if m["role"] == "user"] == [
            "pregunta 0",
            "pregunta 1",
            "pregunta 2",
        ]
        assert ("assistant", "respuesta 1") not in roles_and_text(path)
        assert ("assistant", "respuesta 0") in roles_and_text(path) and ("assistant", "respuesta 2") in roles_and_text(
            path
        )

    def test_memory_loses_the_whole_assistant_and_tool_block_of_that_turn(self, client, project):
        seed_db(project)
        session = live_session(with_turns=False)

        delete(client, "u1")

        assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "user", "assistant"]

    def test_the_idx_form_resolves_by_position_from_the_end(self, client, project):
        """'idx:N' = el N-esimo mensaje del usuario contando desde el FINAL (0 = el ultimo)."""
        seed_db(project)
        session = live_session(with_turns=False)

        r = delete(client, "idx:0")  # el ultimo user = u2

        assert r.status_code == 200
        assert session.messages[-1] == {"role": "user", "content": "pregunta 2"}

    def test_an_unknown_id_is_a_404_and_changes_nothing(self, client, project):
        path = seed_db(project)
        session = live_session(with_turns=False)
        before_memory, before_db = list(session.messages), roles_and_text(path)

        r = delete(client, "no-existe")
        writer.flush()

        assert r.status_code == 404 and "No encontré esa pregunta" in r.json()["detail"]
        assert session.messages == before_memory and roles_and_text(path) == before_db

    def test_an_index_pointing_past_the_end_does_not_crash(self, client, project):
        """Antes un indice fuera de rango daba IndexError (500). Ahora cae al fallback de SQLite y, si no esta, un 404 claro."""
        seed_db(project)
        session = live_session()
        session.turns["u2"].messages_start_index = 999

        r = delete(client, "u2")

        assert r.status_code in (200, 404) and r.status_code != 500

    def test_a_negative_index_is_not_read_as_a_position_from_the_end(self, client, project):
        """En Python `messages[-1]` es el ULTIMO mensaje. Si el ultimo es una pregunta del usuario (turno aun sin respuesta), un indice -1 pasaria la
        comprobacion 'es un mensaje user' y el borrado actuaria sobre el turno equivocado. Se tiene que rechazar el indice y resolver por SQLite."""
        seed_db(project)
        session = live_session()
        del session.messages[-1]  # el ultimo turno queda sin respuesta: el ultimo mensaje es un 'user'
        assert session.messages[-1]["role"] == "user"
        session.turns["u0"].messages_start_index = -1

        r = delete(client, "u0")

        assert r.status_code == 200, r.text  # con el indice negativo aceptado daria 404 ('todavia no tiene respuesta')
        assert "respuesta 0" not in [m.get("content") for m in session.messages]
        assert [m["content"] for m in session.messages if m["role"] == "user"] == ["pregunta 0", "pregunta 1", "pregunta 2"]

    def test_an_index_that_points_to_a_non_user_message_falls_back_to_sqlite(self, client, project):
        seed_db(project)
        session = live_session()
        session.turns["u0"].messages_start_index = 1  # apunta a un mensaje 'assistant'

        r = delete(client, "u0")

        assert r.status_code == 200
        assert "respuesta 0" not in [m.get("content") for m in session.messages]
        assert [m["content"] for m in session.messages if m["role"] == "user"] == [
            "pregunta 0",
            "pregunta 1",
            "pregunta 2",
        ]


# ---------------------------------------------------------------------------
# Sesion inexistente
# ---------------------------------------------------------------------------
def test_a_session_that_is_not_open_says_to_reload(client, monkeypatch):
    class Closed:
        def get_or_create(self, session_id):
            return None

    monkeypatch.setattr(main, "get_session_store", lambda: Closed())

    r = client.delete("/api/chat/otra/response/x")

    assert r.status_code == 404 and "Recargá la página" in r.json()["detail"]
