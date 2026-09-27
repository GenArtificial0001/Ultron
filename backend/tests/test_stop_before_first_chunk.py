"""BUG REAL reportado por el usuario (2026-09-16, "el Detener demora
muchisimo, deberia ser corto y rapido"): antes, `_stream_chat_completion`
(app/llm/loop.py) esperaba con un `queue.Queue.get()` SIN timeout el primer
chunk del proveedor de IA -- si el modelo todavia no habia emitido ni un token (el
caso mas comun de espera larga, ver el incidente real de 13:44 min en
perf.log), el chequeo de `session.stop_requested` nunca se alcanzaba porque
vivia adentro del `async for chunk in stream` del llamador, que solo corre
cuando YA llego un chunk de la cola. Apretar "Detener" no tenia ningun
efecto hasta que el modelo arrancara a responder por su cuenta, sin importar
cuanto tiempo pasara.

Este test prueba, con un stream FALSO que nunca emite nada hasta que se lo
cierra explicitamente (simulando al proveedor de IA "pensando" sin limite), que:
1. Pedir un stop MIENTRAS se espera el primer chunk hace que el generador
   termine (sin yieldear nada) en el proximo poll (<=300ms), no cuando a
   el proveedor de IA se le ocurra responder.
2. Se llama a `stream.close()` -- la conexion HTTP real se corta, no solo
   se deja de leer.

Sin pytest-asyncio (no es dependencia del proyecto): el test es un `def`
sincronico que arranca su propio loop con `asyncio.run(...)`.
"""

from __future__ import annotations

import asyncio
import threading

from app.llm.loop import _stream_chat_completion


class _FakeStream:
    """Simula el iterador sincronico que devuelve `client.chat.completions.
    create(stream=True)` -- nunca produce un chunk hasta que se llama a
    `.close()` (igual que un el proveedor de IA que todavia esta "pensando")."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.close_called = False

    def __iter__(self):
        return self

    def __next__(self):
        self._closed.wait(timeout=5)
        raise StopIteration

    def close(self) -> None:
        self.close_called = True
        self._closed.set()


class _FakeCompletions:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def create(self, **kwargs):
        return self._stream


class _FakeChat:
    def __init__(self, stream: _FakeStream) -> None:
        self.completions = _FakeCompletions(stream)


class _FakeClient:
    def __init__(self, stream: _FakeStream) -> None:
        self.chat = _FakeChat(stream)


class _FakeSession:
    def __init__(self) -> None:
        self.stop_requested = False


def test_stop_while_waiting_first_chunk_closes_stream_promptly():
    async def _run():
        fake_stream = _FakeStream()
        client = _FakeClient(fake_stream)
        session = _FakeSession()

        gen = _stream_chat_completion(client, session, model="x", messages=[])

        async def _consume():
            items = []
            async for item in gen:
                items.append(item)
            return items

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)  # deja que el thread productor arranque
        session.stop_requested = True

        # Con el bug original esto colgaba hasta el timeout del `wait()`
        # de _FakeStream (5s) -- el fix debe cortarlo en el proximo poll
        # (250ms), con margen generoso para CI lento.
        items = await asyncio.wait_for(task, timeout=2.0)
        return items, fake_stream.close_called

    items, close_called = asyncio.run(_run())
    assert items == []
    assert close_called is True


def test_normal_stream_without_stop_yields_all_chunks():
    """No debe romper el caso feliz: sin stop pedido, los chunks se
    yieldean todos y el generador termina solo cuando la cola real se
    agota (sentinela), sin llamar a `close()`."""

    class _RealishStream:
        def __init__(self) -> None:
            self._items = iter(["a", "b", "c"])
            self.close_called = False

        def __iter__(self):
            return self._items

        def close(self) -> None:
            self.close_called = True

    async def _run():
        fake_stream = _RealishStream()
        client = _FakeClient(fake_stream)  # type: ignore[arg-type]
        session = _FakeSession()

        items = []
        async for item in _stream_chat_completion(
            client, session, model="x", messages=[]
        ):
            items.append(item)
        return items, fake_stream.close_called

    items, close_called = asyncio.run(_run())
    assert items == ["a", "b", "c"]
    assert close_called is False
