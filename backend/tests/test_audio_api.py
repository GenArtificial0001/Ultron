"""Rutas `/api/audio/*` (spec control-microfono-windows REQ-001..004, 007, 008): lo que recibe y ve la pagina.

Windows es un FAKE (`audio_control.set_backend`): ningun test toca el audio real. Se prueba por HTTP, igual que lo usa la pantalla: listar, activar, volumen, y sobre
todo el guard de seguridad: estas rutas CAMBIAN hardware, asi que un pedido de otra PC, de otra pagina o de un sitio ajeno se rechaza ANTES de tocar nada
(el mismo criterio que la pantalla de contraseñas)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import audio_control, main
from app.audio_control import Microphone

BASE = "http://127.0.0.1:8020"
ARRAY = Microphone("id-array", "Microphone Array (Cirrus Logic)", True, True, 58)
HEADSET = Microphone("id-headset", "Microphone (Logi USB Headset)", False, False, 80)


class FakeAudio:
    def __init__(self):
        self.mics = {m.id: m for m in (ARRAY, HEADSET)}
        self.writes: list[tuple] = []

    def list_microphones(self):
        return list(self.mics.values())

    def set_mute(self, mic_id, muted):
        self.writes.append(("mute", mic_id, muted))
        m = self.mics[mic_id]
        self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, muted, m.volume)

    def set_volume(self, mic_id, scalar):
        self.writes.append(("volume", mic_id, round(scalar, 2)))
        m = self.mics[mic_id]
        self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, m.muted, round(scalar * 100))


@pytest.fixture
def audio():
    backend = FakeAudio()
    audio_control.set_backend(backend)
    yield backend
    audio_control.set_backend(None)


@pytest.fixture
def api(audio):
    return TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: listar
# ---------------------------------------------------------------------------------------------------------------------
def test_the_list_returns_every_connected_microphone_with_its_state(api):
    resp = api.get("/api/audio/mics")

    assert resp.status_code == 200
    mics = resp.json()["mics"]
    assert [m["name"] for m in mics] == [
        "Microphone Array (Cirrus Logic)",
        "Microphone (Logi USB Headset)",
    ]  # el predeterminado primero
    assert mics[0] == {
        "id": "id-array",
        "name": "Microphone Array (Cirrus Logic)",
        "is_default": True,
        "muted": True,
        "volume": 58,
    }


def test_listing_never_writes_anything(api, audio):
    api.get("/api/audio/mics")
    api.get("/api/audio/mics")

    assert audio.writes == []  # REQ-008: abrir Audio o un modo no modifica ningun dispositivo


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002 / 003 / 004: activar y volumen
# ---------------------------------------------------------------------------------------------------------------------
def test_activating_the_muted_default_microphone_returns_the_state_read_back_from_windows(api, audio):
    resp = api.post("/api/audio/mics/mute", params={"mic_id": "id-array"}, json={"muted": False})

    assert resp.status_code == 200
    assert resp.json()["mic"] == {
        "id": "id-array",
        "name": "Microphone Array (Cirrus Logic)",
        "is_default": True,
        "muted": False,
        "volume": 58,
    }
    assert audio.writes == [("mute", "id-array", False)]  # solo el silencio: el volumen no se toca


def test_a_volume_from_the_slider_sets_the_level_and_unmutes(api):
    resp = api.post("/api/audio/mics/volume", params={"mic_id": "id-array"}, json={"volume": 40})

    assert resp.status_code == 200
    assert resp.json()["mic"]["volume"] == 40 and resp.json()["mic"]["muted"] is False


def test_the_slider_at_zero_mutes_and_keeps_the_level(api):
    resp = api.post("/api/audio/mics/volume", params={"mic_id": "id-headset"}, json={"volume": 0})

    assert resp.json()["mic"]["muted"] is True and resp.json()["mic"]["volume"] == 80


def test_a_windows_style_id_with_braces_and_dots_travels_intact(api, audio):
    audio.mics["{0.0.1.00000000}.{c1a6f972-1f94-4a3f-9f37-6931a74c6c5a}"] = Microphone(
        "{0.0.1.00000000}.{c1a6f972-1f94-4a3f-9f37-6931a74c6c5a}", "Real", False, True, 10
    )

    resp = api.post(
        "/api/audio/mics/mute",
        params={"mic_id": "{0.0.1.00000000}.{c1a6f972-1f94-4a3f-9f37-6931a74c6c5a}"},
        json={"muted": False},
    )

    assert resp.status_code == 200 and resp.json()["mic"]["muted"] is False


@pytest.mark.parametrize("body", [{}, {"volume": "mucho"}, {"volume": None}])
def test_a_malformed_volume_is_rejected_without_touching_audio(api, audio, body):
    resp = api.post("/api/audio/mics/volume", params={"mic_id": "id-array"}, json=body)

    assert resp.status_code == 422 and audio.writes == []


def test_a_missing_mic_id_is_rejected_without_touching_audio(api, audio):
    resp = api.post("/api/audio/mics/mute", json={"muted": False})

    assert resp.status_code == 422 and audio.writes == []


# ---------------------------------------------------------------------------------------------------------------------
# REQ-007: fallas en lenguaje simple
# ---------------------------------------------------------------------------------------------------------------------
def test_a_microphone_that_was_unplugged_gets_a_plain_message_and_nothing_is_written(api, audio):
    resp = api.post("/api/audio/mics/mute", params={"mic_id": "id-que-se-desenchufo"}, json={"muted": False})

    assert resp.status_code == 422
    assert "ya no está conectado" in resp.json()["detail"] and audio.writes == []


def test_a_system_failure_never_leaks_codes_to_the_page(api, audio):
    def boom():
        raise OSError("EnumAudioEndpoints fallo: HRESULT 0x80070005")

    audio.list_microphones = boom

    resp = api.get("/api/audio/mics")

    assert resp.status_code == 422
    assert "HRESULT" not in resp.json()["detail"] and "0x" not in resp.json()["detail"]


def test_a_system_that_is_not_windows_answers_501_with_a_plain_message(monkeypatch):
    audio_control.set_backend(None)
    monkeypatch.setattr(audio_control.sys, "platform", "linux")
    client = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))

    resp = client.get("/api/audio/mics")

    assert resp.status_code == 501 and "solo está disponible en Windows" in resp.json()["detail"]


# ---------------------------------------------------------------------------------------------------------------------
# Seguridad: estas rutas cambian hardware, solo esta PC y la propia pagina
# ---------------------------------------------------------------------------------------------------------------------
FOREIGN = [
    ("otra PC de la red", {"client": ("10.0.0.5", 50000)}, {}),
    ("DNS rebinding (host ajeno)", {"client": ("127.0.0.1", 50000), "base": "http://ataque.example:8020"}, {}),
    ("otra pagina (Origin ajeno)", {"client": ("127.0.0.1", 50000)}, {"Origin": "https://pagina-maliciosa.example"}),
    ("pedido de otro sitio (Sec-Fetch-Site)", {"client": ("127.0.0.1", 50000)}, {"Sec-Fetch-Site": "cross-site"}),
]


@pytest.mark.parametrize(("why", "client_kw", "headers"), FOREIGN, ids=[f[0] for f in FOREIGN])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/audio/mics", None),
        ("post", "/api/audio/mics/mute?mic_id=id-array", {"muted": False}),
        ("post", "/api/audio/mics/volume?mic_id=id-array", {"volume": 5}),
    ],
)
def test_a_foreign_request_is_refused_before_anything_is_read_or_written(
    audio, why, client_kw, headers, method, path, body
):
    client = TestClient(main.app, base_url=client_kw.get("base", BASE), client=client_kw["client"])

    resp = getattr(client, method)(path, headers=headers, **({"json": body} if body is not None else {}))

    assert resp.status_code == 403, why
    assert audio.writes == []
    assert audio.mics["id-array"].muted is True  # el micro silenciado sigue exactamente igual


def test_the_page_itself_with_matching_origin_is_allowed(audio):
    client = TestClient(main.app, base_url=BASE, client=("127.0.0.1", 50000))

    resp = client.get("/api/audio/mics", headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"})

    assert resp.status_code == 200


def test_a_refused_request_explains_it_in_terms_of_audio_not_of_saved_passwords(audio):
    """El guard se comparte con el gestor de contraseñas, pero el texto que ve la persona tiene que hablar de SU audio: «credenciales» confundiria."""
    client = TestClient(main.app, base_url=BASE, client=("10.0.0.5", 50000))

    detail = client.get("/api/audio/mics").json()["detail"]

    assert "audio" in detail.lower() and "credencial" not in detail.lower() and "contraseña" not in detail.lower()
