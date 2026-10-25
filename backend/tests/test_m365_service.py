"""Coordinacion del inicio y cierre de sesion de Outlook y Teams (spec office-outlook-teams-mcp REQ-M02, REQ-M09, REQ-M11, REQ-M14).

El login real abre el navegador y espera a la persona: aca se reemplaza por un `runner` falso para probar lo que es de Vision. Lo que se vigila:
1. «Iniciar sesion» arranca UN solo login a la vez: un segundo toque no deja dos esperando (y no abre dos ventanas).
2. El estado que ve la pantalla distingue sin sesion, esperando el navegador, conectado y con error, y NUNCA lleva el token.
3. Si el login falla, la pantalla recibe la frase para la persona (no el texto crudo) y el estado vuelve a «sin sesion».
4. Cerrar sesion borra el token, corta un login en espera y es seguro de repetir.
5. El resultado del navegador (`/callback`) solo cuenta si coincide con el login en curso."""

from __future__ import annotations

import asyncio

import pytest

from app import secret_store
from app.m365 import auth, service, token_store

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

ACCESS = "ACCESS-TOKEN-SECRETO-8842"


def _save_token():
    from mcp.shared.auth import OAuthToken

    asyncio.run(
        token_store.M365TokenStorage().set_tokens(
            OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
        )
    )


@pytest.fixture(autouse=True)
def _fresh_service():
    service.reset_for_tests()
    yield
    service.reset_for_tests()


def test_without_anything_the_status_says_not_connected():
    status = service.status()

    assert status["state"] == "disconnected" and status["connected"] is False
    assert "message" not in status or not status["message"]


def test_the_status_with_a_saved_token_says_connected_and_never_shows_it():
    _save_token()

    status = service.status()

    assert status["state"] == "connected" and status["connected"] is True
    assert ACCESS not in repr(status)


def test_starting_the_login_reports_waiting_until_the_runner_finishes():
    async def scenario():
        gate = asyncio.Event()

        async def runner(port, flow):
            await gate.wait()

        first = await service.start_login(8020, runner=runner)
        waiting = service.status()
        gate.set()
        await service.wait_idle()
        return first, waiting, service.status()

    first, waiting, after = asyncio.run(scenario())

    assert first["state"] == "waiting" and waiting["state"] == "waiting"
    assert after["state"] == "disconnected"  # el runner de prueba no guardo ningun token


def test_a_second_tap_does_not_start_a_second_login():
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def runner(port, flow):
            calls.append(port)
            await gate.wait()

        await service.start_login(8020, runner=runner)
        await service.start_login(8020, runner=runner)
        await asyncio.sleep(0)
        gate.set()
        await service.wait_idle()
        return calls

    assert asyncio.run(scenario()) == [8020], "un segundo toque no puede abrir otro login"


def test_a_login_that_saves_the_token_ends_connected():
    async def scenario():
        async def runner(port, flow):
            from mcp.shared.auth import OAuthToken

            await token_store.M365TokenStorage().set_tokens(
                OAuthToken(access_token=ACCESS, token_type="Bearer", expires_in=4471, refresh_token="R" * 20)
            )

        await service.start_login(8020, runner=runner)
        await service.wait_idle()
        return service.status()

    assert asyncio.run(scenario())["state"] == "connected"


def test_a_failed_login_leaves_the_friendly_message_and_no_raw_text():
    async def scenario():
        async def runner(port, flow):
            raise auth.LoginTimeout()

        await service.start_login(8020, runner=runner)
        await service.wait_idle()
        return service.status()

    status = asyncio.run(scenario())

    assert status["state"] == "error" and status["connected"] is False
    assert "Iniciar sesión" in status["message"] and "Traceback" not in status["message"]
    assert status["code"] == "M365-LOGIN-DEMORA"


def test_an_unexpected_failure_is_translated_not_shown_raw():
    async def scenario():
        async def runner(port, flow):
            raise RuntimeError(f"fallo interno con access_token={ACCESS}")

        await service.start_login(8020, runner=runner)
        await service.wait_idle()
        return service.status()

    status = asyncio.run(scenario())

    assert status["state"] == "error"
    assert ACCESS not in repr(status) and "RuntimeError" not in repr(status)


def test_starting_again_after_an_error_clears_the_old_error():
    async def scenario():
        async def bad(port, flow):
            raise auth.LoginDenied()

        async def good(port, flow):
            await asyncio.sleep(0)

        await service.start_login(8020, runner=bad)
        await service.wait_idle()
        first = service.status()["state"]
        await service.start_login(8020, runner=good)
        second = service.status()
        await service.wait_idle()
        return first, second

    first, second = asyncio.run(scenario())

    assert first == "error" and second["state"] == "waiting" and not second.get("message")


def test_signing_out_deletes_the_token_and_cancels_a_pending_login():
    _save_token()  # fuera del bucle: `_save_token` usa asyncio.run y no puede llamarse desde una corrutina en marcha

    async def scenario():
        gate = asyncio.Event()

        async def runner(port, flow):
            await gate.wait()

        await service.start_login(8020, runner=runner)
        await service.sign_out()
        return service.status(), token_store.has_session()

    status, has_token = asyncio.run(scenario())

    assert has_token is False and status["state"] == "disconnected"


def test_signing_out_twice_is_harmless():
    async def scenario():
        await service.sign_out()
        await service.sign_out()
        return service.status()

    assert asyncio.run(scenario())["state"] == "disconnected"


def test_the_browser_result_only_counts_for_the_login_in_progress():
    async def scenario():
        captured = {}

        async def runner(port, flow):
            captured["flow"] = flow
            flow.begin(state="estado-del-login")
            await flow.wait(timeout=5)

        await service.start_login(8020, runner=runner)
        await asyncio.sleep(0.05)
        foreign = service.deliver_callback(code="x", state="otro-estado")
        legit = service.deliver_callback(code="el-codigo", state="estado-del-login")
        again = service.deliver_callback(code="el-codigo", state="estado-del-login")
        await service.wait_idle()
        return foreign, legit, again

    assert asyncio.run(scenario()) == (False, True, False)


def test_a_browser_result_with_no_login_in_progress_is_rejected():
    assert service.deliver_callback(code="x", state="y") is False
    assert service.deliver_error(state="y", reason="access_denied") is False


def test_a_browser_result_that_arrives_after_the_login_ended_is_rejected():
    """El login ya termino (o se cerro la sesion) y un codigo viejo llega tarde: no se le entrega a nadie. Antes `deliver_callback` solo miraba el `state`, que sigue guardado."""

    async def scenario():
        async def runner(port, flow):
            flow.begin(state="estado-del-login")  # deja un estado vivo en el flujo, como hace el SDK al abrir el navegador
            await asyncio.sleep(0)

        await service.start_login(8020, runner=runner)
        await service.wait_idle()  # el login termino
        return service.deliver_callback(code="tarde", state="estado-del-login"), service.deliver_error(state="estado-del-login", reason="access_denied")

    assert asyncio.run(scenario()) == (False, False)


def test_signing_out_actually_cancels_the_task_waiting_for_the_browser():
    """No alcanza con cambiar el estado: la tarea que espera al navegador tiene que CORTARSE, si no queda colgada (y podria completar un login despues de cerrar sesion)."""

    async def scenario():
        finished = []

        async def runner(port, flow):
            try:
                await asyncio.sleep(30)
                finished.append("termino solo")  # si llega aca, nadie la corto
            except asyncio.CancelledError:
                finished.append("cancelada")
                raise

        await service.start_login(8020, runner=runner)
        await asyncio.sleep(0)
        task = service._task  # noqa: SLF001 -- hay que mirar LA tarea, no solo el estado: sin cancelarla sigue viva y podria completar un login despues de cerrar sesion
        await service.sign_out()
        await asyncio.sleep(0)
        return finished, task.done()

    finished, task_done = asyncio.run(scenario())

    assert finished == ["cancelada"], "la tarea que espera al navegador no se corto"
    assert task_done is True, "la tarea sigue viva despues de cerrar sesion"

