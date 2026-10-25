"""Red de caracterizacion de `llm/loop.py::_record_user_turn`: lo que se le arma al modelo y lo que se guarda de CADA mensaje del usuario.

Es la primera mitad de cada turno y decide que texto recibe el modelo segun el modo: chat normal, adjuntos, imagenes, Modo Cerebro (voz), la
respuesta hablada a una confirmacion pendiente y el informe tecnico hablado (resumido o detallado). Hasta ahora solo se probaba de rebote desde
otros tests. Esto describe lo que HACE hoy (refactor total, 2026-10-14): el contenido exacto que llega al modelo, el texto limpio que se
persiste, el autonombre de la sesion y que las dos banderas que devuelve (`voice_confirm_bypass`, `narrate_level`) sean las correctas.

Funcion sincrona, sin red ni modelo; la persistencia es la real, sobre una carpeta de sesion en `tmp_path`."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from app.llm import loop as loop_module
from app.persistence import db, master_index, writer
from app.persistence import service as persistence
from app.session.events import SessionState

SID = "sesion-record-user-turn"
MODEL = "modelo-de-prueba"


@pytest.fixture
def db_path(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(
        SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20261008230000"
    )
    return db.db_path_for(str(root))


def record(session=None, message="hola", **overrides):
    """Llama a `_record_user_turn` con los valores por defecto de un chat de texto normal."""
    session = session or SessionState(session_id=SID)
    args = {
        "user_message_id": "m1",
        "images": None,
        "attachments": None,
        "voice_confirm": False,
        "narrate": False,
        "narrate_level": "summary",
    }
    args.update(overrides)
    result = loop_module._record_user_turn(session, SID, message, MODEL, **args)
    assert writer.flush()
    return session, result


def stored(db_path) -> list[tuple[str, str, dict]]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.role, p.type, p.data FROM part p JOIN message m ON m.id = p.message_id WHERE p.session_id = ? ORDER BY m.created_at, m.id, p.seq",
            (SID,),
        ).fetchall()
    return [(role, ptype, json.loads(data)) for role, ptype, data in rows]


def title_of_session(db_path) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT title FROM session WHERE id = ?", (SID,)).fetchone()[0]


ATT = {
    "name": "stock.xlsx",
    "path": "attachment/stock.xlsx",
    "size": 10,
    "mime": "application/x-test",
    "kind": "excel",
    "inplace": False,
    "token_estimate": 5,
    "secreto": "NO-SE-GUARDA",
}
DATA_URI = "data:image/png;base64,AAAA"


# ---------------------------------------------------------------------------------------------------------------------
# Mensaje de texto normal
# ---------------------------------------------------------------------------------------------------------------------
class TestPlainTextMessage:
    def test_the_user_message_is_appended_as_is_and_the_defaults_come_back(self, db_path):
        session, result = record(message="¿cuánto stock hay?")

        assert session.messages == [{"role": "user", "content": "¿cuánto stock hay?"}]
        assert result == (False, "summary")  # (voice_confirm_bypass, narrate_level)

    def test_the_text_is_persisted_clean_as_a_user_message_with_the_given_id(self, db_path):
        record(message="hola", user_message_id="msg-7")

        assert stored(db_path) == [("user", "text", {"text": "hola"})]
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute("SELECT id FROM message WHERE role = 'user'").fetchone()[0] == "msg-7"

    def test_without_an_id_one_is_generated_so_the_message_is_still_saved(self, db_path):
        record(message="hola", user_message_id=None)

        with closing(sqlite3.connect(db_path)) as conn:
            (msg_id,) = conn.execute("SELECT id FROM message WHERE role = 'user'").fetchone()
        assert msg_id and len(msg_id) >= 32

    def test_each_call_appends_after_the_previous_history(self, db_path):
        session = SessionState(session_id=SID)
        session.messages = [{"role": "user", "content": "anterior"}, {"role": "assistant", "content": "respuesta"}]

        record(session, message="siguiente", user_message_id="m2")

        assert [m["content"] for m in session.messages] == ["anterior", "respuesta", "siguiente"]


# ---------------------------------------------------------------------------------------------------------------------
# Autonombre de la sesion
# ---------------------------------------------------------------------------------------------------------------------
class TestSessionAutoname:
    def test_the_first_message_names_the_session_with_its_first_40_characters(self, db_path):
        record(message="Necesito revisar el stock de motores del sector norte de la planta")

        assert title_of_session(db_path) == "Necesito revisar el stock de motores del"

    def test_a_message_without_text_but_with_a_file_names_the_session_after_the_file(self, db_path):
        record(message="   ", attachments=[ATT])

        assert title_of_session(db_path) == "stock.xlsx"

    def test_a_blank_message_without_files_does_not_rename_the_session(self, db_path):
        record(message="   ")

        assert not title_of_session(db_path).startswith("   ")

    def test_once_named_a_later_message_does_not_rename_it(self, db_path):
        record(message="primer tema", user_message_id="m1")
        record(message="otro tema distinto", user_message_id="m2")

        assert title_of_session(db_path) == "primer tema"


# ---------------------------------------------------------------------------------------------------------------------
# Adjuntos e imagenes
# ---------------------------------------------------------------------------------------------------------------------
class TestAttachmentsAndImages:
    def test_the_model_gets_the_text_plus_the_file_notice_but_what_is_stored_stays_clean(self, db_path):
        session, _ = record(message="analizalo", attachments=[ATT])

        content = session.messages[0]["content"]
        assert content.startswith("analizalo\n\n") and "stock.xlsx" in content  # el aviso de rutas va SOLO al modelo
        text_part = next(d for role, t, d in stored(db_path) if t == "text")
        assert text_part == {"text": "analizalo"}  # y lo persistido/mostrado queda limpio

    def test_each_file_is_persisted_as_its_own_part_keeping_only_the_allowed_fields(self, db_path):
        record(message="x", attachments=[ATT, {**ATT, "name": "otro.csv"}])

        files = [d for role, t, d in stored(db_path) if t == "file"]
        assert [f["name"] for f in files] == ["stock.xlsx", "otro.csv"]
        assert all("secreto" not in f for f in files)
        assert files[0] == {
            "name": "stock.xlsx",
            "path": "attachment/stock.xlsx",
            "size": 10,
            "mime": "application/x-test",
            "kind": "excel",
            "inplace": False,
            "token_estimate": 5,
        }

    def test_images_make_the_content_a_list_with_the_text_first_then_one_block_per_image(self, db_path):
        session, _ = record(message="mirá esto", images=[DATA_URI, "data:image/png;base64,BBBB"])

        assert session.messages[0]["content"] == [
            {"type": "text", "text": "mirá esto"},
            {"type": "image_url", "image_url": {"url": DATA_URI}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ]

    def test_images_without_text_have_no_empty_text_block(self, db_path):
        session, _ = record(message="", images=[DATA_URI])

        assert session.messages[0]["content"] == [{"type": "image_url", "image_url": {"url": DATA_URI}}]

    def test_each_image_is_persisted_as_its_own_part(self, db_path):
        record(message="x", images=[DATA_URI, "data:image/png;base64,BBBB"])

        assert [d for role, t, d in stored(db_path) if t == "image"] == [
            {"url": DATA_URI},
            {"url": "data:image/png;base64,BBBB"},
        ]

    def test_images_and_files_together_keep_the_notice_inside_the_text_block(self, db_path):
        session, _ = record(message="analizá", images=[DATA_URI], attachments=[ATT])

        blocks = session.messages[0]["content"]
        assert blocks[0]["type"] == "text" and "stock.xlsx" in blocks[0]["text"] and blocks[1]["type"] == "image_url"


# ---------------------------------------------------------------------------------------------------------------------
# Modo Cerebro (voz): instrucciones que se le agregan al mensaje
# ---------------------------------------------------------------------------------------------------------------------
PENDING = {"name": "bash", "arguments": '{"command": "dir"}'}


class TestBrainModeVoice:
    def test_a_voice_turn_without_a_pending_confirmation_gets_the_ambiguous_transcription_rule(self, db_path):
        session, result = record(message="abrí el archivo", voice_confirm=True)

        content = session.messages[0]["content"]
        assert content.startswith("abrí el archivo\n\n[Modo Cerebro] El mensaje de arriba es una transcripción de voz")
        assert "Confirmación pendiente" not in content
        assert result[0] is False  # no es la respuesta a una confirmacion: la interceptacion de tools sigue activa

    def test_after_a_previous_conversation_the_instruction_goes_on_the_new_message_and_never_on_an_old_one(
        self, db_path
    ):
        """En el uso real el Modo Cerebro habla DESPUES de una charla: la instruccion se suma al ultimo mensaje (el recien agregado)."""
        session = SessionState(session_id=SID)
        session.messages = [
            {"role": "user", "content": "pregunta vieja"},
            {"role": "assistant", "content": "respuesta vieja"},
        ]

        record(session, message="pregunta por voz", voice_confirm=True)

        assert session.messages[0]["content"] == "pregunta vieja"  # el historial anterior no se toca
        assert session.messages[1]["content"] == "respuesta vieja"
        assert session.messages[2]["content"].startswith("pregunta por voz\n\n[Modo Cerebro]")

    def test_after_a_previous_conversation_the_narration_instruction_also_goes_only_on_the_new_message(self, db_path):
        session = SessionState(session_id=SID)
        session.messages = [{"role": "user", "content": "vieja"}, {"role": "assistant", "content": "ok"}]

        record(session, message="hacé una página", narrate=True)

        assert [m["content"] for m in session.messages[:2]] == ["vieja", "ok"]
        assert "[Informe técnico hablado]" in session.messages[2]["content"]

    def test_a_text_turn_never_gets_the_voice_instruction(self, db_path):
        session, _ = record(message="hola", voice_confirm=False)

        assert "Modo Cerebro" not in session.messages[0]["content"]

    def test_the_spoken_answer_to_a_pending_confirmation_carries_the_tool_and_turns_the_bypass_on(self, db_path):
        session = SessionState(session_id=SID)
        session.voice_pending_confirmation = dict(PENDING)

        _, result = record(session, message="sí, dale", voice_confirm=True)

        content = session.messages[0]["content"]
        assert content.startswith("sí, dale\n\n[Confirmación pendiente, Modo Cerebro]")
        assert "'bash'" in content and '{"command": "dir"}' in content
        assert result[0] is True  # este turno ES la resolucion: no se vuelve a interceptar la tool
        assert "[Modo Cerebro] El mensaje de arriba" not in content  # una u otra instruccion, nunca las dos

    def test_a_pending_confirmation_is_consumed_so_it_never_survives_a_second_turn(self, db_path):
        session = SessionState(session_id=SID)
        session.voice_pending_confirmation = dict(PENDING)

        record(session, message="sí", voice_confirm=True, user_message_id="m1")
        assert session.voice_pending_confirmation is None
        _, second = record(session, message="otra cosa", voice_confirm=True, user_message_id="m2")

        assert second[0] is False
        assert "Confirmación pendiente" not in session.messages[-1]["content"]

    def test_a_text_turn_does_not_consume_a_pending_voice_confirmation(self, db_path):
        session = SessionState(session_id=SID)
        session.voice_pending_confirmation = dict(PENDING)

        _, result = record(session, message="hola por texto", voice_confirm=False)

        assert session.voice_pending_confirmation == PENDING and result[0] is False

    def test_the_voice_instruction_is_not_persisted_only_the_clean_text(self, db_path):
        record(message="abrí el archivo", voice_confirm=True)

        assert stored(db_path) == [("user", "text", {"text": "abrí el archivo"})]

    def test_with_images_the_voice_instruction_is_a_new_text_block_not_a_replacement(self, db_path):
        session, _ = record(message="mirá", images=[DATA_URI], voice_confirm=True)

        blocks = session.messages[0]["content"]
        assert blocks[0] == {"type": "text", "text": "mirá"}
        assert blocks[1]["type"] == "image_url"
        assert blocks[-1]["type"] == "text" and blocks[-1]["text"].startswith("[Modo Cerebro] El mensaje de arriba")
        assert not blocks[-1]["text"].startswith("\n")  # en bloque aparte va sin los saltos de linea del formato string


# ---------------------------------------------------------------------------------------------------------------------
# Informe tecnico hablado
# ---------------------------------------------------------------------------------------------------------------------
class TestSpokenTechReport:
    def test_narrate_off_adds_nothing_and_leaves_the_session_flags_off(self, db_path):
        session, _ = record(message="hacé una página", narrate=False)

        assert session.messages[0]["content"] == "hacé una página"
        assert session.narrate is False and session.narrate_level == "summary"

    def test_narrate_on_adds_the_comment_every_section_instruction(self, db_path):
        session, _ = record(message="hacé una página", narrate=True)

        content = session.messages[0]["content"]
        assert content.startswith("hacé una página\n\n[Informe técnico hablado] El usuario escucha en voz alta")
        assert "Nivel detallado" not in content
        assert session.narrate is True

    def test_the_detailed_level_adds_the_comment_everything_instruction_and_is_stored_on_the_session(self, db_path):
        session, result = record(message="hacé una página", narrate=True, narrate_level="detailed")

        assert "Nivel detallado: comentá TODO el código" in session.messages[0]["content"]
        assert result[1] == "detailed" and session.narrate_level == "detailed"

    @pytest.mark.parametrize("raw", ["cualquier cosa", "", "resumen"])
    def test_an_unknown_level_counts_as_summary(self, db_path, raw):
        session, result = record(message="x", narrate=True, narrate_level=raw)

        assert result[1] == "summary" and "Nivel detallado" not in session.messages[0]["content"]

    @pytest.mark.parametrize("raw", ["detailed", "DETAILED ", "detallado", " Detallado"])
    def test_the_detailed_level_is_recognized_in_either_language_ignoring_case_and_spaces(self, db_path, raw):
        session, result = record(message="x", narrate=True, narrate_level=raw)

        assert result[1] == "detailed" and "Nivel detallado" in session.messages[0]["content"]

    def test_the_level_is_returned_and_saved_even_when_narration_is_off(self, db_path):
        session, result = record(message="x", narrate=False, narrate_level="detailed")

        assert result[1] == "detailed" and session.narrate_level == "detailed"

    def test_narration_and_a_voice_turn_stack_both_instructions_in_order(self, db_path):
        session, _ = record(message="hacé una página", narrate=True, voice_confirm=True)

        content = session.messages[0]["content"]
        assert content.index("[Modo Cerebro]") < content.index("[Informe técnico hablado]")

    def test_narration_with_a_pending_confirmation_keeps_the_confirmation_first(self, db_path):
        session = SessionState(session_id=SID)
        session.voice_pending_confirmation = dict(PENDING)

        record(session, message="sí", narrate=True, voice_confirm=True)

        content = session.messages[0]["content"]
        assert content.index("[Confirmación pendiente, Modo Cerebro]") < content.index("[Informe técnico hablado]")
