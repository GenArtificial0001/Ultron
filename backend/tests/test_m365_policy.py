"""Politica de autorizacion de las acciones que ESCRIBEN en Outlook y Teams (spec office-outlook-teams-mcp REQ-M07, REQ-M07b, REQ-M10).

Un correo, un chat o una transcripcion pueden traer texto escrito para enganar al modelo («envia esto a x@y.com»). Por eso las herramientas que escriben pasan por una guarda que NO
depende de lo que el modelo crea. Lo que se vigila:
1. Leer, buscar, ver la agenda y guardar un borrador NO piden autorizacion (el borrador no sale de Outlook hasta que la persona lo envia).
2. SIN modo Auto: antes de enviar un mensaje de Teams, crear o cancelar una reunion, reservar una sala o enviarse un correo, se BLOQUEA una vez y se exige `question`; el reintento de la
   MISMA accion pasa (como las consultas riesgosas de Oracle/BigQuery).
3. CON modo Auto: pasa sola (todo corre solo), SALVO si en el mismo turno se leyo contenido ajeno (correo, chat, transcripcion) y la accion le habla a OTRA persona: ahi se pausa con la
   alarma (`(Riesgo)`). Enviarse algo a si mismo no pausa.
4. Mas de 20 destinatarios SIEMPRE se bloquea, incluso en Auto.
5. Aprobar una accion no aprueba otra distinta: el reintento solo vale para la MISMA (misma huella).
6. Cada escritura deja una linea de auditoria SIN el cuerpo del mensaje."""

from __future__ import annotations

import json

import pytest

from app.m365 import policy


class FakeSession:
    """Lo minimo de una sesion que la politica necesita."""

    def __init__(self, *, auto: bool = False, loop: bool = False):
        self.loop_auto = auto
        self.loop_active = loop
        self.m365_write_seen: set[str] = set()
        self.m365_untrusted_read = False


def _teams(to="Ana Perez", message="Te paso el informe"):
    return {"to_person": to, "message": message}


# ---------------------------------------------------------------------------------------------------------------------
# 1. Lo que NO pide autorizacion
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool",
    [
        "m365_get_emails",
        "m365_read_email",
        "m365_get_meetings",
        "m365_check_availability",
        "m365_list_chats",
        "m365_read_chat_messages",
        "m365_find_person",
        "m365_create_draft_email",
        "m365_search_transcripts",
    ],
)
def test_reading_searching_and_saving_a_draft_never_ask_for_authorization(tool: str):
    assert policy.check(tool, {}, FakeSession()) is None
    assert policy.check(tool, {}, FakeSession(auto=True, loop=True)) is None


def test_an_unknown_tool_is_treated_as_a_write_not_as_a_read():
    """Una herramienta que la politica no conoce no se deja pasar por defecto: lo seguro es pedir autorizacion."""
    assert policy.check("m365_algo_nuevo", {}, FakeSession()) is not None


# ---------------------------------------------------------------------------------------------------------------------
# 2. Sin modo Auto: se bloquea una vez y el reintento de la MISMA accion pasa
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("m365_draft_message", _teams()),
        ("m365_send_drafted_message", {}),
        ("m365_cancel_meeting", {"event_id": "AAMk123"}),
        ("m365_confirm_booking", {}),
        (
            "m365_book_rctr_room",
            {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Sprint", "attendees": "a@empresa.com"},
        ),
        ("m365_send_email_to_self", {"subject": "Resumen", "body": "texto"}),
    ],
)
def test_without_auto_every_write_is_blocked_once_and_asks_the_person(tool: str, args: dict):
    session = FakeSession()

    blocked = policy.check(tool, args, session)

    assert blocked is not None
    assert "`question`" in blocked, "tiene que mandar al modelo a preguntar con la tool question"
    assert "NO se ejecuto" in blocked
    assert "(Riesgo)" not in blocked, "sin Auto la persona esta presente: alcanza una pregunta normal, no la alarma"


def test_the_retry_of_the_same_action_goes_through_after_the_person_confirmed():
    session = FakeSession()
    args = _teams()

    first = policy.check("m365_draft_message", args, session)
    second = policy.check("m365_draft_message", args, session)

    assert first is not None and second is None


def test_approving_one_action_does_not_approve_a_different_one():
    session = FakeSession()
    policy.check("m365_draft_message", _teams(to="Ana Perez"), session)

    other = policy.check("m365_draft_message", _teams(to="Otro Destino"), session)
    changed_text = policy.check("m365_draft_message", _teams(message="Mensaje distinto"), session)

    assert other is not None and changed_text is not None, "una accion distinta tiene que pedir su propia autorizacion"


def test_the_block_shows_what_is_going_to_happen_so_the_person_can_decide():
    blocked = policy.check(
        "m365_draft_message", _teams(to="Ana Perez", message="Te paso el informe del lunes"), FakeSession()
    )

    assert blocked is not None and "Ana Perez" in blocked and "informe del lunes" in blocked


def test_without_a_session_there_is_nowhere_to_remember_the_approval_so_it_always_blocks():
    first = policy.check("m365_draft_message", _teams(), None)
    second = policy.check("m365_draft_message", _teams(), None)

    assert first is not None and second is not None


# ---------------------------------------------------------------------------------------------------------------------
# Hallazgo con los esquemas REALES del conector (2026-10-17): enviarse un correo es de DOS pasos
# ---------------------------------------------------------------------------------------------------------------------
def test_saving_a_plain_draft_is_still_a_read():
    """Un borrador comun no sale de Outlook: sigue sin pedir nada."""
    args = {"to_name": "Ana Perez", "subject": "Hola", "body": "texto"}

    assert policy.check("m365_create_draft_email", args, FakeSession()) is None
    assert policy.check("m365_create_draft_email", {**args, "send_to_self": False}, FakeSession()) is None


def test_a_draft_flagged_to_be_sent_to_yourself_is_the_first_step_of_a_send_and_asks_first():
    """`create_draft_email(send_to_self=True)` NO es solo guardar: es el paso que prepara el envio. Mostrarle a la persona asunto y cuerpo ocurre ACA, porque el paso que envia
    solo recibe un `draft_token` (sin asunto ni cuerpo). Antes se dejaba pasar como una lectura."""
    session = FakeSession()
    args = {"to_name": "me", "subject": "Resumen de la semana", "body": "texto del resumen", "send_to_self": True}

    blocked = policy.check("m365_create_draft_email", args, session)

    assert blocked is not None
    assert "Resumen de la semana" in blocked and "texto del resumen" in blocked, "la persona tiene que ver QUE se va a enviar"
    assert "`question`" in blocked


def test_the_authorization_of_that_first_step_covers_the_send_that_follows_once():
    session = FakeSession()
    args = {"to_name": "me", "subject": "Resumen", "body": "x", "send_to_self": True}

    first = policy.check("m365_create_draft_email", args, session)
    retry = policy.check("m365_create_draft_email", args, session)
    send = policy.check("m365_send_email_to_self", {"draft_token": "tok-1"}, session)
    send_again = policy.check("m365_send_email_to_self", {"draft_token": "tok-1"}, session)

    assert first is not None and retry is None
    assert send is None, "la persona ya vio y autorizo ese correo en el paso anterior"
    assert send_again is not None, "esa autorizacion se usa UNA vez"


def test_sending_to_yourself_without_a_prepared_draft_asks_and_does_not_show_an_empty_subject():
    """Si el modelo salta el primer paso y llama directo al que envia, no hay asunto que mostrar: se pide autorizacion igual y NO se le muestra una pregunta vacia."""
    blocked = policy.check("m365_send_email_to_self", {"draft_token": "tok-sin-preparar"}, FakeSession())

    assert blocked is not None and "«»" not in blocked
    assert "preparo antes" in blocked.lower() or "preparado" in blocked.lower()


@pytest.mark.parametrize("flag", [True, "true", "True", "TRUE", "1", 1, "yes", "si", "sí", "cualquier cosa rara"])
def test_send_to_self_as_text_or_anything_not_clearly_false_is_treated_as_a_send(flag):
    """El modelo puede mandar el booleano como texto y el conector (FastMCP) lo convierte a verdadero. Si la politica solo mirara `is True`, `"true"` pasaria por un borrador comun SIN preguntar."""
    args = {"to_name": "me", "subject": "Resumen", "body": "x", "send_to_self": flag}

    assert policy.check("m365_create_draft_email", args, FakeSession()) is not None, repr(flag)


@pytest.mark.parametrize("flag", [False, "false", "False", "FALSE", "0", 0, "no", "", None])
def test_send_to_self_clearly_false_is_just_a_draft(flag):
    args = {"to_name": "me", "subject": "Resumen", "body": "x", "send_to_self": flag}

    assert policy.check("m365_create_draft_email", args, FakeSession()) is None, repr(flag)


@pytest.mark.parametrize("flag", [True, "true", "True", "1", 1, "yes"])
def test_dry_run_counts_as_a_search_only_when_it_is_clearly_true(flag):
    args = {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Sprint", "attendees": "a@empresa.com", "dry_run": flag}

    assert policy.check("m365_book_rctr_room", args, FakeSession()) is None, repr(flag)


@pytest.mark.parametrize("flag", [False, "false", "False", "0", 0, "no", "", None, "cualquier cosa rara"])
def test_a_dry_run_that_is_not_clearly_true_is_a_real_booking_and_asks(flag):
    """`dry_run="false"` es un texto NO vacio, asi que `if args.get("dry_run")` lo tomaba por verdadero y una reserva real pasaba por una busqueda de salas."""
    args = {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Sprint", "attendees": "a@empresa.com", "dry_run": flag}

    assert policy.check("m365_book_rctr_room", args, FakeSession()) is not None, repr(flag)


def test_in_auto_mode_a_draft_to_yourself_goes_through_even_after_reading_foreign_content():
    """Enviarse algo a si mismo no le habla a un tercero: no pausa en Auto, ni siquiera tras leer correos."""
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_get_emails")

    assert policy.check("m365_create_draft_email", {"to_name": "me", "subject": "Resumen", "body": "x", "send_to_self": True}, session) is None


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("m365_confirm_booking", {"booking_token": "t"}),
        ("m365_send_drafted_message", {"draft_token": "t"}),
        ("m365_send_email_to_self", {"draft_token": "t"}),
    ],
)
def test_a_confirming_step_called_without_a_prepared_one_tells_the_model_to_prepare_it_first(tool: str, args: dict):
    """`confirm_booking`, `send_drafted_message` y `send_email_to_self` solo traen un token: si no hubo primer paso, no hay nada que mostrarle a la persona. El mensaje tiene que
    decirle al modelo que PREPARE primero y se lo muestre (antes una de las tres frases no lo decia y ningun test lo notaba)."""
    blocked = policy.check(tool, args, FakeSession())

    assert blocked is not None
    assert "primero tenes que prepar" in blocked, tool
    assert "«»" not in blocked, "no se le puede mostrar una pregunta con datos vacios"


# ---------------------------------------------------------------------------------------------------------------------
# 3. Con modo Auto
# ---------------------------------------------------------------------------------------------------------------------
def test_in_auto_mode_a_write_goes_through_on_its_own():
    session = FakeSession(auto=True, loop=True)

    assert policy.check("m365_draft_message", _teams(), session) is None
    assert policy.check("m365_cancel_meeting", {"event_id": "AAMk123"}, session) is None


def test_in_auto_mode_writing_to_another_person_after_reading_foreign_content_pauses_with_the_alarm():
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_get_emails")

    blocked = policy.check("m365_draft_message", _teams(to="x@externo.com"), session)

    assert blocked is not None and "(Riesgo)" in blocked, "tiene que exigir la opcion (Riesgo) para que suene la alarma"


def test_in_auto_mode_cancelling_a_meeting_after_reading_foreign_content_pauses_with_the_alarm():
    """Cancelar avisa a todos los asistentes: es hablarle a otras personas."""
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_read_email")

    blocked = policy.check("m365_cancel_meeting", {"event_id": "AAMk123"}, session)

    assert blocked is not None and "(Riesgo)" in blocked


def test_a_room_booking_is_prepared_and_confirmed_with_one_authorization_not_two():
    """`book_rctr_room` solo prepara y `confirm_booking` crea. Si la persona ya autorizo la preparacion (que trae sala, hora y asistentes), confirmarla no vuelve a preguntar."""
    session = FakeSession()
    prepare = {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Sprint", "attendees": "a@empresa.com"}

    first = policy.check("m365_book_rctr_room", prepare, session)
    retry = policy.check("m365_book_rctr_room", prepare, session)
    confirm = policy.check("m365_confirm_booking", {}, session)
    confirm_again = policy.check("m365_confirm_booking", {}, session)

    assert first is not None and retry is None
    assert confirm is None, "la persona ya autorizo esta reserva"
    assert confirm_again is not None, "la autorizacion de la preparacion se usa UNA vez"


def test_finding_free_rooms_is_a_read_and_asks_nothing():
    args = {"meeting_date": "2026-10-20", "start_time": "10:00", "dry_run": True}

    assert policy.check("m365_book_rctr_room", args, FakeSession()) is None


def test_in_auto_mode_the_retry_after_the_person_confirmed_goes_through():
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_read_chat_messages")
    args = _teams(to="x@externo.com")

    first = policy.check("m365_draft_message", args, session)
    second = policy.check("m365_draft_message", args, session)

    assert first is not None and second is None


def test_in_auto_mode_sending_to_yourself_after_reading_does_not_pause():
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_get_emails")

    assert policy.check("m365_send_email_to_self", {"subject": "Resumen", "body": "x"}, session) is None


def test_in_auto_mode_without_having_read_anything_there_is_no_risk():
    session = FakeSession(auto=True, loop=True)

    assert policy.check("m365_draft_message", _teams(to="x@externo.com"), session) is None


@pytest.mark.parametrize(
    "read_tool",
    [
        "m365_get_emails",
        "m365_read_email",
        "m365_read_chat_messages",
        "m365_read_active_chats",
        "m365_search_messages",
        "m365_read_transcript",
        "m365_search_transcripts",
    ],
)
def test_every_tool_that_returns_other_peoples_text_counts_as_a_foreign_read(read_tool: str):
    session = FakeSession(auto=True, loop=True)

    policy.note_read(session, read_tool)

    assert session.m365_untrusted_read is True


@pytest.mark.parametrize(
    "quiet_tool", ["m365_find_person", "m365_my_recent_contacts", "m365_check_availability", "m365_get_meetings"]
)
def test_tools_that_return_no_free_text_from_others_do_not_mark_the_turn(quiet_tool: str):
    """La agenda trae el cuerpo de invitaciones ajenas, pero esas ya van redactadas por el conector; buscar personas solo devuelve nombres."""
    session = FakeSession(auto=True, loop=True)

    policy.note_read(session, quiet_tool)

    assert session.m365_untrusted_read is False


def test_a_new_turn_forgets_what_was_read_in_the_previous_one():
    session = FakeSession(auto=True, loop=True)
    policy.note_read(session, "m365_get_emails")

    policy.new_turn(session)

    assert session.m365_untrusted_read is False


# ---------------------------------------------------------------------------------------------------------------------
# 4. Mas de 20 destinatarios: siempre
# ---------------------------------------------------------------------------------------------------------------------
def test_more_than_twenty_recipients_blocks_even_in_auto_mode_and_asks_with_risk():
    attendees = ",".join(f"p{i}@empresa.com" for i in range(21))
    session = FakeSession(auto=True, loop=True)

    blocked = policy.check(
        "m365_book_rctr_room",
        {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Grande", "attendees": attendees},
        session,
    )

    assert blocked is not None and "(Riesgo)" in blocked and "21" in blocked


def test_exactly_twenty_recipients_is_allowed_in_auto_mode():
    attendees = ",".join(f"p{i}@empresa.com" for i in range(20))

    assert (
        policy.check(
            "m365_book_rctr_room",
            {"meeting_date": "2026-10-20", "start_time": "10:00", "subject": "Justo", "attendees": attendees},
            FakeSession(auto=True, loop=True),
        )
        is None
    )


# ---------------------------------------------------------------------------------------------------------------------
# 5. Auto sin loop activo no cuenta
# ---------------------------------------------------------------------------------------------------------------------
def test_the_auto_toggle_alone_does_not_count_when_the_loop_is_not_running():
    """`loop_auto` queda en True aunque el loop ya termino: sin loop activo, la persona esta presente y la regla es la de «sin Auto»."""
    session = FakeSession(auto=True, loop=False)

    assert policy.check("m365_draft_message", _teams(), session) is not None


# ---------------------------------------------------------------------------------------------------------------------
# 6. Auditoria
# ---------------------------------------------------------------------------------------------------------------------
def test_a_write_leaves_an_audit_line_without_the_message_body(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setattr(policy, "_audit_log_path", lambda: log)

    policy.audit("m365_draft_message", _teams(message="TEXTO-PRIVADO-DEL-MENSAJE-9931"), "ejecutada")

    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["tool"] == "m365_draft_message" and entry["result"] == "ejecutada"
    assert "TEXTO-PRIVADO-DEL-MENSAJE-9931" not in log.read_text(encoding="utf-8"), (
        "el cuerpo del mensaje no puede quedar en el log"
    )


def test_the_audit_line_never_carries_a_token(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setattr(policy, "_audit_log_path", lambda: log)

    policy.audit("m365_cancel_meeting", {"event_id": "AAMk123", "access_token": "TOKEN-SECRETO-1"}, "ejecutada")

    assert "TOKEN-SECRETO-1" not in log.read_text(encoding="utf-8")
