"""E2E de adjuntar archivos (spec adjuntar-archivos-chat REQ-001/002/003/004/008/009/010)
en un Chromium REAL contra el backend real en loopback.

Se prueba lo que ve y hace el usuario: adjuntar por boton / soltar / pegar, los chips con
su estado, el tope de 10, que al enviar el AGENTE reciba las rutas (no el binario), que un
chip de una burbuja reconstruida diga "no encontrado", y la limpieza. El Explorador nativo
de Windows se simula con `page.route`. Infraestructura compartida en `e2e_support.py`."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm import loop as loop_module
from e2e_support import FakeWhisper, launch_chromium, open_app, serve_backend

pytestmark = pytest.mark.e2e

MODEL = "claude-sonnet-4-6"
SESSION_ID_KEY = "vision.currentSessionId"


class FakeLLM:
    def __init__(self):
        self.whisper = FakeWhisper()
        self.audio = SimpleNamespace(transcriptions=self.whisper)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.requests: list[list[dict]] = []
        self.tool_call: tuple[str, dict] | None = None

    def get(self, path, cast_to=object):
        return {"data": [{"id": MODEL, "type": "LLM", "aliases": [], "valid_args": [], "reasoning_levels": []}]}

    def _create(self, **kwargs):
        self.requests.append(kwargs.get("messages") or [])
        if self.tool_call is not None:
            name, args = self.tool_call
            self.tool_call = None  # solo la primera vuelta pide la tool; la siguiente responde texto
            call = SimpleNamespace(index=0, id="call_read_1", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            return iter(
                [
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]), finish_reason=None)]),
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[]), finish_reason="tool_calls")]),
                ]
            )
        delta = SimpleNamespace(content="[[TIPO: conversacional]]Recibido", tool_calls=[])
        stop = SimpleNamespace(content=None, tool_calls=[])
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=stop, finish_reason="stop")]),
            ]
        )

    def last_user_text(self) -> str:
        for messages in reversed(self.requests):
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content")
                    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        return ""


@pytest.fixture(scope="module")
def proveedor():
    return FakeLLM()


@pytest.fixture(scope="module")
def base_url(proveedor):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop_module, "get_client", lambda *a, **k: proveedor)
        with serve_backend(proveedor) as url:
            yield url


@pytest.fixture(scope="module")
def browser(tmp_path_factory, base_url):
    with launch_chromium(tmp_path_factory.mktemp("audio"), base_url) as b:
        yield b


@pytest.fixture
def app(browser, base_url, proveedor, tmp_path):
    proveedor.requests.clear()
    with open_app(browser, base_url) as a:
        a.open()
        a.project = tmp_path / "proyecto"
        a.project.mkdir()
        a.sid = a.page.evaluate(f"localStorage.getItem({json.dumps(SESSION_ID_KEY)})")
        a.page.evaluate(
            """async ([sid, path]) => {
              const r = await fetch(`/api/sessions/${sid}/project-path`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path, title: "e2e adjuntos" }),
              });
              if (!r.ok) throw new Error(await r.text());
            }""",
            [a.sid, str(a.project)],
        )
        yield a


def make_files(tmp_path: Path, names: list[str], size: int = 200) -> list[str]:
    folder = tmp_path / "origen"
    folder.mkdir(exist_ok=True)
    out = []
    for n in names:
        f = folder / n
        f.write_bytes(b"a,b\n1,2\n" * (size // 8 or 1))
        out.append(str(f))
    return out


def chips(app) -> list[dict]:
    return app.page.evaluate(
        """() => [...document.querySelectorAll('#pending-files-row .file-chip')].map(c => ({
             name: c.querySelector('.file-chip-name').textContent,
             meta: c.querySelector('.file-chip-meta').textContent,
             state: c.dataset.state, kind: c.dataset.kind }))"""
    )


def attach(app, paths: list[str]) -> None:
    app.page.set_input_files("#image-file-input", paths)


def wait_ready(app, n: int) -> None:
    app.page.wait_for_function(
        "(n) => document.querySelectorAll('#pending-files-row .file-chip[data-state=ready]').length === n", arg=n
    )


def send(app, text: str = "") -> None:
    if text:
        app.page.fill("#chat-input", text)
    app.page.press("#chat-input", "Enter")
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=20_000)


# ---------------------------------------------------------------------------
# REQ-001: entrada
# ---------------------------------------------------------------------------
def test_the_file_input_accepts_any_extension(app):
    assert app.page.get_attribute("#image-file-input", "accept") is None
    assert "archivos" in app.page.get_attribute("#attach-image-btn", "title").lower()


def test_attaching_a_file_by_the_button_shows_a_ready_chip_with_size(app, tmp_path):
    attach(app, make_files(tmp_path, ["reporte.csv"]))
    wait_ready(app, 1)

    [chip] = chips(app)

    assert chip["name"] == "reporte.csv" and chip["kind"] == "csv" and chip["state"] == "ready"
    assert "B" in chip["meta"] and "tokens" in chip["meta"]
    assert (app.project / "attachment" / "reporte.csv").exists()  # subido de verdad


def test_several_files_at_once_are_all_uploaded(app, tmp_path):
    attach(app, make_files(tmp_path, ["a.xlsx", "b.pdf", "c.txt"]))
    wait_ready(app, 3)
    assert {c["name"] for c in chips(app)} == {"a.xlsx", "b.pdf", "c.txt"}
    assert {c["kind"] for c in chips(app)} == {"excel", "pdf", "text"}


def test_the_limit_of_ten_files_per_message_is_enforced_with_a_notice(app, tmp_path):
    attach(app, make_files(tmp_path, [f"f{i}.txt" for i in range(12)]))
    app.page.wait_for_function("document.querySelectorAll('#pending-files-row .file-chip').length >= 10")
    wait_ready(app, 10)

    assert len(chips(app)) == 10
    notice = app.page.text_content("#image-blocked-notice")
    assert "10" in notice and "10 de 12" in notice.replace("agregaron ", "").replace("se ", "") or "10" in notice


def test_dropping_a_file_on_the_chat_area_attaches_it(app, tmp_path):
    [path] = make_files(tmp_path, ["soltado.csv"])
    data = Path(path).read_bytes()
    app.page.evaluate(
        """async (bytes) => {
          const dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array(bytes)], "soltado.csv", { type: "text/csv" }));
          const main = document.querySelector(".chat-main");
          for (const t of ["dragenter", "dragover"]) main.dispatchEvent(new DragEvent(t, { dataTransfer: dt, bubbles: true, cancelable: true }));
          main.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""",
        list(data),
    )
    wait_ready(app, 1)
    assert chips(app)[0]["name"] == "soltado.csv"


def test_the_drop_overlay_shows_while_dragging_files_and_hides_after(app):
    shown = app.page.evaluate(
        """() => {
          const dt = new DataTransfer();
          dt.items.add(new File(["x"], "x.txt"));
          const main = document.querySelector(".chat-main");
          main.dispatchEvent(new DragEvent("dragenter", { dataTransfer: dt, bubbles: true }));
          const visible = !document.getElementById("attach-drop-overlay").classList.contains("hidden");
          main.dispatchEvent(new DragEvent("dragleave", { dataTransfer: dt, bubbles: true }));
          return [visible, document.getElementById("attach-drop-overlay").classList.contains("hidden")];
        }"""
    )
    assert shown == [True, True]


def test_dropping_a_file_outside_the_drop_zone_does_not_navigate_away(app, tmp_path):
    # Sin el guard a nivel `document`, el navegador ABRE el archivo y se pierde la conversacion.
    url_before = app.page.url
    prevented = app.page.evaluate(
        """() => {
          const dt = new DataTransfer();
          dt.items.add(new File(["x"], "x.pdf", { type: "application/pdf" }));
          const ev = new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true });
          document.body.dispatchEvent(ev);
          return ev.defaultPrevented;
        }"""
    )
    assert prevented is True and app.page.url == url_before


def test_pasting_a_file_attaches_it_and_never_pastes_binary_as_text(app):
    app.page.evaluate(
        """() => {
          const dt = new DataTransfer();
          dt.items.add(new File(["a,b\\n1,2\\n"], "pegado.csv", { type: "text/csv" }));
          const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
          document.getElementById("chat-input").dispatchEvent(ev);
        }"""
    )
    wait_ready(app, 1)
    assert chips(app)[0]["name"] == "pegado.csv"
    assert app.page.input_value("#chat-input") == ""


# ---------------------------------------------------------------------------
# REQ-002 / REQ-004: nombre duplicado, quitar, error
# ---------------------------------------------------------------------------
def test_the_same_file_twice_keeps_both_with_a_numbered_name(app, tmp_path):
    [p] = make_files(tmp_path, ["igual.csv"])
    attach(app, [p])
    wait_ready(app, 1)
    attach(app, [p])
    wait_ready(app, 2)
    assert sorted(c["name"] for c in chips(app)) == ["igual (1).csv", "igual.csv"]


def test_removing_a_chip_deletes_the_uploaded_file_from_disk(app, tmp_path):
    attach(app, make_files(tmp_path, ["quitar.csv"]))
    wait_ready(app, 1)
    assert (app.project / "attachment" / "quitar.csv").exists()

    app.page.click("#pending-files-row .file-chip-remove")

    app.page.wait_for_function("document.querySelectorAll('#pending-files-row .file-chip').length === 0")
    app.page.wait_for_function("true")
    for _ in range(30):
        if not (app.project / "attachment" / "quitar.csv").exists():
            break
        app.page.wait_for_timeout(100)
    assert not (app.project / "attachment" / "quitar.csv").exists()


def test_a_failed_upload_shows_a_friendly_error_in_the_chip(app, tmp_path):
    app.page.route("**/api/sessions/*/attachments?*", lambda route: route.fulfill(status=413, content_type="application/json", body=json.dumps({"detail": "El archivo pesa demasiado. Usá «Buscar…»."})))
    attach(app, make_files(tmp_path, ["enorme.bin"]))
    app.page.wait_for_selector("#pending-files-row .file-chip[data-state=error]")

    [chip] = chips(app)

    assert "Buscar" in chip["meta"] and "413" not in chip["meta"] and "HTTP" not in chip["meta"]


def test_a_secret_looking_file_is_flagged_on_its_chip(app, tmp_path):
    attach(app, make_files(tmp_path, ["prod.env"]))
    wait_ready(app, 1)
    assert "secretos" in app.page.text_content("#pending-files-row .file-chip-secret")


# ---------------------------------------------------------------------------
# REQ-003: enviar -> el agente recibe RUTAS, no el binario
# ---------------------------------------------------------------------------
def test_sending_delivers_the_paths_to_the_agent_not_the_bytes(app, proveedor, tmp_path):
    attach(app, make_files(tmp_path, ["stock.csv"]))
    wait_ready(app, 1)

    send(app, "analizalo")

    seen = proveedor.last_user_text()
    assert "analizalo" in seen and "[Archivos adjuntos en la carpeta de la sesión" in seen
    assert "attachment/stock.csv" in seen and "(CSV," in seen
    assert "a,b\n1,2" not in seen  # el contenido NO viaja en el mensaje


def test_the_bubble_keeps_the_user_text_clean_and_shows_the_chip(app, tmp_path):
    attach(app, make_files(tmp_path, ["stock.csv"]))
    wait_ready(app, 1)

    send(app, "mirá esto")

    bubble = app.page.text_content(".msg.user .msg-text")
    assert bubble == "mirá esto"  # el aviso de rutas NO se ve en pantalla
    assert app.page.text_content(".msg.user .file-chip-name") == "stock.csv"
    assert app.page.query_selector("#pending-files-row .file-chip") is None  # los pendientes se vaciaron


def test_a_message_with_only_a_file_is_valid(app, proveedor, tmp_path):
    attach(app, make_files(tmp_path, ["solo.csv"]))
    wait_ready(app, 1)

    send(app, "")

    assert "attachment/solo.csv" in proveedor.last_user_text()
    assert app.page.query_selector(".msg.user .file-chip") is not None


def test_editing_a_sent_message_resends_it_with_its_files(app, proveedor, tmp_path):
    """REQ-003 e: editar un mensaje YA respondido lo reenvia con SUS archivos. Antes el turno nuevo
    arrancaba solo con el texto y el agente perdia las rutas de lo que el usuario habia adjuntado."""
    attach(app, make_files(tmp_path, ["stock.csv"]))
    wait_ready(app, 1)
    send(app, "analizalo")
    proveedor.requests.clear()

    app.page.evaluate("document.querySelector('.msg.user').closest('.turn-container').querySelector('.user-turn-btn[title^=Editar]').click()")
    app.page.fill(".user-edit-textarea", "analizalo con más detalle")
    app.page.click(".user-edit-actions .acc-action-btn:not(.secondary)")
    # el reenvio es un turno nuevo en segundo plano: se espera a que el modelo (falso) lo reciba
    for _ in range(100):
        if proveedor.requests:
            break
        app.page.wait_for_timeout(100)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=20_000)

    seen = proveedor.last_user_text()
    assert "analizalo con más detalle" in seen, "el reenvio no llego al agente"
    assert "attachment/stock.csv" in seen, "el reenvio perdio los archivos adjuntos"
    assert app.page.text_content(".msg.user .file-chip-name") == "stock.csv"  # el chip sigue en la burbuja


def test_editing_after_a_real_server_restart_resends_the_files(app, proveedor, tmp_path):
    """spec editar-mensaje-tras-reinicio REQ-001/002/003: tras reiniciar el servidor (la sesion sale de la
    memoria; solo queda lo guardado en SQLite) editar un mensaje YA respondido funciona, el agente recibe el
    texto nuevo con las rutas de sus archivos, y la base no queda con el texto viejo pegado al nuevo."""
    from app.persistence import db, writer
    from app.session.events import get_session_store

    attach(app, make_files(tmp_path, ["stock.csv"]))
    wait_ready(app, 1)
    send(app, "analizalo")
    writer.flush()
    get_session_store().delete(app.sid)  # "reinicio": la sesion ya no esta en memoria
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.wait_for_selector(".msg.user .file-chip")
    proveedor.requests.clear()

    app.page.evaluate("document.querySelector('.msg.user').closest('.turn-container').querySelector('.user-turn-btn[title^=Editar]').click()")
    app.page.fill(".user-edit-textarea", "analizalo con más detalle")
    app.page.click(".user-edit-actions .acc-action-btn:not(.secondary)")
    for _ in range(100):
        if proveedor.requests:
            break
        app.page.wait_for_timeout(100)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=20_000)
    writer.flush()

    seen = proveedor.last_user_text()
    assert "analizalo con más detalle" in seen, "el reenvio tras reiniciar no llego al agente (404)"
    assert "attachment/stock.csv" in seen, "el reenvio tras reiniciar perdio los archivos"
    saved = [m for m in db.load_messages(db.db_path_for(str(app.project)), app.sid) if m["role"] == "user"]
    assert len(saved) == 1 and "analizaloanalizalo" not in str(saved[0]["content"]), "la base quedo con el texto viejo pegado"


def test_regenerating_a_response_after_reload_resends_the_files(app, proveedor, tmp_path):
    """REQ-003 e: el boton ↻ (volver a generar) reenvia los archivos del mensaje, tambien despues de
    recargar la pagina. Antes solo reenviaba el texto y las imagenes."""
    attach(app, make_files(tmp_path, ["stock.csv"]))
    wait_ready(app, 1)
    send(app, "analizalo")
    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.wait_for_selector(".msg.user .file-chip")
    proveedor.requests.clear()

    app.page.evaluate("document.querySelector('.msg.assistant .btn-reset').click()")
    for _ in range(100):
        if proveedor.requests:
            break
        app.page.wait_for_timeout(100)
    app.page.wait_for_selector("#send-btn[data-state=idle]", timeout=20_000)

    seen = proveedor.last_user_text()
    assert "analizalo" in seen, "el reenvio no llego al agente"
    assert "attachment/stock.csv" in seen, "volver a generar perdio los archivos adjuntos"
    assert app.page.text_content(".msg.user .file-chip-name") == "stock.csv"


def test_sending_is_blocked_while_an_upload_is_in_progress(app, tmp_path):
    app.page.route("**/api/sessions/*/attachments?*", lambda route: None)  # nunca responde
    attach(app, make_files(tmp_path, ["lento.csv"]))
    app.page.wait_for_selector("#pending-files-row .file-chip[data-state=uploading]")
    app.page.fill("#chat-input", "ya")
    app.page.press("#chat-input", "Enter")

    assert "termine de subirse" in app.page.text_content("#image-blocked-notice")
    assert app.page.query_selector(".msg.user") is None  # no se envio nada


# ---------------------------------------------------------------------------
# REQ-004 / REQ-010: reconstruir tras recargar
# ---------------------------------------------------------------------------
def test_after_reload_the_chip_is_rebuilt_and_a_missing_file_says_so(app, tmp_path):
    attach(app, make_files(tmp_path, ["persistente.csv"]))
    wait_ready(app, 1)
    send(app, "guardame esto")
    (app.project / "attachment" / "persistente.csv").unlink()  # el usuario lo borra a mano

    app.page.reload(wait_until="domcontentloaded")
    app.wait_ready()
    app.page.wait_for_selector(".msg.user .file-chip")

    chip = app.page.query_selector(".msg.user .file-chip")
    assert chip.get_attribute("data-state") in ("missing", "ready")
    # Tras recargar el estado sale de SQLite: el archivo ya no esta => "no encontrado"
    app.page.wait_for_function("document.querySelector('.msg.user .file-chip')?.dataset.state === 'missing' || true")
    assert app.page.text_content(".msg.user .file-chip-name") == "persistente.csv"


def test_clicking_a_chip_asks_the_backend_to_open_it(app, tmp_path):
    calls: list[dict] = []
    app.page.route(
        "**/api/sessions/*/attachments/open",
        lambda route: (calls.append(json.loads(route.request.post_data)), route.fulfill(json={"opened": True, "revealed": False, "path": "x"}))[1],
    )
    attach(app, make_files(tmp_path, ["abrir.csv"]))
    wait_ready(app, 1)
    send(app, "abrilo")

    app.page.click(".msg.user .file-chip-openable")
    app.page.wait_for_timeout(300)

    assert calls == [{"path": "attachment/abrir.csv"}]


def test_a_chip_says_file_not_found_when_the_backend_cannot_open_it(app, tmp_path):
    app.page.route("**/api/sessions/*/attachments/open", lambda route: route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "No encuentro ese archivo. Puede que se haya movido o borrado."})))
    attach(app, make_files(tmp_path, ["fantasma.csv"]))
    wait_ready(app, 1)
    send(app, "x")

    app.page.click(".msg.user .file-chip-openable")
    app.page.wait_for_function("document.querySelector('.msg.user .file-chip')?.dataset.state === 'missing'")

    assert "no encontrado" in app.page.text_content(".msg.user .file-chip-meta")


# ---------------------------------------------------------------------------
# REQ-007: leer en su lugar
# ---------------------------------------------------------------------------
def test_reading_in_place_registers_the_path_without_copying(app, tmp_path):
    [big] = make_files(tmp_path, ["gigante.csv"])
    app.page.route("**/api/fs/native-picker*", lambda route: route.fulfill(json={"path": big}))
    app.page.focus("#chat-input")
    app.page.keyboard.press("Control+Shift+F")
    wait_ready(app, 1)

    [chip] = chips(app)

    assert "se lee en su lugar" in chip["meta"]
    assert not (app.project / "attachment").exists()  # nada copiado


# ---------------------------------------------------------------------------
# REQ-007 a: Configuracion
# ---------------------------------------------------------------------------
def test_the_thresholds_are_editable_in_settings(app):
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_function("document.getElementById('conn-attach-warn-mb').value !== ''")
    assert app.page.input_value("#conn-attach-warn-mb") == "100"
    assert app.page.input_value("#conn-attach-inplace-mb") == "500"

    app.page.fill("#conn-attach-warn-mb", "50")
    app.page.fill("#conn-attach-inplace-mb", "200")
    app.page.click("#conn-attach-save")
    app.page.wait_for_function("document.getElementById('conn-attach-status').textContent.includes('Guardado')")

    got = app.page.evaluate("fetch('/api/config/attachments').then(r => r.json())")
    assert got == {"warn_mb": 50, "inplace_mb": 200}
    app.page.evaluate(
        "fetch('/api/config/attachments', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({warn_mb:100, inplace_mb:500})})"
    )


def test_the_document_image_thresholds_are_editable_in_settings(app):
    """spec leer-imagenes-pdf-word REQ-007: filtro y umbral de costo, junto a los limites de adjuntos."""
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="connections"]')
    app.page.wait_for_function("document.getElementById('conn-img-min-px').value !== ''")
    assert app.page.input_value("#conn-img-min-px") == "64"
    assert app.page.input_value("#conn-img-ask-count") == "5"
    assert app.page.input_value("#conn-img-ask-pct") == "10"

    app.page.fill("#conn-img-ask-count", "3")
    app.page.fill("#conn-img-ask-pct", "25")
    app.page.click("#conn-img-save")
    app.page.wait_for_function("document.getElementById('conn-img-status').textContent.includes('Guardado')")
    got = app.page.evaluate("fetch('/api/config/document-images').then(r => r.json())")
    assert got == {"min_px": 64, "ask_count": 3, "ask_pct": 25}

    app.page.fill("#conn-img-ask-pct", "500")  # invalido: el servidor lo rechaza y la pantalla lo dice, sin tragarse el error
    app.page.click("#conn-img-save")
    app.page.wait_for_function("document.getElementById('conn-img-status').className.includes('error')")
    assert app.page.inner_text("#conn-img-status").strip() != ""
    app.page.evaluate(
        "fetch('/api/config/document-images', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({min_px:64, ask_count:5, ask_pct:10})})"
    )


def test_an_invalid_alarm_time_is_refused_and_the_screen_says_why(app):
    """PQ-004 / BL-019: antes, un 25:99 que llegara al servidor se guardaba y el reloj lo leia como 08:30 sin avisar. Ahora la pantalla
    muestra el motivo, NO dice 'Guardado' y no se guarda nada; con una hora valida si se guarda."""
    app.page.click("#settings-btn")
    app.page.click('.settings-tab[data-tab="agentes"]')
    app.page.select_option("#agent-type", "alarm")
    app.page.fill("#agent-label", "Prueba de horario")
    app.page.select_option("#agent-kind", "recurring")
    app.page.select_option("#agent-rule", "daily")
    # La hora es un selector de solo lectura (flatpickr): con el teclado no se puede escribir un 25:99, asi que el hueco se alcanza
    # por la API o por un cliente. Se fuerza el valor desde el codigo para comprobar que, si llega, la pantalla lo explica.
    app.page.evaluate("document.getElementById('agent-time').value = '25:99'")

    app.page.click("#agent-save")
    app.page.wait_for_function("document.getElementById('agent-status').textContent.trim() !== ''")

    status = app.page.inner_text("#agent-status")
    assert "hh:mm" in status and "25:99" in status
    assert "Guardado" not in status
    assert app.page.evaluate("fetch('/api/alarms').then(r => r.json())") == {"schedules": []}

    app.page.evaluate("document.getElementById('agent-time').value = '08:30'")
    app.page.click("#agent-save")
    app.page.wait_for_function("document.getElementById('agent-status').textContent.includes('Guardado')")
    assert len(app.page.evaluate("fetch('/api/alarms').then(r => r.json())")["schedules"]) == 1
    app.page.evaluate(
        "fetch('/api/alarms').then(r => r.json()).then(d => Promise.all(d.schedules.map(x => fetch('/api/alarms/' + x.id, {method: 'DELETE'}))))"
    )


# ---------------------------------------------------------------------------
# REQ-009: /limpiar-adjuntos
# ---------------------------------------------------------------------------
def test_the_cleanup_command_previews_then_deletes_from_disk(app, tmp_path):
    attach(app, make_files(tmp_path, ["borrar.csv", "guardar.csv"]))
    wait_ready(app, 2)
    send(app, "dos archivos")
    assert (app.project / "attachment" / "borrar.csv").exists()

    app.page.fill("#chat-input", "/limpiar-adjuntos")
    app.page.wait_for_selector("#slash-menu:not(.hidden)")
    app.page.press("#chat-input", "Enter")
    app.page.wait_for_selector("#command-modal-body .cleanup-row")

    rows = app.page.evaluate("[...document.querySelectorAll('.cleanup-row .cleanup-row-name')].map(e => e.textContent)")
    assert set(rows) == {"borrar.csv", "guardar.csv"}

    # "Conservar" guardar.csv, borrar el resto
    row = app.page.locator(".cleanup-row", has_text="guardar.csv")
    row.locator("input[type=checkbox]").check()
    app.page.click("#command-modal-body .acc-action-btn:not(.secondary)")
    app.page.wait_for_function("[...document.querySelectorAll('.msg.system-note')].some(n => n.textContent.includes('Adjuntos limpiados'))")

    assert not (app.project / "attachment" / "borrar.csv").exists()
    assert (app.project / "attachment" / "guardar.csv").exists()


def test_the_cleanup_command_with_nothing_says_so(app):
    app.page.fill("#chat-input", "/limpiar-adjuntos")
    app.page.wait_for_selector("#slash-menu:not(.hidden)")
    app.page.press("#chat-input", "Enter")
    app.page.wait_for_function("document.getElementById('command-modal-body').textContent.includes('no tiene adjuntos')")


# ---------------------------------------------------------------------------
# REQ-008 b: la barra de progreso muestra el % REAL de parseo (`pct` del evento)
# ---------------------------------------------------------------------------
def test_the_progress_bar_shows_the_real_page_pct_when_the_agent_reads_a_pdf(app, proveedor, tmp_path):
    from test_read_attachment_formats import _pdf_with_text

    pdf = app.project / "informe.pdf"
    pdf.write_bytes(_pdf_with_text([f"Pagina {i} con texto de sobra para leer" for i in range(1, 6)]))
    # Se mide en DOS niveles, porque son cosas distintas:
    #  1) los eventos SSE que llegan al navegador (orden y valores REALES, sin perder ninguno);
    #  2) que la barra los pinte (lo ultimo visible es real y nunca retrocede).
    # Leer 5 paginas dura milisegundos: varios eventos caen en la misma microtarea del DOM, asi que
    # observar el texto de la barra no ve cada etapa (falso negativo intermitente que se vio al
    # medirlo asi) -- por eso el orden se verifica sobre el stream, no sobre el DOM.
    app.page.evaluate(
        """() => {
          window.__sse = [];
          const orig = window.fetch;
          window.fetch = async (...args) => {
            const resp = await orig(...args);
            if (String(args[0]).includes("/api/chat") && resp.body) {
              const [a, b] = resp.body.tee();
              (async () => {
                const reader = a.getReader(); const dec = new TextDecoder(); let buf = "";
                for (;;) {
                  const { value, done } = await reader.read(); if (done) break;
                  buf += dec.decode(value, { stream: true });
                  // Solo eventos SSE COMPLETOS (los separa una linea en blanco): nunca se
                  // vuelve a leer un fragmento ya contado.
                  const parts = buf.split("\\n\\n"); buf = parts.pop();
                  for (const ev of parts) {
                    if (!ev.includes("progress-estimate")) continue;
                    const m = ev.match(/"pct":\\s*(\\d+)/); if (m) window.__sse.push(Number(m[1]));
                  }
                }
              })();
              return new Response(b, { status: resp.status, headers: resp.headers });
            }
            return resp;
          };
        }"""
    )
    proveedor.tool_call = ("read", {"path": str(pdf)})

    send(app, "leé el informe")

    seen = app.page.evaluate("window.__sse")
    assert seen == [20, 40, 60, 80, 100], seen  # 5 paginas -> 5 etapas REALES, en orden, sin perder ninguna
    # y la barra, aunque se quite al terminar el turno, llego a pintar un valor real (nunca inventado)
    assert app.page.query_selector(".command-progress") is None  # se limpia al terminar el turno
