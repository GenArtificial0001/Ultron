"""Estado de Office para Configuracion > Conexiones (spec office-com-automatizacion REQ-015) y sus rutas `/api/office/*` (REQ-009).

Lo que se vigila:
1. Excel se informa como abierto / cerrado / ocupado / no disponible, y Word como instalado / no instalado, SIN lanzar nada: Excel solo se mira si ya esta corriendo y Word
   solo por el registro de Windows (preguntarle a Word si esta abierto exigiria enganchar su COM, y eso no hace falta para decir «se puede usar»).
2. Cada estado trae una frase para la persona, sin texto crudo ni jerga.
3. Consultar el estado nunca lanza, nunca pasa del plazo y nunca guarda ni abre un libro.
4. Las rutas responden solo a esta PC y a la propia pagina; el interruptor es booleano estricto y se refleja en `/status`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app import switches as sw
from app.office import core, errors
from app.office import status as office_status
from tests.test_friendly_errors import _JARGON
from tests.test_office_core import FakeOffice

@pytest.fixture(autouse=True)
def _ultron_office_on():
    """ULTRON trae Office APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_office_enabled(True)
    yield


BASE = "http://127.0.0.1:8020"


@pytest.fixture
def office():
    fake = FakeOffice()
    core.set_backend(fake)
    yield fake
    core.set_backend(None)


@pytest.fixture(autouse=True)
def _installed(monkeypatch):
    """Por defecto Office esta instalado y pywin32 presente; cada test lo cambia si hace falta."""
    monkeypatch.setattr(office_status, "_registered", lambda progid: True)
    monkeypatch.setattr(office_status, "_has_pywin32", lambda: True)


@pytest.fixture
def api():
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


# ------------------------------------------------------------------------------------------------------------------ Excel
def test_excel_with_open_books_is_reported_as_running_with_the_count(office):
    snap = office_status.snapshot()

    assert snap["excel"]["state"] == "running" and "1 libro" in snap["excel"]["message"]


def test_excel_running_without_books_is_still_running(office):
    office.books.clear()

    assert office_status.snapshot()["excel"]["state"] == "running"


def test_excel_closed_says_to_open_it_and_does_not_launch_it(office):
    office.running = False

    snap = office_status.snapshot()

    assert snap["excel"]["state"] == "closed" and "abr" in snap["excel"]["message"].lower()
    assert office.launched is False and office.saved is False


def test_excel_with_a_dialog_open_is_busy_not_closed(office):
    office.dialog_open = True

    assert office_status.snapshot()["excel"]["state"] == "busy"


def test_excel_not_installed_is_reported_without_touching_com(office, monkeypatch):
    monkeypatch.setattr(office_status, "_registered", lambda progid: progid != "Excel.Application")

    snap = office_status.snapshot()

    assert snap["excel"]["state"] == "missing"
    assert not office.calls, "si no esta instalado ni se intenta enganchar"


def test_a_pc_without_pywin32_says_a_component_is_missing_and_does_not_break(office, monkeypatch):
    monkeypatch.setattr(office_status, "_has_pywin32", lambda: False)

    snap = office_status.snapshot()

    assert snap["excel"]["state"] == "missing" and snap["word"]["state"] == "missing", "convertir tambien necesita ese componente"
    assert not office.calls


def test_a_slow_excel_is_cut_at_the_deadline_and_reported_as_busy(office):
    office.delay = 2.0

    import time

    started = time.monotonic()
    snap = office_status.snapshot(timeout=0.3)

    assert time.monotonic() - started < 1.5 and snap["excel"]["state"] == "busy"


def test_an_unexpected_failure_is_reported_as_closed_with_a_phrase_and_never_raises(office, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("fallo interno CONTENIDO-DE-CELDA-1")

    monkeypatch.setattr(core, "list_workbooks", boom)

    snap = office_status.snapshot()

    assert snap["excel"]["state"] == "unknown"
    assert "CONTENIDO-DE-CELDA-1" not in str(snap) and "RuntimeError" not in str(snap)


# ------------------------------------------------------------------------------------------------------------------ Word
def test_word_is_reported_installed_from_the_registry_only(office):
    snap = office_status.snapshot()

    assert snap["word"]["state"] == "installed"
    office.calls.clear()
    assert office_status._word_status()["state"] == "installed"
    assert not office.calls, "Word no se consulta por COM"


def test_word_not_installed_is_reported(office, monkeypatch):
    monkeypatch.setattr(office_status, "_registered", lambda progid: progid != "Word.Application")

    assert office_status.snapshot()["word"]["state"] == "missing"


# ------------------------------------------------------------------------------------------------------------------ frases e interruptor
@pytest.mark.parametrize("scenario", ["running", "closed", "busy", "missing", "unknown"])
def test_every_phrase_is_plain_words(office, monkeypatch, scenario):
    if scenario == "closed":
        office.running = False
    elif scenario == "busy":
        office.dialog_open = True
    elif scenario == "missing":
        monkeypatch.setattr(office_status, "_registered", lambda progid: False)
    elif scenario == "unknown":
        monkeypatch.setattr(core, "list_workbooks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

    snap = office_status.snapshot()

    for app_name in ("excel", "word"):
        message = snap[app_name]["message"]
        assert message.endswith(".") and not _JARGON.search(message), message


def test_the_snapshot_carries_the_switch_state(office):
    assert office_status.snapshot()["enabled"] is True

    sw.set_office_enabled(False)

    assert office_status.snapshot()["enabled"] is False


def test_a_switched_off_office_is_not_probed_through_com(office):
    """Apagado, Vision no toca COM ni para mirar: el estado dice que esta apagado y nada mas (REQ-009)."""
    sw.set_office_enabled(False)

    snap = office_status.snapshot()

    assert not office.calls and snap["excel"]["state"] == "off" and snap["word"]["state"] == "off"


def test_the_registry_check_really_runs_on_this_machine_without_raising():
    """Sin mocks: no importa la respuesta, importa que no lance en esta PC ni en una sin Office."""
    assert isinstance(office_status._registry_has("Excel.Application"), bool)
    assert office_status._registry_has("Esto.NoExiste.Nunca") is False


# ------------------------------------------------------------------------------------------------------------------ rutas
def test_status_route_answers_with_the_snapshot_and_never_caches(api, office):
    resp = api.get("/api/office/status")

    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["excel"]["state"] == "running" and body["word"]["state"] == "installed" and body["enabled"] is True


def test_switch_route_turns_office_off_and_on_and_status_follows(api, office):
    assert api.post("/api/office/switch", json={"enabled": False}).json() == {"enabled": False}
    assert api.get("/api/office/status").json()["enabled"] is False
    assert sw.is_office_enabled() is False

    assert api.post("/api/office/switch", json={"enabled": True}).json() == {"enabled": True}
    assert sw.is_office_enabled() is True


@pytest.mark.parametrize("bad", ["no", "false", 0, 1, None, "", [], {}])
def test_switch_must_be_a_real_boolean(api, bad):
    """Un «no» o un 0 mal escrito no puede apagar Office sin avisar."""
    resp = api.post("/api/office/switch", json={"enabled": bad})

    assert resp.status_code == 422 and sw.is_office_enabled() is True


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("get", "/api/office/status", None), ("post", "/api/office/switch", {"enabled": False})],
)
def test_a_request_from_another_pc_is_rejected_before_doing_anything(office, method, path, body):
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))

    resp = getattr(remote, method)(path, **({"json": body} if body else {}))

    assert resp.status_code == 403 and sw.is_office_enabled() is True and not office.calls


def test_a_request_from_another_website_is_rejected(api, office):
    resp = api.post(
        "/api/office/switch",
        json={"enabled": False},
        headers={"Origin": "https://sitio-malo.example.com", "Sec-Fetch-Site": "cross-site"},
    )

    assert resp.status_code == 403 and sw.is_office_enabled() is True


def test_status_route_never_leaks_a_raw_failure(api, monkeypatch):
    monkeypatch.setattr(
        office_status, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detalle-interno C:\\ruta"))
    )

    resp = api.get("/api/office/status")

    assert resp.status_code == 500 and "detalle-interno" not in resp.text and "C:" not in resp.text


def test_the_error_module_exposes_what_the_status_relies_on():
    assert issubclass(errors.OfficeNotRunning, errors.OfficeError) and issubclass(errors.OfficeBusy, errors.OfficeError)
