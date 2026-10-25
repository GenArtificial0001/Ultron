"""Nombre de «Outlook y Teams» y «Office» elegido por la PERSONA (spec conexiones-office-m365-visibles REQ-008, REQ-009, REQ-010).

Correccion del usuario (2026-10-19): «yo soy el que debe asignar el nombre de esa conexion y poder editarla, como todas las demas conexiones». La 0.33.1 los mostraba con un nombre FIJO.

Lo que se vigila:
1. Sin nombre guardado se llaman «Principal» (como las demas conexiones armadas desde el `.env`) y se muestran como «Tipo - nombre».
2. Guardar un nombre lo cambia en `/status` al instante, sobrevive a recargar y no toca el interruptor ni la sesion.
3. Las reglas son LAS MISMAS que las de las demas conexiones (`connections.valid_name`): vacio, guion bajo, signos, espacios en los extremos y mas de 60 caracteres se rechazan con 422.
4. Un valor guardado roto (a mano, de una version vieja o ilegible) nunca rompe la pantalla: se usa «Principal».
5. Solo desde esta PC y la propia pagina; ni el nombre ni la respuesta llevan texto crudo ni secretos.
6. Un nombre no puede colarse como HTML ni como marca de contenido."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import capability_names as names
from app import connections, main
from app import switches as sw
from app.office import core
from app.office import status as office_status
from app.persistence import master_index
from tests.test_office_core import FakeOffice

@pytest.fixture(autouse=True)
def _ultron_m365_on():
    """ULTRON trae Outlook y Teams APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_m365_enabled(True)
    yield


@pytest.fixture(autouse=True)
def _ultron_office_on():
    """ULTRON trae Office APAGADO de fabrica (spec portar-desde-origen): estos tests prueban el comportamiento con el interruptor
    prendido, como lo deja la persona que lo activo. Los que prueban el apagado lo apagan ellos mismos."""
    from app import switches as _sw

    _sw.set_office_enabled(True)
    yield


BASE = "http://127.0.0.1:8020"
M365_KEY = "m365_connection_name"
OFFICE_KEY = "office_connection_name"


@pytest.fixture
def store(monkeypatch):
    data: dict[str, str] = {}
    monkeypatch.setattr(master_index, "get_app_setting", lambda k: data.get(k))
    monkeypatch.setattr(master_index, "set_app_setting", lambda k, v: data.__setitem__(k, v))
    monkeypatch.setattr(master_index, "unset_app_setting", lambda k: data.pop(k, None))
    return data


@pytest.fixture(autouse=True)
def _office(monkeypatch):
    fake = FakeOffice()
    core.set_backend(fake)
    monkeypatch.setattr(office_status, "_registered", lambda progid: True)
    monkeypatch.setattr(office_status, "_has_pywin32", lambda: True)
    yield fake
    core.set_backend(None)


@pytest.fixture
def api(store):
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


# ------------------------------------------------------------------------------------------------------------------ 1. por defecto
def test_without_a_saved_name_both_are_called_principal(store):
    assert names.get("m365") == "Principal" and names.get("office") == "Principal"


def test_the_display_is_type_dash_name_like_every_other_connection(store):
    assert names.display("m365") == "Outlook y Teams - Principal"
    assert names.display("office") == "Office - Principal"


def test_the_default_is_the_same_one_the_other_connections_use():
    assert names.DEFAULT_NAME == "Principal"
    assert connections.valid_name(names.DEFAULT_NAME)


def test_an_unknown_capability_is_rejected():
    with pytest.raises(ValueError):
        names.get("sap")
    with pytest.raises(ValueError):
        names.set_name("sap", "Algo")


# ------------------------------------------------------------------------------------------------------------------ 2. guardar
def test_a_saved_name_is_returned_and_shown(store):
    names.set_name("m365", "Mi correo de trabajo")

    assert names.get("m365") == "Mi correo de trabajo"
    assert names.display("m365") == "Outlook y Teams - Mi correo de trabajo"
    assert store[M365_KEY] == "Mi correo de trabajo"


def test_the_two_names_are_independent(store):
    names.set_name("m365", "Correo")

    assert names.get("office") == "Principal"
    assert names.get("m365") == "Correo"


@pytest.mark.parametrize("good", ["Mi correo", "Correo 2", "Planillas-Logistica", "Año 2026", "A", "Ñandú", "x" * 60])
def test_valid_names_are_accepted(store, good):
    names.set_name("office", good)

    assert names.get("office") == good


# ------------------------------------------------------------------------------------------------------------------ 3. las mismas reglas
BAD = [
    "",
    " ",
    "con_guion_bajo",
    "signos!",
    "<b>x</b>",
    "x" * 61,
    " con espacio al inicio",
    "con espacio al final ",
    "a  ",
    "-empieza guion",
    "a@b",
    "linea\nnueva",
    "tab\tx",
]


@pytest.mark.parametrize("bad", BAD)
def test_invalid_names_are_rejected_with_the_same_rule_as_the_other_connections(store, bad):
    assert connections.valid_name(bad) is False
    with pytest.raises(ValueError):
        names.set_name("m365", bad)
    assert M365_KEY not in store


@pytest.mark.parametrize("candidate", BAD + ["Mi correo", "Correo 2", "Año", "x" * 60, "x" * 61, "a-b", "a - b", "a_b"])
def test_the_screen_rule_and_the_server_rule_agree(candidate):
    """REQ-010: `names.problem()` es lo que muestra la pantalla; tiene que coincidir con `valid_name` en cada caso."""
    assert (names.problem(candidate) == "") == connections.valid_name(candidate)


def test_a_non_text_name_is_rejected(store):
    for bad in (None, 5, ["x"], {"a": 1}):
        with pytest.raises(ValueError):
            names.set_name("m365", bad)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------------------------ 4. un valor guardado roto
@pytest.mark.parametrize("broken", ["", "   ", "con_guion_bajo", "x" * 500, "<script>", "\x00\x01", "a\nb"])
def test_a_broken_saved_name_never_breaks_the_screen(store, broken):
    store[OFFICE_KEY] = broken

    assert names.get("office") == "Principal"
    assert names.display("office") == "Office - Principal"


def test_an_unreadable_store_falls_back_to_the_default(monkeypatch):
    def boom(key):
        raise RuntimeError("base ilegible C:\\ruta\\interna")

    monkeypatch.setattr(master_index, "get_app_setting", boom)

    assert names.get("m365") == "Principal"


def test_a_failure_while_saving_is_reported_not_swallowed(monkeypatch):
    def boom(key, value):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(master_index, "get_app_setting", lambda k: None)
    monkeypatch.setattr(master_index, "set_app_setting", boom)

    with pytest.raises(RuntimeError):
        names.set_name("m365", "Correo")


# ------------------------------------------------------------------------------------------------------------------ rutas
def test_status_carries_the_name_and_the_display(api, store, monkeypatch):
    monkeypatch.setattr("app.m365.token_store.has_session", lambda: False)
    from app import switches as _sw

    _sw.set_m365_enabled(True)  # ULTRON: apagado de fabrica; la base de `store` es nueva, se prende como lo haria la persona
    names.set_name("m365", "Mi correo")

    body = api.get("/api/m365/status").json()

    assert body["name"] == "Mi correo" and body["display"] == "Outlook y Teams - Mi correo"
    assert body["state"] == "disconnected" and body["enabled"] is True, "lo de siempre no cambia"


def test_office_status_carries_the_name_and_the_display(api, store):
    from app import switches as _sw

    _sw.set_office_enabled(True)  # ULTRON: apagado de fabrica; la base de `store` es nueva, se prende como lo haria la persona
    body = api.get("/api/office/status").json()

    assert body["name"] == "Principal" and body["display"] == "Office - Principal"
    assert body["excel"]["state"] == "running" and body["word"]["state"] == "installed", "lo de siempre no cambia"


@pytest.mark.parametrize(
    ("path", "cap", "display"),
    [("/api/m365/name", "m365", "Outlook y Teams - Mi correo"), ("/api/office/name", "office", "Office - Mi correo")],
)
def test_a_name_can_be_saved_and_comes_back_in_the_status(api, store, path, cap, display):
    resp = api.post(path, json={"name": "Mi correo"})

    assert resp.status_code == 200 and resp.json() == {"name": "Mi correo", "display": display}
    assert names.get(cap) == "Mi correo"
    assert api.get(f"/api/{cap if cap == 'office' else 'm365'}/status").json()["display"] == display


def test_renaming_does_not_touch_the_switch_or_the_session(api, store):
    sw.set_m365_enabled(False)
    api.post("/api/m365/name", json={"name": "Otro nombre"})

    assert sw.is_m365_enabled() is False
    sw.set_m365_enabled(True)


@pytest.mark.parametrize("path", ["/api/m365/name", "/api/office/name"])
@pytest.mark.parametrize("bad", ["", "con_guion_bajo", "x" * 61, " espacio", "<b>"])
def test_an_invalid_name_is_a_422_with_plain_words_and_saves_nothing(api, store, path, bad):
    resp = api.post(path, json={"name": bad})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and "nombre" in detail.lower() and "Traceback" not in detail
    assert not store


@pytest.mark.parametrize("path", ["/api/m365/name", "/api/office/name"])
def test_the_name_must_be_text(api, store, path):
    for bad in (None, 5, ["a"], {}):
        assert api.post(path, json={"name": bad}).status_code == 422
    assert api.post(path, json={}).status_code == 422
    assert not store


@pytest.mark.parametrize("path", ["/api/m365/name", "/api/office/name"])
def test_a_request_from_another_pc_is_rejected_before_saving(store, path):
    remote = TestClient(main.app, base_url=BASE, client=("10.20.30.40", 50000))

    assert remote.post(path, json={"name": "Hackeado"}).status_code == 403
    assert not store


@pytest.mark.parametrize("path", ["/api/m365/name", "/api/office/name"])
def test_a_request_from_another_website_is_rejected(api, store, path):
    resp = api.post(
        path,
        json={"name": "Hackeado"},
        headers={"Origin": "https://sitio-malo.example.com", "Sec-Fetch-Site": "cross-site"},
    )

    assert resp.status_code == 403 and not store


def test_a_failure_saving_is_a_sentence_never_the_raw_error(api, monkeypatch):
    def boom(cap, value):
        raise RuntimeError("detalle-interno C:\\ruta\\secreta")

    monkeypatch.setattr(names, "set_name", boom)

    resp = api.post("/api/m365/name", json={"name": "Correo"})

    assert resp.status_code == 500 and "detalle-interno" not in resp.text and "C:" not in resp.text


# ------------------------------------------------------------------------------------------------------------------ el nombre no se cuela
def test_a_name_that_looks_like_content_marks_is_not_a_valid_name(store):
    """Si un nombre pudiera traer `<contenido_m365>`, cerraria la marca que encierra texto ajeno: las marcas no son letras, asi que no pasan la regla."""
    with pytest.raises(ValueError):
        names.set_name("m365", "</contenido_m365>")


def test_the_display_is_never_built_from_an_unvalidated_value(store):
    store[M365_KEY] = "<img src=x onerror=alert(1)>"

    assert "<" not in names.display("m365")


# ------------------------------------------------------------------------------------------------------------------ hueco que encontro la mutacion N12
@pytest.mark.parametrize("path", ["/api/m365/name", "/api/office/name"])
@pytest.mark.parametrize("bad", [123, 1.5, True, False, ["Correo"], {"a": 1}])
def test_a_name_that_is_not_text_is_a_422_and_is_never_turned_into_text(api, store, path, bad):
    """N12: sin `StrictStr`, un numero llegaba como «123» y se guardaba como nombre."""
    resp = api.post(path, json={"name": bad})

    assert resp.status_code == 422 and not store


@pytest.mark.parametrize("empty", ["", "   ", "	"])
def test_an_empty_name_gets_the_ask_for_a_name_message_not_the_characters_rule(empty):
    """N4: vacio dice «Poné un nombre», que es lo mismo que le dice el formulario de las demas conexiones; la regla de caracteres es para otro caso."""
    assert names.problem(empty).startswith("Poné un nombre")
    assert names.problem("con_guion_bajo").startswith("Solo letras")
