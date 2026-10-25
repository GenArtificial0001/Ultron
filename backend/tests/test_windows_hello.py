"""Windows Hello como verificador del desbloqueo (spec gestor-conexiones REQ-007b, paso B3).

NINGUN test abre la ventana real de Windows Hello (la llamada a Windows se reemplaza). Lo que se prueba: que solo «Verified» desbloquea, que nunca lanza, que fuera de
Windows no toca nada, que `app.main` lo instala y que la bateria lo saca antes de cada test (tests/conftest.py). La ventana real se probo a mano con el usuario el
2026-10-08 (prueba B0: «Verified» en 5,6 s, adelante de Chrome)."""

from __future__ import annotations

import ctypes
import hashlib
import uuid

import pytest

from app import secret_access as sa
from app import windows_hello as wh


@pytest.mark.parametrize("result", ["Canceled", "Timeout", "Error", "NotConfiguredForUser", "DeviceNotPresent", "DisabledByPolicy", "RetriesExhausted"])
def test_only_verified_unlocks(monkeypatch, result):
    monkeypatch.setattr(wh, "request_verification", lambda *a, **k: result)

    assert wh.verify() is False


def test_verified_unlocks(monkeypatch):
    monkeypatch.setattr(wh, "request_verification", lambda *a, **k: "Verified")

    assert wh.verify() is True


def test_outside_windows_it_answers_error_without_touching_windows(monkeypatch):
    monkeypatch.setattr(wh.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: pytest.fail("no debe cargar ninguna biblioteca de Windows"), raising=False)

    assert wh.supported() is False and wh.request_verification() == "Error"


def test_if_windows_libraries_cannot_load_it_answers_error_and_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("sin combase")

    monkeypatch.setattr(wh.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", boom, raising=False)

    assert wh.request_verification() == "Error"


def test_the_async_operation_iid_is_the_winrt_parameterized_guid():
    """El GUID de IAsyncOperation<UserConsentVerificationResult> sale del algoritmo de WinRT (SHA-1 de la firma): si alguien lo tipea mal, Windows no responde."""
    sig = "pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};enum(Windows.Security.Credentials.UI.UserConsentVerificationResult;i4))"
    digest = bytearray(hashlib.sha1(uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee").bytes + sig.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80

    assert str(uuid.UUID(bytes=bytes(digest))) == wh._IID_ASYNC_OP


def test_guid_structure_keeps_the_text_it_was_built_from():
    g = wh._GUID.of(wh._IID_INTEROP)
    data4 = bytes(g.Data4)

    assert uuid.UUID(fields=(g.Data1, g.Data2, g.Data3, data4[0], data4[1], int.from_bytes(data4[2:], "big"))) == uuid.UUID(wh._IID_INTEROP)


def test_the_battery_never_starts_with_a_real_verifier_installed():
    """tests/conftest.py saca el verificador antes de cada test: ninguna prueba abre Windows Hello por accidente."""
    assert sa._verifier is None and sa.is_unlocked() is False


@pytest.mark.skipif(not wh.supported(), reason="solo Windows")
def test_vision_installs_windows_hello_as_the_unlock_verifier():
    from app import main

    main._install_unlock_verifier()

    assert sa._verifier is wh.verify  # el fixture de conftest lo vuelve a sacar al terminar
