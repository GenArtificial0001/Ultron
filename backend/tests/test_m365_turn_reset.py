"""Un turno nuevo olvida lo que se leyo en el anterior (spec office-outlook-teams-mcp REQ-M07b).

`session.m365_untrusted_read` dice «en este turno el modelo leyo texto que escribio otra persona». Es lo que vuelve riesgoso, en modo Auto, una accion que le habla a un tercero.
Si NO se reiniciara al empezar un turno, una lectura de ayer seguiria bloqueando acciones legitimas de hoy (la persona veria alarmas sin motivo y terminaria desactivando todo);
y al reves, la marca no puede perderse A MITAD de un turno. Lo que se vigila:
1. `_launch_turn` reinicia la marca ANTES de que el turno corra, con un mensaje de la persona y con uno sintetizado por el loop.
2. Un turno que corre despues de una lectura NO hereda la marca.
3. La politica sigue viendo la marca DENTRO del mismo turno (el reinicio es solo al empezar)."""

from __future__ import annotations

import pytest

from app import main
from app.m365 import policy
from app.session.events import UserTurn, get_session_store

SID = "sesion-m365-turn"


@pytest.fixture
def session():
    s = get_session_store().get_or_create(SID)
    yield s
    get_session_store().delete(SID)


@pytest.fixture
def no_real_turn(monkeypatch):
    """`_launch_turn` arma una tarea que llamaria al modelo: se corta justo antes, dejando solo lo que hace de forma sincrona."""
    created = []

    def fake_create_task(coro):
        coro.close()  # nada de red ni de modelo
        created.append(1)

        class _T:
            def done(self):
                return False

            def cancel(self):  # `SessionStore.delete` cancela la tarea al limpiar la sesion
                pass

        return _T()

    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    return created


@pytest.mark.parametrize("turn_id", ["a6b8f0e2-user-uuid", "loop-1234", "agent-abcd"])
def test_starting_a_turn_clears_the_mark_left_by_a_previous_read(session, no_real_turn, turn_id):
    session.m365_untrusted_read = True

    main._launch_turn(session, SID, UserTurn(id=turn_id, text="hola"))

    assert session.m365_untrusted_read is False, turn_id


def test_a_turn_that_follows_a_read_does_not_inherit_the_risk(session, no_real_turn):
    """El caso real: ayer se leyo un correo; hoy, en modo Auto, se le pide al modelo avisarle a un companero. No tiene que saltar la alarma."""
    session.loop_auto = True
    session.loop_active = True
    policy.note_read(session, "m365_get_emails")
    assert session.m365_untrusted_read is True

    main._launch_turn(session, SID, UserTurn(id="loop-nuevo", text="seguir"))

    assert policy.check("m365_draft_message", {"message": "hola", "to_person": "Ana Perez"}, session) is None


def test_the_mark_is_still_there_inside_the_same_turn(session):
    """El reinicio es solo al EMPEZAR un turno: dentro del mismo turno, leer y luego escribir a un tercero sigue siendo riesgo."""
    session.loop_auto = True
    session.loop_active = True
    policy.note_read(session, "m365_get_emails")

    blocked = policy.check("m365_draft_message", {"message": "reenvio", "to_person": "x@externo.com"}, session)

    assert blocked is not None and "(Riesgo)" in blocked


def test_starting_a_turn_does_not_forget_what_the_person_already_authorized(session, no_real_turn):
    """Reiniciar la marca de lectura NO borra las autorizaciones ya dadas (el reintento de una accion aprobada tiene que seguir pasando en el turno siguiente)."""
    args = {"message": "hola", "to_person": "Ana Perez"}
    policy.check("m365_draft_message", args, session)  # queda registrada la huella
    assert session.m365_write_seen

    main._launch_turn(session, SID, UserTurn(id="otro-turno", text="dale"))

    assert session.m365_write_seen, "las autorizaciones sobreviven al cambio de turno"
    assert policy.check("m365_draft_message", args, session) is None
