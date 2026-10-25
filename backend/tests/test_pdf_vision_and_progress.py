"""spec adjuntar-archivos-chat REQ-006 (PDF escaneado -> vision) y REQ-008 b
(% real de parseo emitido desde el hilo de la tool `read`)."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
from types import SimpleNamespace

import pytest

from app import attachment_readers as ar
from app import pdf_vision as pv
from app.tools import base as tool_base
from app.tools import filesystem as fs
from test_read_attachment_formats import _pdf_with_text

VISION = {
    "claude-sonnet": {"supports_image_input": True, "aliases": ["sonnet"]},
    "gpt-5-mini": {"supports_image_input": True},
    "deepseek": {"supports_image_input": False},
}


# ---------------------------------------------------------------------------
# Catalogo / soporte de vision
# ---------------------------------------------------------------------------
def test_supports_images_by_id_and_by_alias_and_unknown():
    assert pv.supports_images("claude-sonnet", VISION)
    assert pv.supports_images("sonnet", VISION)  # alias
    assert not pv.supports_images("deepseek", VISION)
    assert not pv.supports_images("no-existe", VISION)
    assert not pv.supports_images(None, VISION)
    assert not pv.supports_images("x", {})


def test_vision_models_list_names_only_the_capable_ones():
    assert pv.vision_models_from_catalog(VISION) == ["claude-sonnet", "gpt-5-mini"]


def test_blocked_message_names_models_that_do_support_images():
    msg = pv.blocked_message(["claude-sonnet", "gpt-5-mini"])
    assert "no soporta imágenes" in msg and "claude-sonnet" in msg and "gpt-5-mini" in msg


def test_blocked_message_without_a_roster_still_helps():
    assert "visión" in pv.blocked_message([])


def test_too_many_pages_returns_the_token_estimate_and_asks_to_narrow():
    msg = pv.too_many_message(list(range(1, 16)))
    assert "15 páginas" in msg and "pages" in msg and "k tokens" in msg


# ---------------------------------------------------------------------------
# Render real (pypdfium2): lado mayor 1568 px, JPEG
# ---------------------------------------------------------------------------
def test_render_page_jpeg_caps_the_long_side_at_1568(tmp_path):
    pytest.importorskip("pypdfium2")
    from PIL import Image

    f = tmp_path / "a.pdf"
    f.write_bytes(_pdf_with_text(["Hoja de prueba con texto"]))

    jpeg = pv.render_page_jpeg(f, 1)

    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG" and max(img.size) <= pv.MAX_SIDE_PX
    assert max(img.size) >= pv.MAX_SIDE_PX - 2  # se ajusta al tope, no se queda chico


# ---------------------------------------------------------------------------
# Transcriptor
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self, replies=None, fail_on=()):
        self.calls: list[tuple[str, list]] = []
        self.replies = replies or {}
        self.fail_on = set(fail_on)

    def render(self, path, n):
        return b"\xff\xd8jpeg-" + str(n).encode()

    def complete(self, model, messages):
        self.calls.append((model, messages))
        n = int(base64.b64decode(messages[0]["content"][1]["image_url"]["url"].split(",")[1]).decode("latin-1").rsplit("-", 1)[1])
        if n in self.fail_on:
            raise RuntimeError("falla del modelo")
        return self.replies.get(n, f"texto de la página {n}")


def _tr(rec, model="claude-sonnet", catalog=VISION, progress=None):
    return pv.make_transcriber(model=model, catalog=catalog, complete=rec.complete, render=rec.render, on_progress=progress)


def test_transcribes_with_the_active_model_and_the_no_invent_instruction(tmp_path):
    rec = Recorder()
    out, notice = _tr(rec)(tmp_path / "x.pdf", [2, 3])

    assert out == {2: "texto de la página 2", 3: "texto de la página 3"}
    assert notice == pv.NOTICE and "puede tener errores" in notice
    assert [c[0] for c in rec.calls] == ["claude-sonnet", "claude-sonnet"]
    content = rec.calls[0][1][0]["content"]
    assert "no inventes" in content[0]["text"].lower()
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_model_without_vision_is_blocked_and_nothing_is_invented(tmp_path):
    rec = Recorder()
    out, notice = _tr(rec, model="deepseek")(tmp_path / "x.pdf", [1])

    assert out == {} and rec.calls == []  # ni siquiera llama al modelo
    assert "no soporta imágenes" in notice and "claude-sonnet" in notice


def test_unknown_active_model_is_blocked_too(tmp_path):
    out, notice = _tr(Recorder(), model=None)(tmp_path / "x.pdf", [1])
    assert out == {} and "no soporta imágenes" in notice


def test_more_than_ten_scanned_pages_is_refused_without_calling_the_model(tmp_path):
    rec = Recorder()
    out, notice = _tr(rec)(tmp_path / "x.pdf", list(range(1, 12)))
    assert out == {} and rec.calls == [] and "11 páginas" in notice


def test_exactly_ten_pages_are_allowed(tmp_path):
    rec = Recorder()
    out, _ = _tr(rec)(tmp_path / "x.pdf", list(range(1, 11)))
    assert len(out) == 10


def test_a_failing_page_is_reported_and_the_others_survive(tmp_path):
    out, notice = _tr(Recorder(fail_on={2}))(tmp_path / "x.pdf", [1, 2, 3])
    assert set(out) == {1, 3} and "No pude transcribir las páginas 2" in notice and pv.NOTICE in notice


def test_an_empty_reply_counts_as_a_failure_not_as_text(tmp_path):
    out, notice = _tr(Recorder(replies={1: "   "}))(tmp_path / "x.pdf", [1])
    assert out == {} and "No pude transcribir las páginas 1" in notice


def test_transcriber_reports_progress(tmp_path):
    seen: list[int] = []
    _tr(Recorder(), progress=seen.append)(tmp_path / "x.pdf", [1, 2, 4])
    assert seen == [33, 66, 100]


# ---------------------------------------------------------------------------
# Integracion con read_pdf
# ---------------------------------------------------------------------------
def test_read_pdf_merges_transcribed_pages_with_digital_ones(tmp_path):
    f = tmp_path / "mixto.pdf"
    f.write_bytes(_pdf_with_text(["", "Pagina digital con bastante texto real"]))
    rec = Recorder(replies={1: "TRANSCRIPCION DE LA PAGINA ESCANEADA"})

    out = ar.read_pdf(f, transcribe_pages=_tr(rec))

    assert "TRANSCRIPCION DE LA PAGINA ESCANEADA" in out and "Pagina digital con bastante texto real" in out
    assert pv.NOTICE in out and "no tienen texto extraíble" not in out


def test_read_pdf_scanned_without_vision_gives_the_block_notice_and_no_text(tmp_path):
    f = tmp_path / "escaneado.pdf"
    f.write_bytes(_pdf_with_text(["", ""]))

    out = ar.read_pdf(f, transcribe_pages=_tr(Recorder(), model="deepseek"))

    assert "no soporta imágenes" in out
    assert "--- Página 1 ---\n--- Página 2 ---" in out.replace("\r", "")  # cero texto inventado


# ---------------------------------------------------------------------------
# read (tool): modelo activo de la sesion
# ---------------------------------------------------------------------------
def test_read_tool_uses_the_session_active_model(tmp_path, monkeypatch):
    f = tmp_path / "esc.pdf"
    f.write_bytes(_pdf_with_text([""]))
    calls: list[str] = []
    monkeypatch.setattr(pv, "complete_with_proveedor", lambda model, msgs: calls.append(model) or "TEXTO")
    monkeypatch.setattr(pv, "render_page_jpeg", lambda p, n: b"jpeg")
    from app import main

    monkeypatch.setitem(main._MODELS_CACHE, "by_id", VISION)
    session = SimpleNamespace(active_model="claude-sonnet", session_id="")

    out = fs._read_execute({"path": str(f)}, session)

    assert calls == ["claude-sonnet"] and "TEXTO" in out


def test_read_tool_blocks_when_the_active_model_has_no_vision(tmp_path, monkeypatch):
    f = tmp_path / "esc.pdf"
    f.write_bytes(_pdf_with_text([""]))
    from app import main

    monkeypatch.setitem(main._MODELS_CACHE, "by_id", VISION)
    out = fs._read_execute({"path": str(f)}, SimpleNamespace(active_model="deepseek", session_id=""))
    assert "no soporta imágenes" in out


def test_read_tool_without_a_session_does_not_crash_on_scanned_pdf(tmp_path):
    f = tmp_path / "esc.pdf"
    f.write_bytes(_pdf_with_text([""]))
    out = fs._read_execute({"path": str(f)})  # sin sesion (tests / uso directo)
    assert "no soporta imágenes" in out or "no tienen texto" in out


# ---------------------------------------------------------------------------
# REQ-008 b: push_event desde el hilo de la tool
# ---------------------------------------------------------------------------
class FakeSession:
    def __init__(self):
        self.events: list[tuple[str, dict, str]] = []

    def push_event(self, kind, data):
        # Tiene que correr en el hilo del event loop, NO en el de la tool.
        self.events.append((kind, data, threading.current_thread().name))


def test_emit_from_thread_delivers_on_the_event_loop_thread():
    session = FakeSession()

    async def scenario():
        tool_base._TURN_LOOP.set(asyncio.get_running_loop())
        loop_thread = threading.current_thread().name

        def worker():
            tool_base.emit_from_thread(session, "progress-estimate", {"pct": 40})

        await asyncio.to_thread(worker)
        await asyncio.sleep(0.05)
        return loop_thread

    loop_thread = asyncio.run(scenario())

    assert session.events == [("progress-estimate", {"pct": 40}, loop_thread)]


def test_emit_from_thread_is_a_noop_without_a_loop_or_session():
    tool_base._TURN_LOOP.set(None)
    tool_base.emit_from_thread(FakeSession(), "x", {})  # sin loop: no lanza
    tool_base.emit_from_thread(None, "x", {})  # sin sesion: no lanza


def test_dispatch_lets_read_report_page_progress_through_the_queue(tmp_path):
    f = tmp_path / "tres.pdf"
    f.write_bytes(_pdf_with_text(["Primera pagina con texto de sobra", "Segunda pagina con texto de sobra", "Tercera pagina con texto de sobra"]))
    session = FakeSession()
    registry = tool_base.ToolRegistry()
    registry.register(fs.read_tool)

    import json

    out = asyncio.run(registry.dispatch("read", json.dumps({"path": str(f)}), session))

    assert "--- Página 3 ---" in out
    pcts = [d["pct"] for k, d, _ in session.events if k == "progress-estimate"]
    assert pcts == [33, 66, 100] and all(0 <= p <= 100 for p in pcts)
    assert all(d["operation"] == "read" for k, d, _ in session.events)
