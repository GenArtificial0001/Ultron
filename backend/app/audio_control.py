"""Control del microfono de Windows desde Vision: listar los conectados, activar/silenciar y ajustar el volumen (spec control-microfono-windows).

Por que existe (caso real del usuario, 2026-10-15): al iniciar Windows el microfono predeterminado queda SILENCIADO. El navegador abre un microfono
silenciado sin ningun error (`getUserMedia` devuelve un stream valido que solo trae silencio), asi que Vision no escuchaba y no avisaba nada. El estado de
silencio y el volumen viven en el sistema (Panel de control > Sonido > Grabacion), no en el navegador: solo el backend, que corre en la misma PC, puede leerlos
y cambiarlos.

Como (REQ-001..004): Core Audio de Windows (`IMMDeviceEnumerator` + `IAudioEndpointVolume`) por `ctypes`, sin dependencias nuevas (`pycaw` no esta en el indice de
paquetes de la empresa). Lectura y escritura probadas en la PC del usuario con restauracion exacta del estado original.

Reglas que este modulo fija:
- Solo se listan microfonos CONECTADOS (`DEVICE_STATE_ACTIVE`); los desconectados no se ofrecen (REQ-001).
- `set_mute` solo cambia el silencio: no toca el volumen (REQ-002) ni cual es el microfono predeterminado de Windows (decision D1 del usuario).
- `set_volume`: de 1 a 100 fija el volumen Y quita el silencio; 0 SILENCIA y deja el volumen como estaba, porque Windows no silencia solo al bajar a 0 (REQ-003/004).
  Asi «Activar» despues de un 0 devuelve el volumen anterior en vez de dejar un micro activo pero mudo.
- Toda escritura se RELEE y, si Windows no aplico el cambio, se avisa (nunca se afirma algo que no se verifico).
- Un microfono sin control de volumen (algunos drivers) se lista con `muted`/`volume` en None («estado desconocido»), jamas como silenciado (REQ-009).
- Nunca se toca nada por iniciativa propia (REQ-008): solo las funciones `set_*`, y solo las llama una accion explicita de la persona.

Los errores son `AudioControlError` con texto para una persona; el detalle tecnico va al log. En un sistema que no es Windows, `AudioUnsupported` (REQ-007)."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_void_p, wintypes
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class AudioControlError(Exception):
    """Falla al leer o cambiar el audio; el texto es apto para mostrarle a una persona."""


class AudioUnsupported(AudioControlError):
    """Este sistema no es Windows (o no tiene Core Audio): no se puede controlar el microfono desde Vision."""


@dataclass(frozen=True)
class Microphone:
    """Un microfono conectado. `muted`/`volume` son None si el driver no deja leerlos (estado desconocido, nunca «silenciado»).
    `volume` es el nivel del dispositivo (0-100) aunque este silenciado: el silencio es otra cosa."""

    id: str
    name: str
    is_default: bool
    muted: bool | None
    volume: int | None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "is_default": self.is_default,
            "muted": self.muted,
            "volume": self.volume,
        }


class AudioBackend(Protocol):
    """Lo unico que el resto de Vision necesita del sistema de audio. Los tests lo reemplazan por uno falso (`set_backend`)."""

    def list_microphones(self) -> list[Microphone]: ...

    def set_mute(self, mic_id: str, muted: bool) -> None: ...

    def set_volume(self, mic_id: str, scalar: float) -> None: ...


# ---------------------------------------------------------------------------------------------------------------------
# Core Audio de Windows por ctypes (no corre en otros sistemas)
# ---------------------------------------------------------------------------------------------------------------------
_E_CAPTURE = 1  # EDataFlow.eCapture: entradas (microfonos)
_E_CONSOLE = 0  # ERole.eConsole: el predeterminado «de uso general» (el que muestra Panel de control)
_STATE_ACTIVE = 1  # DEVICE_STATE_ACTIVE: conectado y habilitado
_CLSCTX_ALL = 23
_VT_LPWSTR = 31
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: el hilo ya tenia COM iniciado en otro modo; se sigue sin reiniciarlo
_HRESULT = ctypes.c_long


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        import uuid

        value = uuid.UUID(text)
        return cls(value.time_low, value.time_mid, value.time_hi_version, (ctypes.c_ubyte * 8)(*value.bytes[8:]))


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    """PROPVARIANT de 24 bytes en 64 bits: `vt`, 3 reservados y la union (aca solo se lee el puntero a texto)."""

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("ptr", c_void_p),
        ("pad", c_void_p),
    ]


def _vtable_call(ptr, index: int, *argtypes):
    """Metodo `index` de la vtable de un puntero COM, ya enlazado a `ptr`."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(_HRESULT, c_void_p, *argtypes)
    function = prototype(vtbl[index])
    return lambda *args: function(ptr, *args)


def _check(hresult: int, what: str) -> None:
    if hresult < 0:
        raise OSError(f"{what} fallo: HRESULT 0x{hresult & 0xFFFFFFFF:08X}")


def _release(ptr) -> None:
    if ptr and ptr.value:
        _vtable_call(ptr, 2)()  # IUnknown::Release


class _ComScope:
    """Inicia COM en el hilo que llama (los pedidos del servidor llegan por un pool de hilos) y lo cierra al salir."""

    def __enter__(self) -> _ComScope:
        ole32 = ctypes.WinDLL("ole32")
        hresult = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        self._owns = hresult >= 0
        if hresult < 0 and hresult != _RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx fallo: HRESULT 0x{hresult & 0xFFFFFFFF:08X}")
        return self

    def __exit__(self, *exc) -> None:
        if self._owns:
            ctypes.WinDLL("ole32").CoUninitialize()


class WindowsCoreAudio:
    """Implementacion real de `AudioBackend`. Cada llamada abre y cierra COM y libera todo lo que pide (sin objetos vivos entre pedidos)."""

    _lock = (
        threading.Lock()
    )  # un solo pedido a la vez al hardware: evita ordenes cruzadas si se tocan dos controles seguidos

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AudioUnsupported("Controlar el micrófono desde ULTRON solo está disponible en Windows.")

    # -- enumerador -------------------------------------------------------------------------------------------------
    @staticmethod
    def _enumerator():
        ole32 = ctypes.WinDLL("ole32")
        enumerator = c_void_p()
        clsid = _GUID.parse("{BCDE0395-E52F-467C-8E3D-C4579291692E}")  # MMDeviceEnumerator
        iid = _GUID.parse("{A95664D2-9614-4F35-A746-DE8DB63617E6}")  # IMMDeviceEnumerator
        _check(ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid), byref(enumerator)), "CoCreateInstance")
        return enumerator

    @staticmethod
    def _device_id(device) -> str:
        raw = c_void_p()
        _check(_vtable_call(device, 5, POINTER(c_void_p))(byref(raw)), "IMMDevice::GetId")  # 5 = GetId
        try:
            return ctypes.wstring_at(raw.value) if raw.value else ""
        finally:
            ctypes.WinDLL("ole32").CoTaskMemFree(raw)

    @staticmethod
    def _device_name(device) -> str:
        store = c_void_p()
        _check(
            _vtable_call(device, 4, c_uint, POINTER(c_void_p))(0, byref(store)), "OpenPropertyStore"
        )  # 4 = OpenPropertyStore, STGM_READ
        try:
            key = _PropertyKey(_GUID.parse("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)  # PKEY_Device_FriendlyName
            value = _PropVariant()
            _check(
                _vtable_call(store, 5, POINTER(_PropertyKey), POINTER(_PropVariant))(byref(key), byref(value)),
                "GetValue",
            )  # 5 = GetValue
            try:
                return ctypes.wstring_at(value.ptr) if value.vt == _VT_LPWSTR and value.ptr else ""
            finally:
                ctypes.WinDLL("ole32").PropVariantClear(byref(value))
        finally:
            _release(store)

    @staticmethod
    def _endpoint_volume(device):
        """`IAudioEndpointVolume` del dispositivo, o None si el driver no expone control de volumen."""
        volume = c_void_p()
        iid = _GUID.parse("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        hresult = _vtable_call(device, 3, POINTER(_GUID), c_uint, c_void_p, POINTER(c_void_p))(
            byref(iid), _CLSCTX_ALL, None, byref(volume)
        )
        return volume if hresult >= 0 and volume.value else None

    @staticmethod
    def _read_state(volume) -> tuple[bool, float]:
        muted = wintypes.BOOL()
        level = c_float()
        _check(_vtable_call(volume, 15, POINTER(wintypes.BOOL))(byref(muted)), "GetMute")  # 15 = GetMute
        _check(
            _vtable_call(volume, 9, POINTER(c_float))(byref(level)), "GetMasterVolumeLevelScalar"
        )  # 9 = GetMasterVolumeLevelScalar
        return bool(muted.value), float(level.value)

    # -- AudioBackend -----------------------------------------------------------------------------------------------
    def list_microphones(self) -> list[Microphone]:
        with self._lock, _ComScope():
            enumerator = self._enumerator()
            try:
                default_id = ""
                default = c_void_p()
                if (
                    _vtable_call(enumerator, 4, c_int, c_int, POINTER(c_void_p))(_E_CAPTURE, _E_CONSOLE, byref(default))
                    >= 0
                    and default.value
                ):
                    try:
                        default_id = self._device_id(default)
                    finally:
                        _release(default)
                collection = c_void_p()
                _check(
                    _vtable_call(enumerator, 3, c_int, c_uint, POINTER(c_void_p))(
                        _E_CAPTURE, _STATE_ACTIVE, byref(collection)
                    ),
                    "EnumAudioEndpoints",
                )
                try:
                    count = c_uint()
                    _check(_vtable_call(collection, 3, POINTER(c_uint))(byref(count)), "GetCount")  # 3 = GetCount
                    result: list[Microphone] = []
                    for index in range(count.value):
                        device = c_void_p()
                        _check(
                            _vtable_call(collection, 4, c_uint, POINTER(c_void_p))(index, byref(device)), "Item"
                        )  # 4 = Item
                        try:
                            mic_id = self._device_id(device)
                            volume = self._endpoint_volume(device)
                            muted: bool | None = None
                            percent: int | None = None
                            if volume is not None:
                                try:
                                    is_muted, scalar = self._read_state(volume)
                                    muted, percent = is_muted, round(scalar * 100)
                                except OSError:
                                    logger.warning("No se pudo leer el estado de audio de %s", mic_id, exc_info=True)
                                finally:
                                    _release(volume)
                            result.append(
                                Microphone(
                                    mic_id,
                                    self._device_name(device) or "Micrófono",
                                    mic_id == default_id,
                                    muted,
                                    percent,
                                )
                            )
                        finally:
                            _release(device)
                    return result
                finally:
                    _release(collection)
            finally:
                _release(enumerator)

    def _with_volume(self, mic_id: str, action) -> None:
        with self._lock, _ComScope():
            enumerator = self._enumerator()
            try:
                device = c_void_p()
                _check(
                    _vtable_call(enumerator, 5, ctypes.c_wchar_p, POINTER(c_void_p))(mic_id, byref(device)), "GetDevice"
                )  # 5 = GetDevice
                try:
                    volume = self._endpoint_volume(device)
                    if volume is None:
                        raise AudioControlError(
                            "Este micrófono no deja que ULTRON cambie su volumen. Usá el Panel de control de Windows."
                        )
                    try:
                        action(volume)
                    finally:
                        _release(volume)
                finally:
                    _release(device)
            finally:
                _release(enumerator)

    def set_mute(self, mic_id: str, muted: bool) -> None:
        def action(volume) -> None:
            _check(_vtable_call(volume, 14, wintypes.BOOL, c_void_p)(muted, None), "SetMute")  # 14 = SetMute

        self._with_volume(mic_id, action)

    def set_volume(self, mic_id: str, scalar: float) -> None:
        def action(volume) -> None:
            _check(
                _vtable_call(volume, 7, c_float, c_void_p)(c_float(scalar), None), "SetMasterVolumeLevelScalar"
            )  # 7 = SetMasterVolumeLevelScalar

        self._with_volume(mic_id, action)


# ---------------------------------------------------------------------------------------------------------------------
# API del modulo (lo que usa main.py). El backend se crea la primera vez y los tests lo reemplazan.
# ---------------------------------------------------------------------------------------------------------------------
_backend: AudioBackend | None = None


def set_backend(backend: AudioBackend | None) -> None:
    """Solo para tests: instala un backend falso (o None para volver al real)."""
    global _backend
    _backend = backend


def _get_backend() -> AudioBackend:
    global _backend
    if _backend is None:
        _backend = WindowsCoreAudio()
    return _backend


def _friendly(exc: Exception, what: str) -> AudioControlError:
    """Convierte una falla del sistema en un error para personas; el detalle tecnico queda en el log."""
    if isinstance(exc, AudioControlError):
        return exc
    logger.warning("Fallo de audio de Windows al %s: %s", what, exc, exc_info=True)
    return AudioControlError(f"Windows no dejó {what}. Probá desde el Panel de control > Sonido > Grabación.")


def list_microphones() -> list[Microphone]:
    """Microfonos conectados (REQ-001), el predeterminado primero y despues por nombre."""
    try:
        mics = _get_backend().list_microphones()
    except Exception as exc:  # noqa: BLE001 -- cualquier falla del sistema de audio es un mensaje para la persona, nunca un 500
        raise _friendly(exc, "leer los micrófonos") from exc
    return sorted(mics, key=lambda m: (not m.is_default, m.name.lower()))


def _find(mic_id: str) -> Microphone:
    for mic in list_microphones():
        if mic.id == mic_id:
            return mic
    raise AudioControlError("Ese micrófono ya no está conectado. Elegí otro de la lista.")


def set_mute(mic_id: str, muted: bool) -> Microphone:
    """Silencia o activa el microfono SIN tocar su volumen ni cual es el predeterminado (REQ-002). Devuelve el estado releido."""
    _find(mic_id)  # confirma que sigue conectado antes de escribir
    try:
        _get_backend().set_mute(mic_id, muted)
    except Exception as exc:  # noqa: BLE001 -- ver list_microphones
        raise _friendly(exc, "activar el micrófono" if not muted else "silenciar el micrófono") from exc
    after = _find(mic_id)
    if after.muted is not muted:
        raise AudioControlError(
            "Windows no aplicó el cambio en este micrófono. Probá desde el Panel de control > Sonido > Grabación."
        )
    return after


def set_volume(mic_id: str, percent: int) -> Microphone:
    """Volumen de 0 a 100. De 1 a 100: fija el nivel y quita el silencio (REQ-003). 0: SILENCIA y deja el nivel como estaba (REQ-004)."""
    level = max(0, min(100, int(percent)))
    _find(mic_id)
    try:
        backend = _get_backend()
        if level == 0:
            backend.set_mute(mic_id, True)
        else:
            backend.set_volume(mic_id, level / 100)
            backend.set_mute(mic_id, False)
    except Exception as exc:  # noqa: BLE001 -- ver list_microphones
        raise _friendly(exc, "cambiar el volumen") from exc
    after = _find(mic_id)
    expected_muted = level == 0
    if after.muted is not expected_muted or (level > 0 and after.volume is not None and abs(after.volume - level) > 1):
        raise AudioControlError(
            "Windows no aplicó el cambio de volumen en este micrófono. Probá desde el Panel de control > Sonido > Grabación."
        )
    return after
