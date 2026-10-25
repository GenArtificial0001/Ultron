"""BL-008: la ventana de alarma/agente (`app/notifier.py`), lo UNICO que avisa de una alarma si Vision no esta en primer plano.

Nunca se abre una ventana real ni se reproduce un sonido: `tkinter`'s widgets y `winsound` se reemplazan por dobles que registran lo que se les pide.
El Tk falso devuelve numeros reales en `winfo_screenwidth/height` (un `None` rompe la cuenta de la posicion de la ventana) y guarda el `command=` de cada boton
para poder "apretarlo". Sin hilos de verdad: `start_notifier` se prueba con un `Thread` falso."""

from __future__ import annotations

import logging
import time

import pytest

from app import notifier as nt


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.command = kwargs.get("command")
        self.destroyed = False
        self.cancelled: list = []
        self.scheduled: list = []
        self.protocols: dict = {}  # nombre de protocolo -> callback (WM_DELETE_WINDOW)

    def protocol(self, name, fn=None):
        self.protocols[name] = fn

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def after(self, ms, fn=None, *args):
        self.scheduled.append((ms, fn))
        return f"after-{len(self.scheduled)}"

    def after_cancel(self, ident):
        self.cancelled.append(ident)

    def destroy(self):
        self.destroyed = True

    def __getattr__(self, name):  # pack, place, title, attributes, configure, geometry, protocol...
        return lambda *a, **k: None


class FakeTk:
    def __init__(self):
        self.buttons: list[FakeWidget] = []
        self.windows: list[FakeWidget] = []
        self.labels: list[FakeWidget] = []

    def Toplevel(self, root):
        w = FakeWidget()
        self.windows.append(w)
        return w

    def Frame(self, *a, **k):
        return FakeWidget(**k)

    def Label(self, *a, **k):
        w = FakeWidget(**k)
        self.labels.append(w)
        return w

    def Button(self, *a, **k):
        b = FakeWidget(*a, **k)
        self.buttons.append(b)
        return b


class FakeWinsound:
    SND_PURGE, SND_FILENAME, SND_ASYNC = 1, 2, 4

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail = False

    def PlaySound(self, *args):
        if self.fail:
            raise RuntimeError("sin dispositivo de audio")
        self.calls.append(args)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    # El logo real llama a `ImageTk.PhotoImage`, que necesita una raiz de Tk de verdad: se evita apuntando a un PNG que no existe
    # (la alerta lo tolera y sigue; ver `test_a_missing_logo_...`).
    monkeypatch.setattr(nt, "_LOGO_PNG", str(tmp_path / "sin-logo.png"))
    nt._snoozed.clear()
    while not nt._requests.empty():
        nt._requests.get_nowait()
    monkeypatch.setattr(nt, "_open_windows_offset", 0)
    yield
    nt._snoozed.clear()


@pytest.fixture
def ws(monkeypatch):
    fake = FakeWinsound()
    monkeypatch.setattr(nt, "winsound", fake)
    return fake


def request(kind="alarm", item="zcc", minutes=10, label="ZCC", wav="x.wav"):
    return nt._AlertRequest(kind, item, label, wav, minutes, "08:30")


def show(tk: FakeTk, **kw):
    nt._make_window(object(), tk, request(**kw))
    snooze = next(b for b in tk.buttons if "Posponer" in b.kwargs.get("text", ""))
    close = next(b for b in tk.buttons if b.kwargs.get("text") == "✕")
    return snooze, close


# ---------------------------------------------------------------------------------------------------------------------
# A) Logica pura: encolar y posponer
# ---------------------------------------------------------------------------------------------------------------------
class TestShowAlert:
    def test_it_queues_a_request_with_the_current_time(self, monkeypatch):
        monkeypatch.setattr(nt.time, "strftime", lambda fmt: "14:05")

        nt.show_alert("agent", "a1", "Reporte", "beep.wav", 7)

        req = nt._requests.get_nowait()
        assert (req.kind, req.item_id, req.label, req.wav_path, req.snooze_minutes, req.fired_at) == (
            "agent",
            "a1",
            "Reporte",
            "beep.wav",
            7,
            "14:05",
        )

    def test_requests_come_out_in_arrival_order(self):
        for i in range(3):
            nt.show_alert("alarm", f"i{i}", f"L{i}", "w.wav", 5)

        assert [nt._requests.get_nowait().item_id for _ in range(3)] == ["i0", "i1", "i2"]

    def test_it_can_be_called_from_another_thread(self):
        import threading

        t = threading.Thread(target=lambda: nt.show_alert("alarm", "x", "L", "w.wav", 5))
        t.start()
        t.join()

        assert nt._requests.get_nowait().item_id == "x"


class TestPopDueSnoozes:
    def test_a_due_snooze_is_returned_once_and_removed(self):
        nt._snoozed[("alarm", "x")] = 100.0

        assert nt.pop_due_snoozes("alarm", 200.0) == ["x"]
        assert nt.pop_due_snoozes("alarm", 9e12) == [] and ("alarm", "x") not in nt._snoozed

    def test_a_future_snooze_is_kept(self):
        nt._snoozed[("alarm", "x")] = 500.0

        assert nt.pop_due_snoozes("alarm", 499.9) == [] and ("alarm", "x") in nt._snoozed

    def test_the_exact_boundary_counts_as_due(self):
        nt._snoozed[("alarm", "x")] = 500.0
        assert nt.pop_due_snoozes("alarm", 500.0) == ["x"]

    def test_agents_and_alarms_do_not_consume_each_others_snoozes(self):
        nt._snoozed[("agent", "x")] = 100.0
        nt._snoozed[("alarm", "x")] = 100.0

        assert nt.pop_due_snoozes("agent", 200.0) == ["x"]
        assert ("alarm", "x") in nt._snoozed, "el reloj de agentes no debe llevarse la alarma del mismo id"

    def test_several_due_items_are_all_returned(self):
        for i in range(3):
            nt._snoozed[("alarm", f"i{i}")] = 10.0 + i
        nt._snoozed[("alarm", "tarde")] = 9e12

        assert sorted(nt.pop_due_snoozes("alarm", 100.0)) == ["i0", "i1", "i2"] and list(nt._snoozed) == [
            ("alarm", "tarde")
        ]

    def test_nothing_pending_gives_an_empty_list(self):
        assert nt.pop_due_snoozes("alarm", 1.0) == []

    def test_concurrent_pops_never_raise_when_two_threads_race_for_the_same_keys(self):
        """Sin el lock, dos hilos (el reloj de agentes y el de alarmas) que recorren y borran las mismas claves se pisan: el 2o hace `del` de una clave que
        el 1o ya borro y lanza KeyError. Con un intervalo de cambio de hilo diminuto la carrera se vuelve reproducible en vez de depender de la suerte."""
        import sys
        import threading

        errors: list[str] = []
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            def worker():
                for _ in range(150):
                    for i in range(40):
                        nt._snoozed[("alarm", f"i{i}")] = 1.0
                    try:
                        nt.pop_due_snoozes("alarm", 10.0)
                    except Exception as exc:  # noqa: BLE001 -- se cuenta, el assert de abajo decide
                        errors.append(type(exc).__name__)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        finally:
            sys.setswitchinterval(old)

        assert errors == [], f"carrera sin proteger: {sorted(set(errors))}"

    def test_concurrent_pops_never_return_the_same_item_twice(self):
        import threading

        for i in range(200):
            nt._snoozed[("alarm", f"i{i}")] = 1.0
        out: list[list[str]] = []
        lock = threading.Lock()

        def worker():
            got = nt.pop_due_snoozes("alarm", 10.0)
            with lock:
                out.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        flat = [x for g in out for x in g]
        assert len(flat) == 200 and len(set(flat)) == 200, "cada posponer vencido se entrega exactamente una vez"


# ---------------------------------------------------------------------------------------------------------------------
# B) La ventana: botones, sonido y animacion (Tk y winsound falsos)
# ---------------------------------------------------------------------------------------------------------------------
class TestWindowButtons:
    def test_snooze_registers_the_return_time_and_closes_the_window(self, ws):
        tk = FakeTk()
        snooze, _ = show(tk, minutes=10)
        before = time.time()

        snooze.command()

        due = nt._snoozed[("alarm", "zcc")]
        assert before + 10 * 60 <= due <= time.time() + 10 * 60
        assert tk.windows[0].destroyed

    @pytest.mark.parametrize(
        "minutes,expected", [(0, 1), (-5, 1), (1, 1), (2, 2), (59, 59), (60, 60), (61, 60), (999, 60)]
    )
    def test_snooze_minutes_are_clamped_between_1_and_60(self, ws, minutes, expected):
        snooze, _ = show(FakeTk(), kind="agent", item="a", minutes=minutes)
        before = time.time()

        snooze.command()

        assert (nt._snoozed[("agent", "a")] - before) / 60 == pytest.approx(expected, abs=0.05)

    def test_close_does_not_register_a_snooze_and_closes_the_window(self, ws):
        tk = FakeTk()
        _, close = show(tk)

        close.command()

        assert nt._snoozed == {} and tk.windows[0].destroyed

    def test_the_snooze_button_shows_the_configured_minutes(self, ws):
        tk = FakeTk()
        show(tk, minutes=25)
        assert any("Posponer 25 min" in b.kwargs.get("text", "") for b in tk.buttons)

    def test_snoozing_again_replaces_the_previous_return_time(self, ws):
        snooze, _ = show(FakeTk(), minutes=5)
        snooze.command()
        first = nt._snoozed[("alarm", "zcc")]
        snooze2, _ = show(FakeTk(), minutes=30)
        snooze2.command()

        assert len([k for k in nt._snoozed if k == ("alarm", "zcc")]) == 1 and nt._snoozed[("alarm", "zcc")] > first

    def test_two_items_snoozed_together_are_tracked_independently(self, ws):
        a, _ = show(FakeTk(), item="uno", minutes=5)
        b, _ = show(FakeTk(), item="dos", minutes=15)
        a.command()
        b.command()

        assert (
            set(nt._snoozed) == {("alarm", "uno"), ("alarm", "dos")}
            and nt._snoozed[("alarm", "dos")] > nt._snoozed[("alarm", "uno")]
        )

    def test_the_full_cycle_snooze_then_it_rings_again_then_close_ends_cleanly(self, ws):
        """Posponer -> vence -> el reloj lo re-dispara -> el usuario cierra con la X: no queda nada pendiente."""
        snooze, _ = show(FakeTk())
        snooze.command()
        due = nt._snoozed[("alarm", "zcc")]
        assert nt.pop_due_snoozes("alarm", due + 1) == ["zcc"]
        _, close = show(FakeTk())

        close.command()

        assert nt._snoozed == {}

    def test_close_cancels_a_snooze_that_is_already_pending_for_the_same_item(self, ws):
        """PQ-006 (opcion (a), delegada en ADR-018): si lo cerraste a mano con la X, no esperas que reaparezca. Antes la X solo cerraba la
        ventana: el posponer del disparo anterior seguia vivo y la alarma volvia a sonar aunque ya la hubieras descartado."""
        snooze, _ = show(FakeTk())
        snooze.command()
        assert ("alarm", "zcc") in nt._snoozed
        _, close = show(FakeTk())  # otro disparo del mismo item, antes de que venza el posponer

        close.command()

        assert ("alarm", "zcc") not in nt._snoozed

    def test_closing_one_item_does_not_cancel_the_snooze_of_another(self, ws):
        """La X cancela el posponer de ESE item, no el de los demas ni el de otro tipo de reloj con el mismo id."""
        nt._snoozed[("alarm", "otra")] = 123.0
        nt._snoozed[("agent", "zcc")] = 456.0
        _, close = show(FakeTk())  # alarm / zcc

        close.command()

        assert nt._snoozed == {("alarm", "otra"): 123.0, ("agent", "zcc"): 456.0}

    def test_closing_with_the_window_manager_also_cancels_the_pending_snooze(self, ws):
        """El cierre por Alt+F4 / WM_DELETE_WINDOW usa el mismo camino que la X."""
        snooze, _ = show(FakeTk())
        snooze.command()
        tk = FakeTk()
        show(tk)

        tk.windows[-1].protocols["WM_DELETE_WINDOW"]()

        assert ("alarm", "zcc") not in nt._snoozed

    def test_closing_an_agent_cancels_its_own_snooze_not_the_alarm_with_the_same_id(self, ws):
        """El agente y la alarma tienen relojes distintos aunque compartan id: la X de uno no toca el posponer del otro."""
        nt._snoozed[("agent", "zcc")] = 111.0
        nt._snoozed[("alarm", "zcc")] = 222.0
        _, close = show(FakeTk(), kind="agent")

        close.command()

        assert nt._snoozed == {("alarm", "zcc"): 222.0}

    def test_snoozing_again_after_closing_works(self, ws):
        """La X no deja el item 'bloqueado': un posponer posterior se registra normalmente."""
        _, close = show(FakeTk())
        close.command()
        snooze, _ = show(FakeTk())

        snooze.command()

        assert ("alarm", "zcc") in nt._snoozed


class TestWindowSoundAndAnimation:
    def test_the_alert_starts_playing_the_wav_asynchronously(self, ws):
        show(FakeTk(), wav="alarma.wav")

        assert ws.calls[0] == ("alarma.wav", nt.winsound.SND_FILENAME | nt.winsound.SND_ASYNC)

    def test_the_sound_loop_reschedules_itself_at_the_configured_interval(self, ws):
        tk = FakeTk()
        show(tk)

        scheduled = [(ms, fn) for w in tk.windows for (ms, fn) in getattr(w, "scheduled", [])]
        assert nt.SOUND_LOOP_INTERVAL_MS in [ms for ms, _ in scheduled]

    def test_running_the_loop_again_replays_the_sound(self, ws):
        tk = FakeTk()
        show(tk, wav="a.wav")
        loop = next(fn for ms, fn in tk.windows[0].scheduled if ms == nt.SOUND_LOOP_INTERVAL_MS)

        loop()

        assert [c[0] for c in ws.calls].count("a.wav") == 2

    def test_a_failing_sound_device_is_logged_and_the_loop_keeps_going(self, ws, caplog):
        tk = FakeTk()
        show(tk)
        ws.fail = True
        loop = next(fn for ms, fn in tk.windows[0].scheduled if ms == nt.SOUND_LOOP_INTERVAL_MS)

        with caplog.at_level(logging.WARNING, logger="app.notifier"):
            loop()

        assert any("No se pudo repetir el sonido" in r.getMessage() for r in caplog.records)
        assert len([1 for ms, _ in tk.windows[0].scheduled if ms == nt.SOUND_LOOP_INTERVAL_MS]) >= 2, (
            "sigue reprogramandose"
        )

    @pytest.mark.parametrize("button", ["snooze", "close"])
    def test_either_button_stops_the_sound_and_both_timers(self, ws, button):
        tk = FakeTk()
        snooze, close = show(tk)

        {"snooze": snooze, "close": close}[button].command()

        assert any(c == (None, nt.winsound.SND_PURGE) for c in ws.calls), "se purga el sonido en curso"
        assert len(tk.windows[0].cancelled) == 2, "se cancelan el bucle de sonido y la animacion de la campana"

    def test_a_failing_purge_does_not_stop_the_window_from_closing(self, ws):
        tk = FakeTk()
        _, close = show(tk)
        ws.fail = True

        close.command()  # PlaySound(None, SND_PURGE) lanza: la ventana igual se cierra

        assert tk.windows[0].destroyed

    def test_a_failing_after_cancel_does_not_stop_the_window_from_closing(self, ws, monkeypatch):
        tk = FakeTk()
        snooze, _ = show(tk)

        def boom(ident):
            raise RuntimeError("tcl: id invalido")

        tk.windows[0].after_cancel = boom
        snooze.command()

        assert tk.windows[0].destroyed and ("alarm", "zcc") in nt._snoozed, "aun asi el posponer queda registrado"

    def test_the_bell_animation_reschedules_every_70_ms(self, ws):
        tk = FakeTk()
        show(tk)

        assert 70 in [ms for ms, _ in tk.windows[0].scheduled]

    def test_the_bell_swings_inside_a_6_pixel_range(self, ws):
        tk = FakeTk()
        show(tk)
        bell = tk.labels[0]
        placed: list[int] = []
        bell.place = lambda **k: placed.append(k.get("x", 0))
        animate = next(fn for ms, fn in tk.windows[0].scheduled if ms == 70)

        for _ in range(48):
            animate()

        assert max(placed) <= 6 and min(placed) >= -6 and max(placed) > 0 > min(placed)


class TestWindowContent:
    def test_an_alarm_shows_the_clock_icon_and_an_agent_the_robot(self, ws):
        alarm, agent = FakeTk(), FakeTk()
        show(alarm, kind="alarm", label="Despertar")
        show(agent, kind="agent", item="a", label="Reporte")

        assert any(l.kwargs.get("text", "").startswith("⏰") and "Despertar" in l.kwargs["text"] for l in alarm.labels)
        assert any(l.kwargs.get("text", "").startswith("🤖") and "Reporte" in l.kwargs["text"] for l in agent.labels)
        assert any(l.kwargs.get("text") == "Alarma y recordatorio" for l in alarm.labels)
        assert any(l.kwargs.get("text") == "Agente programado" for l in agent.labels)

    def test_the_trigger_time_is_shown(self, ws):
        tk = FakeTk()
        show(tk)
        assert any(l.kwargs.get("text") == "08:30" for l in tk.labels)

    def test_consecutive_windows_are_staggered_and_wrap_after_six(self, ws, monkeypatch):
        geoms: list[str] = []

        class Geo(FakeWidget):
            def geometry(self, spec):
                geoms.append(spec)

        class GeoTk(FakeTk):
            def Toplevel(self, root):
                w = Geo()
                self.windows.append(w)
                return w

        for _ in range(7):
            show(GeoTk())

        ys = [int(g.split("+")[-1]) for g in geoms]
        assert len(set(ys[:6])) == 6 and all(ys[i] - ys[i + 1] == 36 for i in range(5)), (
            "cada ventana nueva sube 36 px para no tapar a la anterior"
        )
        assert ys[6] == ys[0], "la septima vuelve a la posicion inicial"

    def test_the_window_never_starts_off_screen(self, ws):
        geoms: list[str] = []

        class Tiny(FakeWidget):
            def winfo_screenwidth(self):
                return 100

            def winfo_screenheight(self):
                return 100

            def geometry(self, spec):
                geoms.append(spec)

        class TinyTk(FakeTk):
            def Toplevel(self, root):
                return Tiny()

        show(TinyTk())

        x, y = (int(v) for v in geoms[0].split("+")[1:])
        assert x >= 0 and y >= 0

    def test_a_missing_logo_is_logged_and_the_alert_still_appears(self, ws, caplog, monkeypatch):
        monkeypatch.setattr(nt, "_LOGO_PNG", "C:/no/existe/logo.png")
        tk = FakeTk()

        with caplog.at_level(logging.WARNING, logger="app.notifier"):
            show(tk)

        assert tk.buttons and tk.windows, "la alerta se muestra igual"


# ---------------------------------------------------------------------------------------------------------------------
# C) El hilo de Tk
# ---------------------------------------------------------------------------------------------------------------------
class TestStartNotifier:
    def test_it_starts_the_thread_once(self, monkeypatch):
        started: list[dict] = []

        class FakeThread:
            def __init__(self, target, name, daemon):
                started.append({"target": target, "name": name, "daemon": daemon})

            def start(self):
                started[-1]["started"] = True

        monkeypatch.setattr(nt, "_started", False)
        monkeypatch.setattr(nt.threading, "Thread", FakeThread)

        nt.start_notifier()
        nt.start_notifier()
        nt.start_notifier()

        assert len(started) == 1 and started[0]["started"] and started[0]["daemon"] is True
        assert started[0]["target"] is nt._tk_thread_main and started[0]["name"] == "vision-notifier"

    def test_the_daemon_flag_lets_vision_exit_even_if_an_alert_is_open(self, monkeypatch):
        flags: list[bool] = []
        monkeypatch.setattr(nt, "_started", False)
        monkeypatch.setattr(
            nt.threading,
            "Thread",
            lambda target, name, daemon: flags.append(daemon) or type("T", (), {"start": lambda self: None})(),
        )

        nt.start_notifier()

        assert flags == [True]


class TestTkThreadMain:
    def _run(self, monkeypatch, requests, make_window):
        """Ejecuta `_tk_thread_main` con un `tkinter` falso cuyo mainloop corre UN ciclo de `_poll` y sale."""
        import sys
        import types

        class Root:
            def __init__(self):
                self.polls: list = []
                self.hidden = False

            def withdraw(self):
                self.hidden = True

            def after(self, ms, fn):
                self.polls.append((ms, fn))

            def mainloop(self):
                ms, poll = self.polls[0]
                poll()

        roots: list[Root] = []
        fake_tk = types.SimpleNamespace(Tk=lambda: roots.append(Root()) or roots[-1])
        monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
        monkeypatch.setattr(nt, "_make_window", make_window)
        for r in requests:
            nt._requests.put(r)

        nt._tk_thread_main()
        return roots[0]

    def test_the_root_is_hidden_and_polling_is_scheduled(self, monkeypatch):
        root = self._run(monkeypatch, [], lambda *a: None)

        assert root.hidden is True
        assert [ms for ms, _ in root.polls] == [150, 150], "se programa el 1o y cada poll reprograma el siguiente"

    def test_every_queued_request_becomes_a_window(self, monkeypatch):
        shown: list[str] = []

        self._run(
            monkeypatch,
            [request(item="a"), request(item="b"), request(item="c")],
            lambda root, tk, req: shown.append(req.item_id),
        )

        assert shown == ["a", "b", "c"] and nt._requests.empty()

    def test_one_broken_window_is_logged_and_the_rest_still_appear(self, monkeypatch, caplog):
        shown: list[str] = []

        def make(root, tk, req):
            if req.item_id == "roto":
                raise RuntimeError("tcl fallo")
            shown.append(req.item_id)

        with caplog.at_level(logging.WARNING, logger="app.notifier"):
            self._run(monkeypatch, [request(item="a"), request(item="roto"), request(item="c")], make)

        assert shown == ["a", "c"]
        assert any("No se pudo mostrar la alerta" in r.getMessage() for r in caplog.records)
