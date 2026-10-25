"""Confirmacion de identidad con Windows Hello para desbloquear las credenciales (spec gestor-conexiones REQ-007b, paso B3).

Por que asi (prueba B0 del 2026-10-08, con el usuario mirando): pedir `UserConsentVerifier.RequestVerificationAsync` desde un proceso sin ventana (el servidor de Vision)
tenia dos fallas: la ventana de Windows Hello quedaba DETRAS de las demas y, aunque el usuario la confirmaba, el resultado no llegaba (la espera bloqueaba el hilo que
tenia que recibirlo). Lo que funciono, y es lo que hace este modulo:
1. `IUserConsentVerifierInterop::RequestVerificationForWindowAsync` (la API oficial para programas de escritorio) atada a la ventana que esta AL FRENTE en ese momento:
   la del navegador donde el usuario toco el ojo.
2. El resultado se consulta por SONDEO (`IAsyncInfo::get_Status`), sin depender de un bucle de mensajes.
3. Apenas aparece la ventana de Windows Hello (proceso `CredentialUIBroker.exe`) se la trae al frente; un toque de ALT habilita `SetForegroundWindow` desde un proceso en
   segundo plano (regla de «foreground lock» de Windows). Resultado medido: «Verified» en 5,6 s, con la ventana adelante de Chrome.

Todo por `ctypes`, sin dependencias nuevas. Solo Windows. Nunca lanza: cualquier falla es «no confirmado» (`verify` devuelve False) y el motivo va al log."""

from __future__ import annotations

import ctypes
import logging
import sys
import time
import uuid
from ctypes import wintypes

logger = logging.getLogger(__name__)

MESSAGE = "ULTRON: confirmá que sos vos para ver o cambiar tus contraseñas"
TIMEOUT_S = 90.0
_POLL_S = 0.2
_RESULTS = {0: "Verified", 1: "DeviceNotPresent", 2: "NotConfiguredForUser", 3: "DisabledByPolicy", 4: "DeviceBusy", 5: "RetriesExhausted", 6: "Canceled"}
_IID_INTEROP = "39E050C3-4E74-441A-8DC0-B81104DF949C"  # IUserConsentVerifierInterop
_IID_ASYNC_OP = "fd596ffd-2318-558f-9dbe-d21df43764a5"  # IAsyncOperation<UserConsentVerificationResult>: GUID de interfaz parametrizada, calculado y verificado
_IID_ASYNC_INFO = "00000036-0000-0000-C000-000000000046"  # IAsyncInfo
_BROKER = "credentialuibroker.exe"


def supported() -> bool:
    return sys.platform == "win32"


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> _GUID:
        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = u.bytes[8:]
        return g


def _vcall(ptr: ctypes.c_void_p, index: int, *argtypes):
    """Funcion `index` de la tabla virtual de un objeto COM (HRESULT de retorno)."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(ptr: ctypes.c_void_p) -> None:
    if ptr:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(ptr)


def _process_name(kernel32, pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value.rsplit(chr(92), 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def _bring_hello_to_front(user32, kernel32) -> bool:
    """Trae al frente la ventana visible de Windows Hello. True si la encontro."""
    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _process_name(kernel32, pid.value) == _BROKER:
                found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(collect), 0)
    for hwnd in found:
        user32.keybd_event(0x12, 0, 0, 0)  # ALT abajo
        user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT arriba
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0043)  # al frente de todo (SWP_NOMOVE|SWP_NOSIZE|SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0043)  # y deja de ser «siempre visible»
        user32.SetForegroundWindow(hwnd)
    return bool(found)


def request_verification(message: str = MESSAGE, timeout_s: float = TIMEOUT_S) -> str:
    """Pide la confirmacion y devuelve el resultado de Windows («Verified», «Canceled», «NotConfiguredForUser», ...) o «Timeout» / «Error». Bloquea hasta que la
    persona responde o vence `timeout_s`: llamarla desde un hilo de trabajo, nunca desde el bucle de eventos."""
    if not supported():
        return "Error"
    try:
        combase = ctypes.WinDLL("combase")
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
    except OSError:
        logger.warning("Windows Hello: no se pudieron cargar las bibliotecas de Windows", exc_info=True)
        return "Error"
    user32.GetForegroundWindow.restype = wintypes.HWND
    initialized = combase.RoInitialize(1) in (0, 1)  # MTA; S_OK o S_FALSE = hay que equilibrar con RoUninitialize
    strings: list[ctypes.c_void_p] = []
    factory = ctypes.c_void_p()
    op = ctypes.c_void_p()
    info = ctypes.c_void_p()
    try:
        def hstring(text: str) -> ctypes.c_void_p:
            h = ctypes.c_void_p()
            if combase.WindowsCreateString(ctypes.c_wchar_p(text), len(text), ctypes.byref(h)) != 0:
                raise OSError("WindowsCreateString")
            strings.append(h)
            return h

        iid_interop, iid_op, iid_info = _GUID.of(_IID_INTEROP), _GUID.of(_IID_ASYNC_OP), _GUID.of(_IID_ASYNC_INFO)
        if combase.RoGetActivationFactory(hstring("Windows.Security.Credentials.UI.UserConsentVerifier"), ctypes.byref(iid_interop), ctypes.byref(factory)) != 0:
            return "Error"
        hwnd = user32.GetForegroundWindow()
        request = _vcall(factory, 6, wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        if request(factory, hwnd, hstring(message), ctypes.byref(iid_op), ctypes.byref(op)) != 0:
            return "Error"
        if _vcall(op, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(op, ctypes.byref(iid_info), ctypes.byref(info)) != 0:
            return "Error"
        get_status = _vcall(info, 7, ctypes.POINTER(ctypes.c_int))
        status = ctypes.c_int(0)
        raised = False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            get_status(info, ctypes.byref(status))
            if status.value != 0:  # 0 = Started
                break
            if not raised:
                raised = _bring_hello_to_front(user32, kernel32)
            time.sleep(_POLL_S)
        if status.value == 0:
            _vcall(info, 9)(info)  # IAsyncInfo::Cancel: cierra la ventana que quedo abierta
            return "Timeout"
        if status.value != 1:  # 2 = Canceled, 3 = Error
            return "Canceled" if status.value == 2 else "Error"
        result = ctypes.c_int(-1)
        if _vcall(op, 8, ctypes.POINTER(ctypes.c_int))(op, ctypes.byref(result)) != 0:
            return "Error"
        return _RESULTS.get(result.value, "Error")
    except Exception:  # noqa: BLE001 -- cualquier falla de la llamada a Windows es «no confirmado», nunca un desbloqueo
        logger.warning("Windows Hello fallo", exc_info=True)
        return "Error"
    finally:
        for ptr in (info, op, factory):
            _release(ptr)
        for h in strings:
            combase.WindowsDeleteString(h)
        if initialized:
            combase.RoUninitialize()


def verify() -> bool:
    """El verificador que usa `secret_access`: True SOLO si Windows confirmo la identidad («Verified»)."""
    result = request_verification()
    if result != "Verified":
        logger.info("Windows Hello no confirmo la identidad: %s", result)
    return result == "Verified"
