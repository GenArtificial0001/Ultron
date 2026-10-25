"""REQ-014 (spec fix-detener-y-progreso-visible): Detener y la cola de mensajes, segun la regla del usuario (D3):
«Detener como hoy (al detener arranca el siguiente mensaje en cola), pero si elimino el mensaje con el botón borrar
mensaje en cola, se elimina de la cola». Un mensaje que el usuario BORRA no se responde nunca.

Contrato observable que se prueba (spec, criterio REQ-014 a-f; el resto de la cola es el de specs/parada-y-cola-mensajes):
  (a) borrar un mensaje que sigue en la cola: sale de la cola y nunca llega al modelo; Detener arranca el siguiente NO borrado.
  (b) borrar un mensaje cuyo turno YA arranco (el borrado llego tarde: modal de confirmacion abierto mientras el turno
      anterior terminaba, o despues de Detener) equivale a Detener ese turno y descartarlo: nunca termina de responderse
      ('done'), y ni su mensaje ni su respuesta parcial quedan en memoria ni en SQLite; los turnos anteriores quedan intactos.
  (c) al borrar un mensaje en cola se CIERRA su conexion enseguida (antes quedaba abierta para siempre: el boton y
      «Vision está trabajando…» quedaban pegados en el navegador).
  (d) cuando un mensaje sale de la cola y arranca se avisa con 'dequeued' (con la cola que queda, para renumerar).
  (e) borrar, detener y desencolar no chocan: nunca un 500 ni un ValueError.
  (f) Detener con el id del turno que el usuario ve solo frena ESE turno: si ya termino y arranco el siguiente de la
      cola, no lo frena (stopped:false con un motivo). Sin id, como hoy.

Como se prueba: backend REAL en loopback (`e2e_support.serve_backend`, uvicorn en un hilo), el proveedor de IA FALSO y lento
(stream con pausas entre chunks; anota que mensajes de usuario le llegaron de verdad) y HTTP crudo con `requests` (sin
navegador). "Se mando" = el el proveedor de IA falso recibio ese texto. La base viene del experimento HTTP del diagnostico
(spec, seccion D «Cola»): sus casos `xfail` pasaron a ser asserts normales.

Desde backend/:  python -m pytest tests/test_queue_delete_and_stop.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from e2e_support import FakeWhisper, serve_backend

# Backend real + red de loopback, sin navegador: no es e2e (no abre Chromium) pero tampoco es unit.
pytestmark = pytest.mark.integration

MODEL = "claude-sonnet-4-6"
READ_TIMEOUT_S = 25  # un stream sin datos por mas de esto se corta del lado del cliente (zombi)
T0 = time.monotonic()


def now() -> float:
    return round(time.monotonic() - T0, 3)


# ---------------------------------------------------------------------------------------------------------------------
# El proveedor de IA falso y lento
# ---------------------------------------------------------------------------------------------------------------------
def _chunk(content=None, finish=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=[]), finish_reason=finish)],
        usage=None,
    )


def _user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            return str(c or "")
    return ""


class SlowStream:
    """Iterador sincronico como el de `chat.completions.create(stream=True)`: un chunk cada `step` segundos durante
    `seconds`. `close()` (lo llama el loop al Detener) lo corta. `on_last` corre justo antes del chunk final."""

    def __init__(self, tag: str, seconds: float, on_last=None, step: float = 0.1):
        n = max(1, int(seconds / step))
        self._pieces = [_chunk("[[TIPO: conversacional]]")] + [_chunk(f"{tag}·") for _ in range(n)]
        self._pieces.append(_chunk(None, "stop"))
        self._step, self._i, self._closed, self._on_last = step, 0, threading.Event(), on_last

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed.is_set() or self._i >= len(self._pieces):
            raise StopIteration
        if 0 < self._i < len(self._pieces) - 1:
            self._closed.wait(self._step)
            if self._closed.is_set():
                raise StopIteration
        if self._i == len(self._pieces) - 1 and self._on_last is not None:
            self._on_last()
        piece = self._pieces[self._i]
        self._i += 1
        return piece

    def close(self):
        self._closed.set()


class FakeLLM:
    """Anota (instante, tag) de cada mensaje de usuario que le llega. El tag es el `[XXX]` del texto; `LENTO=n`
    fija cuantos segundos tarda en responder (default 0.3 s)."""

    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.calls: list[tuple[float, str]] = []
        self.on_last: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def tags(self, prefix: str = "") -> list[str]:
        with self._lock:
            return [t for _, t in self.calls if t.startswith(prefix)]

    def _create(self, **kwargs):
        if not kwargs.get("stream"):
            msg = SimpleNamespace(content="resumen", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)
        text = _user_text(kwargs.get("messages") or [])
        m = re.search(r"\[([\w-]+)\]", text)
        tag = m.group(1) if m else "?"
        s = re.search(r"LENTO=(\d+(?:\.\d+)?)", text)
        with self._lock:
            self.calls.append((now(), tag))
        return SlowStream(tag, float(s.group(1)) if s else 0.3, on_last=self.on_last.pop(tag, None))


# ---------------------------------------------------------------------------------------------------------------------
# Cliente HTTP (sin navegador)
# ---------------------------------------------------------------------------------------------------------------------
def _http() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # nunca un proxy del entorno para loopback
    return s


class Turn:
    """Un POST /api/chat con su stream SSE leido en un hilo propio: eventos con instante y si la conexion cerro."""

    def __init__(self, base: str, sid: str, tag: str, slow: float | None = None):
        self.base, self.sid, self.tag = base, sid, tag
        self.mid = f"{tag}-{uuid.uuid4().hex[:6]}"
        self.text = f"mensaje [{tag}]" + (f" LENTO={slow}" if slow else "")
        self.events: list[tuple[float, str, dict]] = []
        self.status: int | None = None
        self.closed_at: float | None = None
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        body = {"session_id": self.sid, "message": self.text, "message_id": self.mid, "model": MODEL}
        try:
            with _http().post(f"{self.base}/api/chat", json=body, stream=True, timeout=(5, READ_TIMEOUT_S)) as r:
                self.status = r.status_code
                for line in r.iter_lines(chunk_size=1):
                    if line.startswith(b"data:"):
                        ev = json.loads(line[5:].strip())
                        self.events.append((now(), ev.get("type"), ev.get("data") or {}))
            self.closed_at = now()
        except Exception as exc:  # noqa: BLE001 -- se registra: un ReadTimeout = stream que nunca cerro
            self.error = type(exc).__name__

    def types(self) -> list[str]:
        out: list[str] = []
        for _, t, _ in self.events:
            if t == "text-delta" and out and out[-1] == "text-delta":
                continue  # compacta rafagas de texto
            out.append(t)
        return out

    def of_type(self, typ: str) -> list[dict]:
        return [d for _, t, d in self.events if t == typ]

    def wait_type(self, typ: str, timeout: float = 10) -> bool:
        return wait_until(lambda: any(t == typ for _, t, _ in self.events), timeout)

    def is_open(self) -> bool:
        return self.thread.is_alive()

    def summary(self) -> dict:
        return {
            "tag": self.tag,
            "eventos": self.types(),
            "cerro_en": self.closed_at,
            "sigue_abierto": self.is_open(),
            "error_cliente": self.error,
        }


def delete(base, sid, mid) -> dict:
    r = _http().delete(f"{base}/api/chat/{sid}/message/{mid}", timeout=10)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:200]
    return {"t": now(), "status": r.status_code, "body": body}


def stop(base, sid, turn_id: str | None = None) -> dict:
    """Detener. Con `turn_id` = el turno que el usuario esta viendo (REQ-014 f); sin el, como hoy."""
    kwargs: dict[str, Any] = {"timeout": 10}
    if turn_id is not None:
        kwargs["json"] = {"turn_id": turn_id}
    r = _http().post(f"{base}/api/chat/{sid}/stop", **kwargs)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:200]
    return {"t": now(), "status": r.status_code, "body": body}


def status(base, sid) -> dict:
    return _http().get(f"{base}/api/session/{sid}/status", timeout=10).json()


def wait_until(pred, timeout: float = 10, every: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(every)
    return False


def wait_idle(base, sid, timeout: float = 15) -> bool:
    return wait_until(lambda: not status(base, sid)["busy"], timeout, every=0.1)


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def memory(sid: str) -> list[tuple[str, str]]:
    """Lo que el backend tiene EN MEMORIA (el servidor corre en ESTE proceso, en otro hilo), sin el system prompt."""
    from app.session.events import get_session_store

    s = get_session_store().get(sid)
    if s is None:
        return []
    return [(m.get("role"), _text_of(m.get("content"))) for m in s.messages if m.get("role") != "system"]


def queue_of(sid: str) -> list[str]:
    from app.session.events import get_session_store

    s = get_session_store().get(sid)
    return list(s.queue_order) if s is not None else []


def flush_writes() -> None:
    """Espera a que el hilo escritor de SQLite termine lo pendiente (las escrituras van por una cola en orden)."""
    from app.persistence.writer import flush

    flush()


def sqlite_view(base, sid) -> list[tuple[str, str]]:
    """Lo que ve el chat al leer de SQLite (cambiar de sesion / otra PC)."""
    flush_writes()
    r = _http().get(f"{base}/api/sessions/{sid}/messages", timeout=10)
    assert r.status_code == 200, r.text
    return [(m.get("role"), str(m.get("text") or "")) for m in r.json()["messages"]]


def sqlite_reload(sid: str) -> list[tuple[str, str]]:
    """Lo que el MODELO veria tras reiniciar Vision (historial reconstruido desde SQLite)."""
    from app.persistence import service as persistence

    flush_writes()
    return [(m.get("role"), _text_of(m.get("content"))) for m in persistence.load_session_messages(sid)]


def mentions(rows: list[tuple[str, str]], tag: str) -> list[tuple[str, str]]:
    """Filas que tienen algo del mensaje `tag`: su texto de usuario («[tag]») o su respuesta («tag·»)."""
    return [r for r in rows if f"[{tag}]" in r[1] or f"{tag}·" in r[1]]


def new_session(base: str, project: Path) -> str:
    """Sesion con carpeta de proyecto real (SQLite por sesion, como en el uso diario)."""
    sid = f"cola-{uuid.uuid4().hex[:10]}"
    project.mkdir(parents=True, exist_ok=True)
    r = _http().post(f"{base}/api/sessions/{sid}/project-path", json={"path": str(project), "title": sid}, timeout=10)
    assert r.status_code == 200, r.text
    return sid


# ---------------------------------------------------------------------------------------------------------------------
# Fixtures: UN servidor para todo el modulo
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def proveedor():
    return FakeLLM()


@pytest.fixture(scope="module")
def base(proveedor):
    from app.llm import loop as loop_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture
def project(tmp_path):
    return tmp_path / "proyecto"


def start_active(base, sid, proveedor, tag, slow) -> Turn:
    a = Turn(base, sid, tag, slow=slow)
    assert wait_until(lambda: tag in proveedor.tags(tag), 15), "el turno activo nunca llego al modelo"
    return a


def enqueue(base, sid, tag, slow=None) -> Turn:
    t = Turn(base, sid, tag, slow=slow)
    assert t.wait_type("queued", 10), f"{tag} no recibio 'queued'"
    return t


# ---------------------------------------------------------------------------------------------------------------------
# (a) borrar un mensaje que sigue en la cola: como hoy (sale y no se manda); Detener arranca el siguiente no borrado
# ---------------------------------------------------------------------------------------------------------------------
def test_a_deleted_queued_message_is_never_sent_and_the_next_one_runs(base, proveedor, project):
    sid = new_session(base, project)
    start_active(base, sid, proveedor, "A1", slow=2.5)
    b = enqueue(base, sid, "B1")
    enqueue(base, sid, "C1")

    d = delete(base, sid, b.mid)

    assert wait_idle(base, sid, 20)
    time.sleep(1.0)  # margen para ver si B arranca tarde
    seen = proveedor.tags()
    assert d["status"] == 200 and d["body"]["case"] == "queued_turn_removed"
    assert "B1" not in seen, "REGLA ROTA: el mensaje borrado igual llego al modelo"
    assert "C1" in seen, "el siguiente NO borrado se procesa"


def test_deleting_the_first_queued_and_stopping_starts_the_next_not_deleted(base, proveedor, project):
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A2", slow=5)
    b = enqueue(base, sid, "B2")
    enqueue(base, sid, "C2")

    delete(base, sid, b.mid)
    time.sleep(0.3)
    stop(base, sid)

    assert wait_idle(base, sid, 20)
    time.sleep(1.0)
    seen = proveedor.tags()
    assert "stopped" in a.types()
    assert "B2" not in seen, "REGLA ROTA: el borrado se mando"
    assert "C2" in seen, "Detener como hoy: arranca el siguiente NO borrado"


def test_deleting_a_middle_queued_and_stopping_keeps_the_order(base, proveedor, project):
    sid = new_session(base, project)
    start_active(base, sid, proveedor, "A3", slow=5)
    enqueue(base, sid, "B3")
    c = enqueue(base, sid, "C3")
    enqueue(base, sid, "D3")

    delete(base, sid, c.mid)
    time.sleep(0.3)
    stop(base, sid)

    assert wait_idle(base, sid, 20)
    time.sleep(1.0)
    assert [t for t in proveedor.tags() if t.endswith("3")] == ["A3", "B3", "D3"]


def test_deleting_the_only_queued_and_stopping_leaves_the_session_idle(base, proveedor, project):
    sid = new_session(base, project)
    start_active(base, sid, proveedor, "A4", slow=5)
    b = enqueue(base, sid, "B4")

    delete(base, sid, b.mid)
    time.sleep(0.3)
    stop(base, sid)

    assert wait_idle(base, sid, 10)
    time.sleep(1.5)
    st = status(base, sid)
    assert "B4" not in proveedor.tags()
    assert st["busy"] is False and st["active_turn_id"] is None


# ---------------------------------------------------------------------------------------------------------------------
# (c) al borrar un mensaje en cola se cierra SU conexion (evento terminal para ese turno), sin esperar al turno activo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_connection_of_a_deleted_queued_message_closes_right_away(base, proveedor, project):
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A9", slow=4)
    b = enqueue(base, sid, "B9")

    d = delete(base, sid, b.mid)
    b.thread.join(2.0)  # el turno activo todavia tiene ~3 s por delante

    assert d["status"] == 200 and d["body"]["case"] == "queued_turn_removed"
    assert not b.is_open() and b.error is None, f"el stream del mensaje borrado sigue abierto (zombi): {b.summary()}"
    assert status(base, sid)["busy"] is True, "la prueba necesita que el turno activo siga corriendo"
    assert a.is_open(), "borrar un mensaje en cola NO corta la conexion del turno que sigue respondiendo"
    assert not ({"text-delta", "done", "stopped"} & set(b.types())), "el borrado no tiene contenido propio"
    assert wait_idle(base, sid, 15)
    a.thread.join(3.0)
    assert a.types()[-1] == "done" and not a.is_open()


# ---------------------------------------------------------------------------------------------------------------------
# (d) 'dequeued' cuando un mensaje sale de la cola y arranca, con la cola que queda (para renumerar «En cola · posición N»)
# ---------------------------------------------------------------------------------------------------------------------
def test_dequeued_is_announced_when_a_queued_message_starts(base, proveedor, project):
    sid = new_session(base, project)
    start_active(base, sid, proveedor, "A10", slow=1.5)
    b = enqueue(base, sid, "B10", slow=1)
    c = enqueue(base, sid, "C10")

    assert wait_idle(base, sid, 20)
    b.thread.join(3.0)
    c.thread.join(3.0)

    b_started = [d for d in b.of_type("dequeued") if d.get("message_id") == b.mid]
    assert b_started, f"B arranco y nunca se aviso 'dequeued': {b.types()}"
    assert b_started[0].get("queue") == [c.mid], "el aviso trae la cola que queda: C pasa a la posicion 1"
    first_text = b.types().index("text-delta")
    assert b.types().index("dequeued") < first_text, "el aviso llega ANTES de la respuesta"
    # C (que sigue esperando) tambien se entera de que B salio, y despues de su propio arranque (cola vacia).
    c_seen = [d for d in c.of_type("dequeued")]
    assert [d.get("message_id") for d in c_seen] == [b.mid, c.mid]
    assert c_seen[-1].get("queue") == []
    assert b.types()[-1] == "done" and c.types()[-1] == "done"


# ---------------------------------------------------------------------------------------------------------------------
# (b) borrar un mensaje cuyo turno YA arranco: Detener ese turno y descartarlo, con el historial consistente
# ---------------------------------------------------------------------------------------------------------------------
def _assert_only_a_answered(base, sid, tag_a: str, tag_b: str) -> None:
    """Memoria y SQLite: el turno de A completo (pregunta y respuesta) y NADA del borrado B."""
    mem = memory(sid)
    shown = sqlite_view(base, sid)
    reloaded = sqlite_reload(sid)
    for name, rows in (("memoria", mem), ("SQLite (chat)", shown), ("SQLite (lo que veria el modelo)", reloaded)):
        assert not mentions(rows, tag_b), f"{name}: quedo algo del mensaje borrado {tag_b}: {rows}"
        assert [r[0] for r in rows] == ["user", "assistant"], f"{name}: el turno anterior quedo roto: {rows}"
        assert f"[{tag_a}]" in rows[0][1] and f"{tag_a}·" in rows[1][1], f"{name}: el turno anterior no esta entero: {rows}"


def test_deleting_a_message_whose_turn_already_started_stops_and_discards_it(base, proveedor, project):
    """Caso real de UX: el usuario toca 🗑 sobre un mensaje que la UI todavia muestra «En cola · posición 1»; mientras el
    modal «Eliminar mensaje» esta abierto, el turno activo termina y el backend ARRANCA ese mensaje. El DELETE llega
    cuando ya no esta en la cola."""
    sid = new_session(base, project)
    start_active(base, sid, proveedor, "A5", slow=1)
    b = enqueue(base, sid, "B5", slow=3)
    assert wait_until(lambda: "B5" in proveedor.tags("B5"), 10), "B nunca arranco"
    assert b.wait_type("text-delta", 5), "B ya esta respondiendo (texto en streaming)"

    d = delete(base, sid, b.mid)

    assert wait_idle(base, sid, 10)
    b.thread.join(3.0)
    assert d["status"] == 200, d
    assert "done" not in b.types(), f"REGLA ROTA: el mensaje borrado se respondio entero: {b.types()}"
    assert b.types()[-1] == "stopped" and not b.is_open()
    _assert_only_a_answered(base, sid, "A5", "B5")


def test_deleting_right_after_stopping_never_answers_the_deleted_message(base, proveedor, project):
    """Detener y enseguida (o ~1 s despues: lo que tarda el modal «Eliminar mensaje») borrar el que estaba en cola. Con
    Detener el siguiente arranca en ~0,3 s: el borrado tardio lo encuentra corriendo y tiene que frenarlo y descartarlo."""
    for label, wait_s in (("inm", 0.0), ("mod", 1.0)):
        sid = new_session(base, project / label)
        tag_a, tag_b = f"A7{label}", f"B7{label}"
        a = start_active(base, sid, proveedor, tag_a, slow=5)
        assert a.wait_type("text-delta", 5)
        b = enqueue(base, sid, tag_b, slow=2)

        s = stop(base, sid)
        time.sleep(wait_s)
        d = delete(base, sid, b.mid)

        assert wait_idle(base, sid, 20)
        b.thread.join(3.0)
        assert s["body"] == {"stopped": True} and d["status"] == 200, (s, d)
        assert "done" not in b.types(), f"{label}: REGLA ROTA, el borrado se respondio entero: {b.types()}"
        assert not b.is_open(), f"{label}: la conexion del borrado quedo abierta"
        mem = memory(sid)
        assert not mentions(mem, tag_b) and not mentions(sqlite_reload(sid), tag_b), f"{label}: quedo algo de B: {mem}"
        assert d["body"]["case"] in ("queued_turn_removed", "active_turn_discarded"), d
        if d["body"]["case"] == "queued_turn_removed":
            # El borrado le gano al desencolado (lo comun con wait_s=0): seguia en la cola, nunca llega al modelo.
            assert tag_b not in proveedor.tags(tag_b), f"{label}: salio de la cola borrado y aun asi llego al modelo"


@pytest.mark.timeout(180)
def test_deleting_in_the_dequeue_window_never_answers_it_nor_breaks_history(base, proveedor, project):
    """El DELETE sale en el instante exacto del ultimo chunk de A (+ desfase): cae antes, durante o despues del
    desencolado. En TODOS los casos: sin 500, B nunca se responde entero, nada de B queda en memoria/SQLite, A queda
    entero y el modelo nunca vuelve a contestar A (pasaba cuando el turno de B corria sin su mensaje en el historial)."""
    rows = []
    for i, offset in enumerate([0.0, 0.005, 0.01, 0.05, 0.2]):
        sid = new_session(base, project / f"d2-{i}")
        holder: dict[str, Any] = {}
        tag_a, tag_b = f"A6x{i}", f"B6x{i}"

        def fire(sid=sid, holder=holder, offset=offset):
            def go():
                # Con la PC cargada, encolar B puede tardar: el borrado sale igual (sin B no hay nada que borrar).
                wait_until(lambda: "b" in holder, 5)
                time.sleep(offset)
                holder["delete"] = delete(base, sid, holder["b"].mid)

            threading.Thread(target=go, daemon=True).start()

        proveedor.on_last[tag_a] = fire
        # 1,2 s: margen para que B quede EN COLA antes de que A termine (si no, B arranca directo y no hay ventana que probar).
        start_active(base, sid, proveedor, tag_a, slow=1.2)
        holder["b"] = enqueue(base, sid, tag_b, slow=0.8)
        assert wait_until(lambda h=holder: "delete" in h, 10)
        assert wait_idle(base, sid, 15)
        holder["b"].thread.join(3.0)
        rows.append(
            {
                "offset": offset,
                "delete": holder["delete"],
                "b_events": holder["b"].types(),
                "b_open": holder["b"].is_open(),
                "model_calls": [t for t in proveedor.tags() if t in (tag_a, tag_b)],
                "memory": memory(sid),
                "reloaded": sqlite_reload(sid),
                "tag_a": tag_a,
                "tag_b": tag_b,
            }
        )
    for r in rows:
        assert r["delete"]["status"] == 200, r
        assert "done" not in r["b_events"], f"desfase {r['offset']}: el borrado se respondio entero: {r}"
        assert not r["b_open"], f"desfase {r['offset']}: la conexion del borrado quedo abierta: {r}"
        assert r["model_calls"].count(r["tag_a"]) == 1, f"desfase {r['offset']}: el modelo contesto A dos veces: {r}"
        for rows_name in ("memory", "reloaded"):
            hist = r[rows_name]
            assert not mentions(hist, r["tag_b"]), f"desfase {r['offset']}: quedo algo de B en {rows_name}: {r}"
            assert [h[0] for h in hist] == ["user", "assistant"], f"desfase {r['offset']}: historial roto: {r}"


# ---------------------------------------------------------------------------------------------------------------------
# (e) borrar y desencolar sin carreras
# ---------------------------------------------------------------------------------------------------------------------
def test_remove_and_dequeue_are_atomic_between_threads():
    """El DELETE puede correr en otro hilo que el desencolado (el `finally` de `_launch_turn`, en el event loop). Con una
    demora inyectada entre el "¿esta en la cola?" y el "sacalo" (un cambio de hilo justo ahi), el par tiene que ser
    atomico: o gana el borrado (y nadie lo desencola) o gana el desencolado (y el borrado dice que ya no estaba). Nunca
    una excepcion (antes: ValueError -> 500, y el turno arrancaba igual)."""
    from app.session.events import SessionState, UserTurn

    class SlowList(list):
        def __contains__(self, item):
            r = super().__contains__(item)
            time.sleep(0.05)  # el otro hilo corre aca
            return r

    s = SessionState(session_id="x")
    s.enqueue_turn(UserTurn(id="B", text="b"))
    s.queue_order = SlowList(s.queue_order)
    res: dict[str, Any] = {}

    def borrar():
        try:
            res["remove"] = s.remove_queued_turn("B")
        except Exception as exc:  # noqa: BLE001
            res["remove"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=borrar)
    t.start()
    time.sleep(0.01)
    dq = s.dequeue_next_turn()  # lo que hace el `finally` de _launch_turn al terminar el turno activo
    t.join()

    assert res["remove"] in (True, False), f"borrar en la carrera no puede explotar: {res['remove']}"
    assert (res["remove"] is True) != (dq is not None), f"uno solo gana: borrado={res['remove']} desencolado={dq}"
    if dq is not None:
        assert dq.id == "B"
    assert list(s.queue_order) == []


# ---------------------------------------------------------------------------------------------------------------------
# (f) Detener apunta al turno que el usuario ve
# ---------------------------------------------------------------------------------------------------------------------
def test_a_stop_aimed_at_a_finished_turn_does_not_stop_the_next_queued(base, proveedor, project):
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A11", slow=0.5)
    b = enqueue(base, sid, "B11", slow=2)
    assert wait_until(lambda: "B11" in proveedor.tags("B11"), 10), "B nunca arranco"
    a.thread.join(3.0)
    assert a.types()[-1] == "done"

    s = stop(base, sid, turn_id=a.mid)  # el usuario apreto Detener mirando A, que ya habia terminado

    assert s["status"] == 200 and s["body"].get("stopped") is False and s["body"].get("reason"), s
    assert s["body"]["reason"] != "no_active_turn", "hay un turno corriendo: es OTRO, no 'ninguno'"
    assert wait_idle(base, sid, 15)
    b.thread.join(3.0)
    assert b.types()[-1] == "done", f"un Detener dirigido a A frenó a B: {b.types()}"


def test_a_stop_aimed_at_the_running_turn_stops_it_as_today(base, proveedor, project):
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A12", slow=5)
    b = enqueue(base, sid, "B12")

    s = stop(base, sid, turn_id=a.mid)

    assert s["body"] == {"stopped": True}
    assert wait_idle(base, sid, 15)
    b.thread.join(3.0)
    assert a.types()[-1] == "stopped"
    assert b.types()[-1] == "done", "Detener como hoy: el siguiente de la cola arranca"


@pytest.mark.timeout(120)
def test_a_stop_in_the_last_moments_of_a_turn_never_stops_the_next_queued(base, proveedor, project):
    """El Detener (con el id del turno que el usuario ve) llega justo cuando A termina: frena a A o llega tarde, pero
    nunca frena a B, que acaba de arrancar (antes, en los ultimos 10-100 ms frenaba al SIGUIENTE mensaje de la cola)."""
    rows = []
    for i, offset in enumerate([0.0, 0.01, 0.02, 0.05]):
        sid = new_session(base, project / f"d5-{i}")
        holder: dict[str, Any] = {}
        tag_a, tag_b, tag_c = f"A8x{i}", f"B8x{i}", f"C8x{i}"

        def fire(sid=sid, holder=holder, offset=offset):
            def go():
                wait_until(lambda: "a" in holder, 5)
                time.sleep(offset)
                holder["stop"] = stop(base, sid, turn_id=holder["a"].mid)

            threading.Thread(target=go, daemon=True).start()

        proveedor.on_last[tag_a] = fire
        holder["a"] = start_active(base, sid, proveedor, tag_a, slow=1.2)  # margen para encolar B y C antes de que A termine
        b = enqueue(base, sid, tag_b, slow=1)
        c = enqueue(base, sid, tag_c)
        assert wait_until(lambda h=holder: "stop" in h, 10)
        assert wait_idle(base, sid, 15)
        b.thread.join(3.0)
        c.thread.join(3.0)
        rows.append({"offset": offset, "stop": holder["stop"], "a": holder["a"].types()[-1:], "b": b.types()[-1:], "c": c.types()[-1:]})
    for r in rows:
        assert r["stop"]["status"] == 200, r
        assert r["a"] in (["done"], ["stopped"]), r
        assert r["b"] == ["done"] and r["c"] == ["done"], f"desfase {r['offset']}: el Detener de A frenó a otro: {r}"


# ---------------------------------------------------------------------------------------------------------------------
# Historial: borrar un mensaje VIEJO mientras otro turno corre no le corta el historial al turno en curso
# ---------------------------------------------------------------------------------------------------------------------
def test_deleting_an_older_message_while_another_turn_runs_keeps_the_running_turn(base, proveedor, project):
    """Mismo origen que (b): el borrado de B llega cuando B ya termino y arranco C. Antes se truncaba `session.messages`
    desde B con el turno de C CORRIENDO: C perdia su propio mensaje y seguia respondiendo sobre un historial roto."""
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A13", slow=0.3)
    assert wait_idle(base, sid, 10)
    a.thread.join(3.0)
    c = start_active(base, sid, proveedor, "C13", slow=2)
    assert c.wait_type("text-delta", 5)

    d = delete(base, sid, a.mid)

    assert d["status"] == 200, d
    assert wait_idle(base, sid, 15)
    c.thread.join(3.0)
    assert c.types()[-1] == "done"
    for name, rows in (("memoria", memory(sid)), ("SQLite", sqlite_reload(sid))):
        assert not mentions(rows, "A13"), f"{name}: el mensaje borrado sigue: {rows}"
        assert [r[0] for r in rows] == ["user", "assistant"] and "[C13]" in rows[0][1] and "C13·" in rows[1][1], (
            f"{name}: el turno que estaba corriendo quedo roto: {rows}"
        )


# ---------------------------------------------------------------------------------------------------------------------
# Revision de la rama (3 revisores, hallazgos confirmados): Detener con el loop, borrar durante la compactacion y editar
# historial viejo con otro turno corriendo. Los primeros corren EN PROCESO (sin servidor HTTP): el turno corre en el event
# loop de `asyncio.run` y se miran memoria y respuestas de los endpoints directamente.
# ---------------------------------------------------------------------------------------------------------------------
def _contents(session) -> list[str]:
    return [_text_of(m.get("content")) for m in session.messages if m.get("role") != "system"]


def test_a_stop_aimed_at_the_loop_turn_that_just_ended_stops_the_loop(monkeypatch):
    """REQ-007b (spec modo-plan-loop): un Detener frena el turno y apaga el loop. Regresion encontrada en la revision de REQ-014 f: el
    turno del loop que el usuario miraba ya habia terminado y el loop ya habia arrancado el siguiente (`loop-<uuid>`, desde el `finally`
    de `_launch_turn`) sin ningun evento todavia (compactando o pidiendo la lista de modelos). El Detener con el id del turno terminado
    contestaba `other_turn_active` y el loop seguia hasta un segundo Detener."""
    from app import main
    from app.session.events import UserTurn

    started: list[str] = []

    async def fake_run_turn(session, text, session_id, **kw):
        mid = kw.get("user_message_id")
        started.append(mid)
        if mid == "loop-N":
            yield {"type": "done", "data": {}}
            return
        for _ in range(100):  # el turno siguiente del loop corre hasta que lo frenen (o 5 s)
            if session.stop_requested:
                yield {"type": "stopped", "data": {"reason": "user_requested"}}
                return
            await asyncio.sleep(0.05)
        yield {"type": "done", "data": {}}

    monkeypatch.setattr(main, "run_turn", fake_run_turn)

    async def scenario():
        sid = f"loop-stop-{uuid.uuid4().hex[:8]}"
        session = main.get_session_store().get_or_create(sid)
        session.todos = [{"content": "paso", "status": "pending"}]
        session.loop_active = True
        inbox = session.subscribe()
        main._launch_turn(session, sid, UserTurn(id="loop-N", text="seguir"))
        for _ in range(200):  # loop-N termina y el loop arma el siguiente en el mismo `finally`
            if session.active_turn_id not in (None, "loop-N"):
                break
            await asyncio.sleep(0.01)
        next_id = session.active_turn_id
        # El usuario aprieta Detener mirando todavia loop-N: el primer evento del turno siguiente no llego.
        resp = await main.stop_turn_endpoint(sid, main.StopTurnRequest(turn_id="loop-N"))
        await asyncio.wait_for(session.current_task, 10)
        await asyncio.sleep(0.2)  # si el loop siguiera, aca ya habria arrancado otro turno
        events = []
        while not inbox.empty():
            events.append(inbox.get_nowait())
        return resp, next_id, session, events

    resp, next_id, session, events = asyncio.run(scenario())
    assert next_id and next_id.startswith("loop-") and next_id != "loop-N", "la prueba necesita que el loop ya haya armado el siguiente"
    assert resp.get("stopped") is True, f"Detener no freno el turno siguiente del loop: {resp}"
    assert (session.loop_active, session.loop_auto, session.loop_turns_count) == (False, False, 0), "el loop sigue prendido"
    assert any(e["type"] == "stopped" and e["_turn_id"] == next_id for e in events), "el turno siguiente del loop no termino detenido"
    assert started == ["loop-N", next_id], f"el loop armo otro turno despues del Detener: {started}"
    assert not session.is_busy


@pytest.mark.parametrize("which", ["reciente", "viejo"])
def test_deleting_an_older_message_while_the_running_turn_compacts_never_brings_it_back(monkeypatch, which):
    """REQ-014 c (spec fix-detener-y-progreso-visible, revision): borrar historial viejo con otro turno corriendo justo cuando ese turno
    esta compactando (esperando el resumen del modelo: 18 s medidos). La compactacion armaba la lista nueva con lo que habia ANTES de
    esperar y despues la reemplazaba: el mensaje borrado volvia a la memoria (si era de los turnos recientes) o quedaba adentro del resumen
    (si era de los viejos), y el modelo lo seguia viendo aunque SQLite ya no lo tuviera."""
    from app import main
    from app.llm import loop as loop_module
    from app.session import compaction as compaction_module
    from app.session.events import UserTurn

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SlowStream("ok", 0.1))))
    monkeypatch.setattr(loop_module, "get_settings", lambda: SimpleNamespace(llm_model="test-model"))
    monkeypatch.setattr(loop_module, "get_client", lambda *a, **k: client)
    monkeypatch.setitem(main._MODELS_CACHE, "by_id", {})  # sin pedirle el catalogo a nadie (se restaura al terminar)
    monkeypatch.setattr(main, "resolve_context_window", lambda model: 4000)

    async def scenario():
        in_summary, release = asyncio.Event(), asyncio.Event()

        async def slow_summarize(transcript, model):
            in_summary.set()
            await release.wait()
            return "resumen de lo viejo"

        monkeypatch.setattr(compaction_module, "_summarize", slow_summarize)
        sid = f"compacta-{uuid.uuid4().hex[:8]}"
        session = main.get_session_store().get_or_create(sid)
        big = "x" * 16_000  # ~4000 tokens cada uno: el historial pasa el 85 % de 4000 y el turno viejo no entra en lo reciente
        session.messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "viejo " + big},
            {"role": "assistant", "content": "resp vieja " + big},
            {"role": "user", "content": "BORRAME"},
            {"role": "assistant", "content": "respuesta a borrame"},
        ]
        for tid, text, idx in (("t0", "viejo " + big, 1), ("t1", "BORRAME", 3)):
            t = UserTurn(id=tid, text=text)
            t.messages_start_index, t.status = idx, "done"
            session.turns[tid] = t

        main._launch_turn(session, sid, UserTurn(id="t3", text="tercero"))
        await asyncio.wait_for(in_summary.wait(), 10)
        # El turno t3 esta compactando (esperando el resumen). El usuario borra un mensaje viejo.
        resp = await main.delete_message_endpoint(sid, "t1" if which == "reciente" else "t0")
        release.set()
        await asyncio.wait_for(session.current_task, 10)
        return resp, session

    resp, session = asyncio.run(scenario())
    final = _contents(session)
    assert resp["case"] == "history_truncated", resp
    assert not any("BORRAME" in c or "borrame" in c for c in final), f"el mensaje borrado volvio a la memoria: {final}"
    if which == "viejo":
        assert not any(c.startswith(("viejo", "resp vieja")) for c in final), f"el turno viejo borrado volvio: {final}"
        assert not any("[Resumen automatico" in c for c in final), f"el resumen se armo con lo que el usuario borro: {final}"
    else:
        assert "[Resumen automatico" in final[0], f"lo que no se borro se sigue resumiendo: {final}"
    assert final[-2:] == ["tercero", "ok·"], f"el turno en curso quedo roto: {final}"


def test_editing_an_older_message_while_another_turn_runs_keeps_the_running_turn_in_memory(monkeypatch):
    """Preexistente, mismo defecto que REQ-014 c arreglo para borrar (spec fix-detener-y-progreso-visible, revision): editar un mensaje
    de historial cerrado con OTRO turno corriendo truncaba desde ese mensaje hasta el final, tambien el mensaje y la salida del turno en
    curso, que seguia respondiendo sobre un historial sin su pregunta."""
    from app import main
    from app.session.events import UserTurn

    async def fake_run_turn(session, text, session_id, **kw):
        session.messages.append({"role": "user", "content": text})
        for _ in range(30):
            if session.stop_requested:
                yield {"type": "stopped", "data": {}}
                return
            await asyncio.sleep(0.05)
        session.messages.append({"role": "assistant", "content": "resp " + text})
        yield {"type": "done", "data": {}}

    monkeypatch.setattr(main, "run_turn", fake_run_turn)

    async def scenario():
        sid = f"edita-{uuid.uuid4().hex[:8]}"
        session = main.get_session_store().get_or_create(sid)
        session.messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "VIEJO"},
            {"role": "assistant", "content": "resp VIEJO"},
        ]
        t1 = UserTurn(id="t1", text="VIEJO")
        t1.messages_start_index, t1.status = 1, "done"
        session.turns["t1"] = t1
        main._launch_turn(session, sid, UserTurn(id="t2", text="EN CURSO"))
        await asyncio.sleep(0.2)  # t2 ya agrego su mensaje y esta "respondiendo"
        resp = await main.edit_message_endpoint(sid, "t1", main.EditMessageRequest(text="VIEJO editado"))
        during = _contents(session)
        for _ in range(200):  # t2 termina y arranca el editado (encolado)
            if not session.is_busy and not session.queue_order:
                break
            await asyncio.sleep(0.05)
        return resp, during, _contents(session)

    resp, during, final = asyncio.run(scenario())
    assert resp["case"] == "history_truncated_and_relaunched", resp
    assert during == ["EN CURSO"], f"el turno en curso perdio su pregunta (o quedo lo editado): {during}"
    assert final == ["EN CURSO", "resp EN CURSO", "VIEJO editado", "resp VIEJO editado"], final


def test_editing_an_older_message_while_another_turn_runs_keeps_the_running_turn(base, proveedor, project):
    """Lo mismo con el backend real y SQLite (mismo armado que el borrado de arriba): editar A mientras C responde. C termina entero y
    el editado se responde despues; memoria y SQLite quedan iguales y sin nada del texto viejo de A."""
    sid = new_session(base, project)
    a = start_active(base, sid, proveedor, "A14", slow=0.3)
    assert wait_idle(base, sid, 10)
    a.thread.join(3.0)
    c = start_active(base, sid, proveedor, "C14", slow=2)
    assert c.wait_type("text-delta", 5)

    r = _http().patch(f"{base}/api/chat/{sid}/message/{a.mid}", json={"text": "mensaje [E14] editado"}, timeout=10)

    assert r.status_code == 200 and r.json()["case"] == "history_truncated_and_relaunched", r.text
    during = memory(sid)
    assert mentions(during, "C14") and not mentions(during, "A14"), f"el turno en curso perdio su pregunta: {during}"
    assert wait_until(lambda: "E14" in proveedor.tags("E14"), 15), "el mensaje editado nunca se respondio"
    assert wait_idle(base, sid, 15)
    c.thread.join(3.0)
    assert c.types()[-1] == "done"
    for name, rows in (("memoria", memory(sid)), ("SQLite", sqlite_reload(sid))):
        assert not mentions(rows, "A14"), f"{name}: el mensaje editado sigue con el texto viejo: {rows}"
        assert [r[0] for r in rows] == ["user", "assistant", "user", "assistant"], f"{name}: historial roto: {rows}"
        assert "[C14]" in rows[0][1] and "C14·" in rows[1][1], f"{name}: el turno que estaba corriendo quedo roto: {rows}"
        assert "[E14]" in rows[2][1] and "E14·" in rows[3][1], f"{name}: el editado no quedo despues: {rows}"
