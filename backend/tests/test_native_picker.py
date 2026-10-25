"""Selector nativo de carpeta/archivo (`GET /api/fs/native-picker`, spec programador-alarmas y adjuntar-archivos-chat, BL-006).

El endpoint abre el dialogo REAL del Explorador de Windows desde el backend (tkinter), asi que estos tests reemplazan `tkinter` y `tkinter.filedialog` por falsos: NUNCA se abre una
ventana. Lo que importa: que cada modo (carpeta, archivo, cualquier archivo) llame al dialogo correcto con el filtro correcto, que cancelar devuelva '' sin error, que un segundo pedido
mientras el primero sigue abierto se rechace con 409 (dos `Tk()` a la vez corrompian el interprete de Tcl y el explorador dejaba de abrir hasta reiniciar), que el lock se libere SIEMPRE
(si no, el explorador quedaria bloqueado para siempre) y que en un entorno sin pantalla el error sea claro.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


class Dialogs:
    """Registra que dialogo se abrio y con que argumentos; `answer` es lo que 'elige' la persona ('' = cancela)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.answer = "C:/elegida"
        self.raises: Exception | None = None
        self.tk_created = 0
        self.tk_destroyed = 0
        self.gate: threading.Event | None = None  # si se define, el dialogo espera a que el test lo suelte
        self.opened = threading.Event()

    def _open(self, kind, **kw):
        self.calls.append((kind, kw))
        self.opened.set()
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.raises:
            raise self.raises
        return self.answer


@pytest.fixture
def dialogs(monkeypatch):
    d = Dialogs()

    class FakeTk:
        def __init__(self):
            if d.raises_on_tk:
                raise RuntimeError("no display name and no $DISPLAY environment variable")
            d.tk_created += 1

        def withdraw(self):
            pass

        def attributes(self, *a):
            pass

        def lift(self):
            pass

        def focus_force(self):
            pass

        def destroy(self):
            d.tk_destroyed += 1

    d.raises_on_tk = False
    tk = types.ModuleType("tkinter")
    tk.Tk = FakeTk
    fd = types.ModuleType("tkinter.filedialog")
    fd.askdirectory = lambda **kw: d._open("directory", **kw)
    fd.askopenfilename = lambda **kw: d._open("file", **kw)
    tk.filedialog = fd
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fd)
    monkeypatch.setattr(main, "_force_foreground_window", lambda root: None)  # no toca Win32 ni envia teclas de verdad
    return d


@pytest.fixture(autouse=True)
def lock_is_free():
    """Cada test arranca con el lock libre y lo deja libre."""
    while main._native_picker_lock.locked():
        main._native_picker_lock.release()
    yield
    while main._native_picker_lock.locked():
        main._native_picker_lock.release()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Que dialogo se abre en cada modo
# ---------------------------------------------------------------------------
class TestModes:
    def test_the_default_mode_picks_a_folder(self, client, dialogs):
        r = client.get("/api/fs/native-picker")

        assert r.status_code == 200 and r.json() == {"path": "C:/elegida"}
        kind, kw = dialogs.calls[0]
        assert kind == "directory" and "carpeta de proyecto" in kw["title"]

    def test_file_mode_filters_executables_and_scripts_first(self, client, dialogs):
        client.get("/api/fs/native-picker?mode=file")

        kind, kw = dialogs.calls[0]
        assert kind == "file" and "ejecutar" in kw["title"]
        assert kw["filetypes"][0] == ("Ejecutables/Scripts", "*.exe;*.bat;*.cmd;*.ps1;*.com")
        assert ("Todos los archivos", "*.*") in kw["filetypes"]  # y siempre se puede elegir otro tipo

    def test_any_file_mode_has_no_executable_filter(self, client, dialogs):
        """Para adjuntar/leer un archivo cualquiera (CSV, PDF...): sin el filtro de ejecutables que ocultaria casi todo."""
        client.get("/api/fs/native-picker?mode=any-file")

        kind, kw = dialogs.calls[0]
        assert kind == "file" and "adjuntar" in kw["title"]
        assert kw["filetypes"] == [("Todos los archivos", "*.*")]

    def test_an_unknown_mode_falls_back_to_a_folder(self, client, dialogs):
        client.get("/api/fs/native-picker?mode=cualquier-cosa")

        assert dialogs.calls[0][0] == "directory"

    def test_the_initial_folder_is_passed_through(self, client, dialogs):
        client.get("/api/fs/native-picker", params={"initial_path": "C:/Proyectos"})

        assert dialogs.calls[0][1]["initialdir"] == "C:/Proyectos"

    @pytest.mark.parametrize("mode", ["folder", "file", "any-file"])
    def test_without_an_initial_folder_it_starts_in_the_users_home(self, client, dialogs, mode):
        client.get(f"/api/fs/native-picker?mode={mode}")

        assert dialogs.calls[0][1]["initialdir"] == str(Path.home())


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------
class TestResult:
    def test_cancelling_gives_an_empty_path_not_an_error(self, client, dialogs):
        dialogs.answer = ""

        r = client.get("/api/fs/native-picker")

        assert r.status_code == 200 and r.json() == {"path": ""}

    def test_a_missing_answer_is_also_an_empty_path(self, client, dialogs):
        dialogs.answer = None

        assert client.get("/api/fs/native-picker").json() == {"path": ""}

    def test_the_window_is_always_destroyed(self, client, dialogs):
        client.get("/api/fs/native-picker")

        assert dialogs.tk_created == 1 and dialogs.tk_destroyed == 1

    def test_the_window_is_destroyed_even_if_the_dialog_blows_up(self, client, dialogs):
        dialogs.raises = RuntimeError("el dialogo fallo")

        client.get("/api/fs/native-picker")

        assert dialogs.tk_destroyed == 1  # sin esto quedaria una ventana Tk huerfana


# ---------------------------------------------------------------------------
# El lock: un solo selector a la vez
# ---------------------------------------------------------------------------
class TestSingleInstance:
    def test_the_lock_is_free_again_after_a_normal_pick(self, client, dialogs):
        client.get("/api/fs/native-picker")

        assert main._native_picker_lock.locked() is False

    def test_the_lock_is_free_after_cancelling(self, client, dialogs):
        dialogs.answer = ""
        client.get("/api/fs/native-picker")

        assert main._native_picker_lock.locked() is False

    def test_the_lock_is_free_after_the_dialog_fails(self, client, dialogs):
        """Si quedara tomado, el explorador no abriria nunca mas hasta reiniciar el backend (el bug original)."""
        dialogs.raises = RuntimeError("falla")
        client.get("/api/fs/native-picker")

        assert main._native_picker_lock.locked() is False

    def test_the_lock_is_free_after_tk_itself_cannot_start(self, client, dialogs):
        dialogs.raises_on_tk = True
        client.get("/api/fs/native-picker")

        assert main._native_picker_lock.locked() is False

    def test_a_second_request_while_one_is_open_is_refused_with_a_409(self, client, dialogs):
        dialogs.gate = threading.Event()
        first = {}

        def open_first():
            first["r"] = TestClient(main.app, raise_server_exceptions=False).get("/api/fs/native-picker")

        t = threading.Thread(target=open_first)
        t.start()
        assert dialogs.opened.wait(timeout=10)  # el primer dialogo esta abierto

        second = client.get("/api/fs/native-picker")

        assert second.status_code == 409
        assert "Ya hay un selector" in second.json()["detail"] and "Alt+Tab" in second.json()["detail"]
        assert dialogs.tk_created == 1  # el segundo NO llego a crear un Tk (dos Tk a la vez corrompen Tcl)
        dialogs.gate.set()
        t.join(timeout=10)
        assert first["r"].status_code == 200 and main._native_picker_lock.locked() is False

    def test_a_refused_request_does_not_release_the_lock_of_the_first(self, client, dialogs):
        dialogs.gate = threading.Event()

        t = threading.Thread(
            target=lambda: TestClient(main.app, raise_server_exceptions=False).get("/api/fs/native-picker")
        )
        t.start()
        assert dialogs.opened.wait(timeout=10)
        client.get("/api/fs/native-picker")  # rechazado con 409

        assert main._native_picker_lock.locked() is True  # el primero sigue siendo el dueño
        dialogs.gate.set()
        t.join(timeout=10)


class TestPatience:
    def test_the_user_has_thirty_minutes_to_choose(self, client, dialogs, monkeypatch):
        """300 s era corto para elegir una carpeta con calma (y al vencer dejaba un dialogo huerfano). Se comprueba el plazo que recibe `wait_for`, sin esperar de verdad."""
        seen = {}
        real_wait_for = main.asyncio.wait_for

        async def spy(awaitable, timeout):
            seen["timeout"] = timeout
            return await real_wait_for(awaitable, timeout)

        monkeypatch.setattr(main.asyncio, "wait_for", spy)

        client.get("/api/fs/native-picker")

        assert seen["timeout"] == 1800


# ---------------------------------------------------------------------------
# Entornos sin pantalla
# ---------------------------------------------------------------------------
class TestNoDisplay:
    def test_tk_failing_gives_a_clear_500_without_the_technical_detail(self, client, dialogs):
        dialogs.raises_on_tk = True

        r = client.get("/api/fs/native-picker")

        assert r.status_code == 500
        assert r.json()["detail"] == "No se pudo abrir el explorador nativo de Windows en este entorno."
        assert "DISPLAY" not in r.text

    def test_a_dialog_error_is_also_a_clear_500(self, client, dialogs):
        dialogs.raises = RuntimeError("falla del dialogo")

        r = client.get("/api/fs/native-picker")

        assert r.status_code == 500 and "No se pudo abrir el explorador nativo" in r.json()["detail"]


# ---------------------------------------------------------------------------
# _force_foreground_window (la funcion real, con Win32 falso)
# ---------------------------------------------------------------------------
class FakeUser32:
    def __init__(self, parent=1234):
        self.parent = parent
        self.events: list[tuple] = []

    def GetParent(self, wid):
        self.events.append(("GetParent", wid))
        return self.parent

    def keybd_event(self, vk, scan, flags, extra):
        self.events.append(("key", vk, flags))

    def SetForegroundWindow(self, hwnd):
        self.events.append(("foreground", hwnd))


class TestForceForeground:
    @pytest.fixture
    def win(self, monkeypatch):
        user32 = FakeUser32()
        windll = types.SimpleNamespace(user32=user32)
        monkeypatch.setattr("ctypes.windll", windll, raising=False)
        return user32

    @pytest.fixture(autouse=True)
    def real_function(self, monkeypatch):
        # El fixture `dialogs` reemplaza la funcion en `main`; estos tests quieren la REAL (guardada en `REAL`).
        monkeypatch.setattr(main, "_force_foreground_window", REAL_FORCE_FOREGROUND)

    def test_it_presses_and_releases_alt_before_asking_for_the_foreground(self, win):
        """Pulsar ALT hace que Windows trate el cambio de ventana como iniciado por la persona y no lo bloquee (LockSetForegroundWindow)."""
        main._force_foreground_window(types.SimpleNamespace(winfo_id=lambda: 99))

        assert win.events == [("GetParent", 99), ("key", 0x12, 0), ("key", 0x12, 0x0002), ("foreground", 1234)]

    def test_without_a_parent_window_it_does_nothing(self, win):
        win.parent = 0

        main._force_foreground_window(types.SimpleNamespace(winfo_id=lambda: 99))

        assert win.events == [("GetParent", 99)]  # ni teclas ni foreground

    def test_a_win32_failure_never_breaks_the_picker(self, win, monkeypatch):
        def boom(*a):
            raise OSError("win32 fallo")

        win.SetForegroundWindow = boom

        main._force_foreground_window(types.SimpleNamespace(winfo_id=lambda: 99))  # no lanza

    def test_outside_windows_it_does_nothing(self, win, monkeypatch):
        monkeypatch.setattr("os.name", "posix")

        main._force_foreground_window(types.SimpleNamespace(winfo_id=lambda: 99))

        assert win.events == []


REAL_FORCE_FOREGROUND = main._force_foreground_window
