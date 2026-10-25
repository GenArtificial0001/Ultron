"""Cifrado de la configuracion del usuario con DPAPI de Windows (spec gestor-conexiones REQ-001).

Por que existe: el usuario decidio (2026-10-08) que ni el `.env` ni `connections.json` deben existir y que TODO (conexiones, contrasenas, la key de BigQuery, la
configuracion general) quede CIFRADO en `~/.vision/vision.db`. Hace falta un cifrado que no dependa de un archivo de clave: si la clave viviera en disco junto a la
base, copiar las dos cosas alcanzaria para leer todo.

Diseno: DPAPI (`CryptProtectData`/`CryptUnprotectData` de `crypt32.dll`) por `ctypes`, sin dependencias nuevas (`pywin32` no esta instalado). Windows deriva la clave de la
sesion del USUARIO: solo ese usuario de Windows, en esa PC, puede descifrar. Copiar `vision.db` a otra PC u otro perfil no muestra ningun valor (y Vision lo informa como
«hay que volver a cargar la contrasena», nunca rompe el arranque). La entropia propia de Vision evita que un blob de otra aplicacion DPAPI se confunda con uno de Vision.

Lo que NO protege (honesto): un programa que ya corre con la sesion del mismo usuario puede llamar a DPAPI igual que Vision. Es la misma garantia que el administrador de
credenciales de Windows o los navegadores para sus contrasenas guardadas.

Uso: `encrypt_json(dict) -> bytes` y `decrypt_json(bytes) -> dict`. Cualquier falla sale como `SecretStoreError` con un texto sin valores."""

from __future__ import annotations

import ctypes
import json
import sys
from typing import Any

# Entropia propia de Vision (no es un secreto: liga cada blob a Vision y a esta version del formato). Cambiarla deja ilegible todo lo guardado.
_ENTROPY = b"Vision/gestor-conexiones/v1"
_DESCRIPTION = "ULTRON"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01  # nunca abrir una ventana de Windows al cifrar o descifrar: el servidor corre sin interfaz


class SecretStoreError(Exception):
    """No se pudo cifrar o descifrar. El texto nunca incluye valores; `undecryptable=True` cuando el blob existe pero este usuario/PC no puede leerlo."""

    def __init__(self, message: str, *, undecryptable: bool = False) -> None:
        super().__init__(message)
        self.undecryptable = undecryptable


def is_available() -> bool:
    """DPAPI existe solo en Windows (Vision es una app de Windows; el lanzador y el `.exe` tambien)."""
    return sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    """Arma un DATA_BLOB que apunta a una copia de `data`. Devuelve tambien el buffer: si se pierde la referencia, Python lo libera y el puntero queda colgado."""
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _functions() -> tuple[Any, Any, Any]:
    """Las tres funciones de Windows, con sus firmas declaradas (sin `argtypes` un puntero de 64 bits se trunca)."""
    if not is_available():
        raise SecretStoreError("El cifrado de la configuracion solo funciona en Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_p = ctypes.POINTER(_DataBlob)
    protect = crypt32.CryptProtectData
    protect.argtypes = [blob_p, ctypes.c_wchar_p, blob_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_p]
    protect.restype = ctypes.c_int
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_p]
    unprotect.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return protect, unprotect, local_free


def _take(out: _DataBlob, local_free: Any) -> bytes:
    """Copia el resultado de Windows y libera su memoria (DPAPI la reserva con LocalAlloc)."""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        local_free(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect_bytes(data: bytes) -> bytes:
    """Cifra `data` para el usuario de Windows actual (REQ-001)."""
    protect, _unprotect, local_free = _functions()
    in_blob, _in_buf = _blob_from(data)
    entropy, _ent_buf = _blob_from(_ENTROPY)
    out = _DataBlob()
    if not protect(ctypes.byref(in_blob), _DESCRIPTION, ctypes.byref(entropy), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise SecretStoreError(f"Windows no pudo cifrar la configuracion (codigo {ctypes.get_last_error()}).")
    return _take(out, local_free)


def unprotect_bytes(blob: bytes) -> bytes:
    """Descifra un blob de `protect_bytes`. Falla con `undecryptable=True` si es de otro usuario/PC, si se altero o si no es de Vision."""
    _protect, unprotect, local_free = _functions()
    if not blob:
        raise SecretStoreError("No hay datos cifrados para leer.", undecryptable=True)
    in_blob, _in_buf = _blob_from(blob)
    entropy, _ent_buf = _blob_from(_ENTROPY)
    out = _DataBlob()
    if not unprotect(ctypes.byref(in_blob), None, ctypes.byref(entropy), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise SecretStoreError(
            "Este usuario de Windows no puede leer la configuracion guardada (se guardo en otra PC u otro perfil, o se altero).",
            undecryptable=True,
        )
    return _take(out, local_free)


def encrypt_json(data: dict[str, Any]) -> bytes:
    """Cifra un diccionario (los campos de una conexion o de la configuracion general)."""
    return protect_bytes(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def decrypt_json(blob: bytes) -> dict[str, Any]:
    """Descifra lo de `encrypt_json`. Un contenido que no es un diccionario JSON se trata como ilegible (nunca se devuelve a medias)."""
    raw = unprotect_bytes(blob)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SecretStoreError("La configuracion guardada esta danada.", undecryptable=True) from exc
    if not isinstance(data, dict):
        raise SecretStoreError("La configuracion guardada esta danada.", undecryptable=True)
    return data
