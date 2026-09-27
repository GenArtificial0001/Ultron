"""
Alerta visual nativa persistente para agentes/alarmas (pedido del usuario
2026-09-17: "que suene hasta que presione el boton X, y si presiono Zzz
vuelve a sonar segun el tiempo de recurrencia"). Ni el toast de Windows
(`win11toast`, ver app/scheduler/alarms.py) ni una notificacion del
navegador se pueden dejar sonando/visibles indefinidamente hasta un click
del usuario -- son fire-and-forget. Hace falta una ventana propia.

Arquitectura (mismo criterio "backend, no navegador" que el resto de
agentes/alarmas -- tiene que verse aunque Vision este cerrado en el
navegador):
  - Un solo hilo de fondo corre el mainloop de Tkinter (viene con Python en
    Windows, cero dependencia nueva) -- Tkinter SOLO se puede tocar desde
    ESE hilo.
  - `show_alert()` la llama el asyncio del backend (OTRO hilo) -- por eso
    solo encola el pedido en `_requests` (queue.Queue, thread-safe); el
    hilo de Tk lo saca de ahi solo, vía polling con `root.after()`.
  - El boton "Zzz" (posponer), al click (YA en el hilo de Tk), guarda
    `hora_de_reaparecer` en `_snoozed` (dict protegido con Lock, no hace
    falta Tkinter para tocarlo). El reloj de agentes/alarmas (20s de tick)
    llama `pop_due_snoozes(kind, now)` para saber si hay que re-disparar.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import winsound
from dataclasses import dataclass
from typing import Any

from app.config import RESOURCE_ROOT

logger = logging.getLogger(__name__)

SOUND_LOOP_INTERVAL_MS = 4500  # cada cuanto se repite el WAV mientras la alerta sigue abierta

# Paleta pedida por el usuario 2026-09-17: fondo azul solido + texto blanco
# (nada de acento por tema aca -- es una ventana nativa aparte, no el chat).
# Pedido explicito de contraste: los botones NO pueden ser un azul mas
# oscuro sobre el fondo azul (se "perdian") -- blanco solido con texto azul,
# el maximo contraste posible contra el fondo.
BG = "#0000FF"
FG = "#FFFFFF"
BTN_BG = "#FFFFFF"
BTN_FG = "#0000FF"
BTN_ACTIVE_BG = "#FFD400"
BTN_CLOSE_BG = "#E53935"  # pedido del usuario: la X tiene que ser roja
BTN_CLOSE_ACTIVE_BG = "#FF6F60"

# Logo de la empresa (pedido del usuario 2026-09-17: se probo primero el caballito
# de rojo animado -- rotacion+rebote calibrados contra un video real de
# referencia -- pero el usuario decidio volver a algo mas simple: el logo
# fijo, sin animacion).
_LOGO_PNG = str(RESOURCE_ROOT / "frontend" / "assets" / "logo.png")


@dataclass
class _AlertRequest:
    kind: str  # "agent" | "alarm" -- distingue a que reloj le corresponde el snooze
    item_id: str
    label: str
    wav_path: str
    snooze_minutes: int
    fired_at: str  # "HH:MM", pedido del usuario: la hora en negrita (no cursiva)


_requests: queue.Queue[_AlertRequest] = queue.Queue()
_snoozed: dict[tuple[str, str], float] = {}
_snoozed_lock = threading.Lock()
_started = False
_open_windows_offset = 0  # para no apilar todas las ventanas nuevas exactamente en el mismo punto


def show_alert(kind: str, item_id: str, label: str, wav_path: str, snooze_minutes: int) -> None:
    """Thread-safe: encola un pedido de alerta. Se puede llamar desde
    cualquier hilo (el asyncio del backend, en particular)."""
    fired_at = time.strftime("%H:%M")
    _requests.put(_AlertRequest(kind, item_id, label, wav_path, snooze_minutes, fired_at))


def pop_due_snoozes(kind: str, now: float) -> list[str]:
    """Devuelve los item_id de `kind` cuyo tiempo de posponer ya se cumplio
    -- y los saca de la lista de pendientes (no se vuelven a devolver)."""
    due: list[str] = []
    with _snoozed_lock:
        for key, fire_at in list(_snoozed.items()):
            k, item_id = key
            if k == kind and fire_at <= now:
                due.append(item_id)
                del _snoozed[key]
    return due


def _make_window(root: Any, tk: Any, req: _AlertRequest) -> None:
    global _open_windows_offset

    win = tk.Toplevel(root)
    win.title("Vision")
    win.attributes("-topmost", True)
    win.resizable(False, False)
    # Pedido del usuario: nada de barra de titulo/cerrar/minimizar del SO --
    # tiene que verse como un popup propio, con SOLO los botones de abajo.
    win.overrideredirect(True)
    win.configure(bg=BG, highlightbackground="#8f8fff", highlightthickness=1)

    width, height = 380, 220
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    offset = (_open_windows_offset % 6) * 36
    _open_windows_offset += 1
    x = screen_w - width - 24
    y = screen_h - height - 60 - offset
    win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=14, pady=(14, 0))

    # Campanita "sonando" -- Tkinter no rota texto/canvas sin Pillow (no se
    # suma esa dependencia solo por esto), asi que se simula el repique
    # oscilando la posicion horizontal del emoji con una sinusoide (ver
    # _animate_bell mas abajo). Frame propio de tamaño fijo con place():
    # mezclar pack/place en el MISMO padre rompe el calculo de pack, por
    # eso el bamboleo vive adentro de su propio contenedor chico.
    bell_frame = tk.Frame(header, bg=BG, width=34, height=34)
    bell_frame.pack(side="left")
    bell_frame.pack_propagate(False)
    bell_label = tk.Label(bell_frame, text="🔔", font=("Segoe UI Emoji", 20), bg=BG, fg=FG)
    bell_label.place(relx=0.5, rely=0.5, anchor="center")

    # Logo de la empresa, chico, arriba a la derecha -- fijo, sin animacion
    # (pedido del usuario: sacar el caballito animado y volver a esto).
    # Bug real reportado ("se ve muy pixelado"): tk.PhotoImage.subsample()
    # solo puede achicar en factores ENTEROS y lo hace por nearest-neighbor
    # (descarta pixeles, no promedia) -- para un logo con curvas se nota
    # mucho. Pillow (LANCZOS, mismo criterio que se uso para el emblema de
    # rojo antes) escala con antialiasing de verdad.
    try:
        from PIL import Image, ImageTk

        logo_img_src = Image.open(_LOGO_PNG).convert("RGBA")
        target_h = 30
        ratio = target_h / logo_img_src.height
        logo_img_src = logo_img_src.resize(
            (max(1, round(logo_img_src.width * ratio)), target_h), Image.LANCZOS
        )
        logo_img = ImageTk.PhotoImage(logo_img_src)
        logo_label = tk.Label(header, image=logo_img, bg=BG)
        logo_label.image = logo_img  # referencia dura -- sin esto el GC se lo come
        logo_label.pack(side="right")
    except Exception:  # noqa: BLE001 -- el logo es cosmetico, nunca tira la alerta abajo
        logger.warning("No se pudo cargar el logo de Vision en la alerta", exc_info=True)

    icon = "⏰" if req.kind == "alarm" else "🤖"
    tk.Label(
        win, text=f"{icon}  {req.label}", font=("Segoe UI", 13, "italic"),
        bg=BG, fg=FG, wraplength=340, justify="left",
    ).pack(padx=14, pady=(6, 2), anchor="w")
    tk.Label(
        win, text=req.fired_at, font=("Segoe UI", 15, "bold"),
        bg=BG, fg=FG,
    ).pack(padx=14, anchor="w")
    tk.Label(
        win, text="Alarma y recordatorio" if req.kind == "alarm" else "Agente programado",
        font=("Segoe UI", 9), bg=BG, fg=FG,
    ).pack(padx=14, pady=(2, 0), anchor="w")

    btn_row = tk.Frame(win, bg=BG)
    btn_row.pack(side="bottom", fill="x", padx=14, pady=14)

    after_id: list[str | None] = [None]
    bell_anim_id: list[str | None] = [None]
    bell_angle = [0]

    def _stop_sound() -> None:
        if after_id[0] is not None:
            try:
                win.after_cancel(after_id[0])
            except Exception:  # noqa: BLE001
                pass
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001
            pass

    def _loop_sound() -> None:
        try:
            winsound.PlaySound(req.wav_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:  # noqa: BLE001
            logger.warning("No se pudo repetir el sonido de la alerta '%s'", req.label, exc_info=True)
        after_id[0] = win.after(SOUND_LOOP_INTERVAL_MS, _loop_sound)

    def _animate_bell() -> None:
        bell_angle[0] = (bell_angle[0] + 1) % 24
        offset_x = round(6 * math.sin(bell_angle[0] / 24 * 2 * math.pi))
        bell_label.place(relx=0.5, rely=0.5, anchor="center", x=offset_x)
        bell_anim_id[0] = win.after(70, _animate_bell)

    def _stop_bell() -> None:
        if bell_anim_id[0] is not None:
            try:
                win.after_cancel(bell_anim_id[0])
            except Exception:  # noqa: BLE001
                pass

    def _dismiss() -> None:
        _stop_sound()
        _stop_bell()
        win.destroy()

    def _snooze() -> None:
        _stop_sound()
        _stop_bell()
        fire_at = time.time() + max(1, min(60, req.snooze_minutes)) * 60
        with _snoozed_lock:
            _snoozed[(req.kind, req.item_id)] = fire_at
        win.destroy()

    tk.Button(
        btn_row, text=f"😴 Posponer {req.snooze_minutes} min", command=_snooze,
        bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE_BG, activeforeground=BTN_FG,
        relief="flat", padx=10, pady=6, cursor="hand2", font=("Segoe UI", 9, "bold"),
    ).pack(side="left")
    tk.Button(
        btn_row, text="✕", command=_dismiss,
        bg=BTN_CLOSE_BG, fg=FG, activebackground=BTN_CLOSE_ACTIVE_BG, activeforeground=FG,
        relief="flat", width=3, cursor="hand2", font=("Segoe UI", 11, "bold"),
    ).pack(side="right")

    # sin barra de titulo (overrideredirect), asi que esto es la unica red
    # de contencion si algo dispara el cierre de otra forma (alt+f4, etc.)
    win.protocol("WM_DELETE_WINDOW", _dismiss)
    _loop_sound()
    _animate_bell()


def _tk_thread_main() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()  # la raiz no se muestra -- solo sostiene el mainloop, las alertas son Toplevel

    def _poll() -> None:
        try:
            while True:
                req = _requests.get_nowait()
                try:
                    _make_window(root, tk, req)
                except Exception:  # noqa: BLE001 -- una ventana rota no debe tirar el resto
                    logger.warning("No se pudo mostrar la alerta de '%s'", req.label, exc_info=True)
        except queue.Empty:
            pass
        root.after(150, _poll)

    root.after(150, _poll)
    root.mainloop()


def start_notifier() -> None:
    """Arranca el hilo de Tkinter una sola vez (llamado desde el startup de
    FastAPI). Idempotente -- un segundo llamado no hace nada."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_tk_thread_main, name="vision-notifier", daemon=True).start()
