"""Unit de `record_decision` (spec decisions-md-automatico): append-only a
`decisions/decisions.md` de la carpeta de proyecto de LA SESION, con lock de
archivo (msvcrt, solo Windows)."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

import app.persistence.service as persistence
from app.tools import decisions as d

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="el lock usa msvcrt (solo Windows)")


class FakeSession:
    def __init__(self, sid="s1"):
        self.session_id = sid
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind, payload):
        self.events.append((kind, payload))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: tmp_path)
    return tmp_path


def _decisions_file(root):
    return root / persistence.DECISIONS_SUBDIR / d.DECISIONS_FILENAME


def test_requires_active_session():
    assert "no hay sesión activa" in d._record_decision_execute({"decision": "x", "motivo": "y"})
    assert "no hay sesión activa" in d._record_decision_execute(
        {"decision": "x", "motivo": "y"}, SimpleNamespace(session_id="")
    )


@pytest.mark.parametrize(
    ("args", "fragment"),
    [({"motivo": "m"}, "'decision'"), ({"decision": "  ", "motivo": "m"}, "'decision'"), ({"decision": "d"}, "'motivo'")],
)
def test_missing_arguments(project, args, fragment):
    assert fragment in d._record_decision_execute(args, FakeSession())
    assert not _decisions_file(project).exists()


def test_without_project_folder_asks_frontend_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: None)
    s = FakeSession()
    out = d._record_decision_execute({"decision": "x", "motivo": "y"}, s)
    assert "no tiene ninguna carpeta" in out
    assert s.events == [("project-folder-required", {"action": "record_decision"})]
    assert list(tmp_path.iterdir()) == []


def test_first_write_creates_header_and_entry(project):
    out = d._record_decision_execute(
        {"decision": "Vendorizar Chart.js", "motivo": "cero CDN", "alternativa_descartada": "usar CDN"},
        FakeSession("sesion-9"),
    )
    text = _decisions_file(project).read_text(encoding="utf-8")
    assert text.startswith("# decisions.md")
    assert "Vendorizar Chart.js" in text and "**Motivo:** cero CDN" in text
    assert "**Alternativa descartada:** usar CDN" in text and "**Sesión:** sesion-9" in text
    assert out.startswith("Decisión registrada en") and "Vendorizar Chart.js" in out


def test_alternative_line_is_optional(project):
    d._record_decision_execute({"decision": "x", "motivo": "y"}, FakeSession())
    assert "Alternativa descartada" not in _decisions_file(project).read_text(encoding="utf-8")


def test_entries_are_appended_in_order_and_never_rewritten(project):
    for i in range(3):
        d._record_decision_execute({"decision": f"dec{i}", "motivo": "m"}, FakeSession())
    text = _decisions_file(project).read_text(encoding="utf-8")
    assert text.count("# decisions.md") == 1  # encabezado una sola vez
    assert text.index("dec0") < text.index("dec1") < text.index("dec2")


def test_first_write_waits_for_lock_held_by_another_writer(project):
    """Regresion QA-002: el header se escribia con `write_bytes` (modo "wb",
    que TRUNCA) ANTES de tomar el lock -- con un lock ajeno fallaba con
    PermissionError y la decision se perdia, justo el caso que REQ-005 protege."""
    import msvcrt

    path = _decisions_file(project)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")  # existe pero vacio -> el codigo lo trata como "archivo nuevo"
    holder = open(path, "r+b")  # noqa: SIM115 -- se libera desde el Timer
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)

    def release():
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()

    timer = threading.Timer(0.3, release)
    timer.start()
    out = d._record_decision_execute({"decision": "no-perder", "motivo": "m"}, FakeSession())
    timer.join()
    assert out.startswith("Decisión registrada")
    assert "no-perder" in path.read_text(encoding="utf-8")


def test_concurrent_appends_do_not_lose_or_interleave_entries(project):
    # Archivo ya creado; la carrera de la PRIMERA escritura la cubre
    # `test_header_is_written_exactly_once_under_concurrency` (QA-002).
    d._record_decision_execute({"decision": "inicial", "motivo": "m"}, FakeSession())

    def worker(i):
        d._record_decision_execute({"decision": f"concurrente-{i}", "motivo": "m" * 50}, FakeSession(f"s{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    text = _decisions_file(project).read_text(encoding="utf-8")
    assert all(f"concurrente-{i}" in text for i in range(8))
    assert text.count("**Motivo:**") == 9  # 1 inicial + 8 concurrentes


def test_lock_timeout_becomes_error_text(project, monkeypatch):
    import msvcrt

    def always_locked(fd, mode, nbytes):
        raise OSError("bloqueado")

    monkeypatch.setattr(msvcrt, "locking", always_locked)
    monkeypatch.setattr(d, "LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(d, "LOCK_POLL_INTERVAL_S", 0.01)
    out = d._record_decision_execute({"decision": "x", "motivo": "y"}, FakeSession())
    assert out.startswith("Error: no se pudo escribir decisions.md")


def test_tool_metadata():
    assert d.record_decision_tool.thread_safe is False
    assert d.record_decision_tool.parameters["required"] == ["decision", "motivo"]


def test_header_is_written_exactly_once_under_concurrency(project):
    """QA-002: el encabezado se decide DENTRO del lock -- dos escritores que
    arrancan a la vez sobre un decisions.md vacio no deben duplicarlo."""
    threads = [
        threading.Thread(
            target=d._record_decision_execute,
            args=({"decision": f"d{i}", "motivo": "m"}, FakeSession(f"s{i}")),
        )
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = _decisions_file(project).read_text(encoding="utf-8")
    assert text.count("# decisions.md") == 1
    assert text.startswith("# decisions.md")
    assert all(f"d{i}" in text for i in range(8))


def test_existing_content_is_never_truncated(project):
    """QA-002: `write_bytes` truncaba; si el archivo ya tenia decisiones y
    quedaba en 0 bytes por otra razon, no se debe perder lo que hay."""
    d._record_decision_execute({"decision": "primera", "motivo": "m"}, FakeSession())
    before = _decisions_file(project).read_text(encoding="utf-8")
    d._record_decision_execute({"decision": "segunda", "motivo": "m"}, FakeSession())
    after = _decisions_file(project).read_text(encoding="utf-8")
    assert after.startswith(before) and "segunda" in after
