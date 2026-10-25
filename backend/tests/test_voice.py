"""spec voz-fish-audio: motores de voz intercambiables (app/voice/).

Todo con servicios falsos: `engines._SESSION` se reemplaza por una sesion HTTP
de mentira, asi ningun test llama a Fish Audio ni a ElevenLabs de verdad
(REQ-009). La configuracion y la muestra de voz van a `tmp_path`."""

from __future__ import annotations

import base64
import io
import json
import struct
import wave

import pytest
import requests
from fastapi.testclient import TestClient

from app.config import get_settings
from app.voice import engines, local_clone, service, settings as voice_settings


# ---------------------------------------------------------------------------
# Infraestructura
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, content=b"", payload=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else content.decode("utf-8", "replace")

    def json(self):
        return self._payload


class FakeSession:
    """Responde segun una funcion `handler(method, url, kwargs)` y guarda cada pedido."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.handler(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def isolated_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_settings, "CONFIG_FILE", tmp_path / "voice_config.json")
    monkeypatch.setattr(local_clone, "SAMPLE_DIR", tmp_path / "voz")
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "clave-fish-de-prueba")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "clave-eleven-de-prueba")
    monkeypatch.setattr(settings, "fish_voice_id", "")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "")
    service.PAUSES.clear()
    yield
    service.PAUSES.clear()


def use_session(monkeypatch, handler):
    session = FakeSession(handler)
    monkeypatch.setattr(engines, "_SESSION", session)
    return session


def save_chain(chain, **extra):
    cfg = voice_settings.VoiceConfig(
        chain=chain,
        fish_voice_id=extra.get("fish_voice_id", "voz-fish"),
        elevenlabs_voice_id=extra.get("elevenlabs_voice_id", "voz-eleven"),
        consent=extra.get("consent", {"fish": True, "elevenlabs": True}),
    )
    voice_settings.save_config(cfg)
    return cfg


def wav_bytes(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def mp3_bytes(frames: int, xing_frames: int | None = None) -> bytes:
    """MP3 sintetico: frames MPEG1 Layer III, 128 kbps, 44,1 kHz, mono."""
    header = bytes([0xFF, 0xFB, 0x90, 0xC4])
    frame = header + b"\x00" * (417 - 4)
    if xing_frames is None:
        return frame * frames
    first = bytearray(frame)
    tag = b"Xing" + struct.pack(">I", 1) + struct.pack(">I", xing_frames)
    first[4 + 17 : 4 + 17 + len(tag)] = tag
    return bytes(first) + frame * (frames - 1)


# ---------------------------------------------------------------------------
# REQ-005 / REQ-006: motores en la nube
# ---------------------------------------------------------------------------
def test_fish_sends_the_voice_the_model_and_the_key(monkeypatch):
    session = use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"ID3audio"))
    audio = engines.FishAudioEngine().synthesize("Hola.", model="s2.1-pro-free", voice_id="3f45a7fd")
    assert audio == engines.Audio(b"ID3audio", "audio/mpeg")
    call = session.calls[0]
    assert call["url"] == "https://api.fish.audio/v1/tts"
    assert call["headers"]["Authorization"] == "Bearer clave-fish-de-prueba"
    assert call["headers"]["model"] == "s2.1-pro-free"
    assert call["json"]["reference_id"] == "3f45a7fd"
    assert call["json"]["text"] == "Hola."


def test_elevenlabs_sends_the_voice_in_the_url_and_the_model(monkeypatch):
    session = use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"mp3"))
    engines.ElevenLabsEngine().synthesize("Hola.", model="eleven_flash_v2_5", voice_id="voz123")
    call = session.calls[0]
    assert call["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voz123"
    assert call["headers"]["xi-api-key"] == "clave-eleven-de-prueba"
    assert call["json"]["model_id"] == "eleven_flash_v2_5"


@pytest.mark.parametrize(
    ("status", "payload", "kind"),
    [
        (402, {"message": "Payment required"}, "sin_credito"),
        (401, {"detail": {"status": "quota_exceeded"}}, "sin_credito"),
        (401, {"detail": {"status": "invalid_api_key"}}, "clave_invalida"),
        (404, {"message": "not found"}, "sin_voz"),
        (429, {"message": "slow down"}, "limite"),
        (500, {"message": "boom"}, "fallo"),
    ],
)
def test_service_errors_are_classified(monkeypatch, status, payload, kind):
    use_session(monkeypatch, lambda m, u, k: FakeResponse(status, payload=payload))
    with pytest.raises(engines.VoiceEngineError) as err:
        engines.ElevenLabsEngine().synthesize("Hola.", model="eleven_flash_v2_5", voice_id="v")
    assert err.value.kind == kind


def test_network_errors_mean_no_connection(monkeypatch):
    use_session(monkeypatch, lambda m, u, k: requests.ConnectionError("sin red"))
    with pytest.raises(engines.VoiceEngineError) as err:
        engines.FishAudioEngine().synthesize("Hola.", model="s2.1-pro", voice_id="v")
    assert err.value.kind == "sin_conexion"


def test_without_key_or_voice_nothing_is_sent(monkeypatch):
    session = use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"x"))
    monkeypatch.setattr(get_settings(), "fish_api_key", "")
    with pytest.raises(engines.VoiceEngineError) as err:
        engines.FishAudioEngine().synthesize("Hola.", model="s2.1-pro", voice_id="v")
    assert err.value.kind == "sin_clave"
    with pytest.raises(engines.VoiceEngineError) as err:
        engines.ElevenLabsEngine().synthesize("Hola.", model="eleven_flash_v2_5", voice_id="")
    assert err.value.kind == "sin_voz"
    assert session.calls == []


def test_list_voices_of_each_account(monkeypatch):
    def handler(method, url, kwargs):
        if "elevenlabs" in url:
            return FakeResponse(200, payload={"voices": [{"voice_id": "a1", "name": "Tomás"}]})
        return FakeResponse(200, payload={"items": [{"_id": "f1", "title": "Mi voz"}]})

    use_session(monkeypatch, handler)
    assert engines.ElevenLabsEngine().list_voices() == [{"id": "a1", "name": "Tomás"}]
    assert engines.FishAudioEngine().list_voices() == [{"id": "f1", "name": "Mi voz"}]


# ---------------------------------------------------------------------------
# REQ-002: apagado por defecto y consentimiento
# ---------------------------------------------------------------------------
def test_a_new_install_uses_the_browser_voice():
    cfg = voice_settings.load_config()
    assert cfg.chain == [] and cfg.effect is False
    with pytest.raises(service.NoVoiceEngineAvailable) as err:
        service.speak("Hola.")
    assert err.value.notice is None  # sin falla: el navegador habla sin aviso


def test_a_cloud_engine_cannot_be_saved_without_consent():
    with pytest.raises(voice_settings.VoiceConfigError, match="confirmar"):
        voice_settings.update_config({"chain": [{"engine": "fish"}]}, service.models_by_engine())
    cfg = voice_settings.update_config(
        {"chain": [{"engine": "fish"}], "consent": {"fish": True}}, service.models_by_engine()
    )
    assert cfg.chain == [{"engine": "fish", "model": "s2.1-pro"}]


def test_revoking_consent_takes_the_engine_out_of_the_chain():
    models = service.models_by_engine()
    voice_settings.update_config(
        {"chain": [{"engine": "fish"}, {"engine": "local"}], "consent": {"fish": True}}, models
    )
    cfg = voice_settings.update_config({"consent": {"fish": False}}, models)
    assert cfg.chain == [{"engine": "local", "model": "multilingual-v3"}]


def test_text_never_goes_to_a_cloud_engine_without_consent(monkeypatch):
    """Aunque el archivo de configuracion se haya editado a mano."""
    session = use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"x"))
    save_chain([{"engine": "fish", "model": "s2.1-pro"}], consent={})
    with pytest.raises(service.NoVoiceEngineAvailable):
        service.speak("Dato de la empresa.")
    assert session.calls == []


def test_the_chain_is_validated():
    models = service.models_by_engine()
    with pytest.raises(voice_settings.VoiceConfigError):
        voice_settings.update_config({"chain": [{"engine": "inventado"}]}, models)
    with pytest.raises(voice_settings.VoiceConfigError):
        voice_settings.update_config({"chain": [{"engine": "local", "model": "otro"}]}, models)
    cfg = voice_settings.update_config({"chain": [{"engine": "local"}, {"engine": "local"}]}, models)
    assert len(cfg.chain) == 1


def test_env_voice_ids_are_the_initial_value(monkeypatch):
    monkeypatch.setattr(get_settings(), "fish_voice_id", "voz-del-env")
    assert voice_settings.VoiceConfig().voice_for("fish") == "voz-del-env"
    assert voice_settings.VoiceConfig(fish_voice_id="elegida").voice_for("fish") == "elegida"


# ---------------------------------------------------------------------------
# REQ-003: salto automatico, pausas y un solo aviso
# ---------------------------------------------------------------------------
def fish_by_model(responses):
    def handler(method, url, kwargs):
        return responses[kwargs["headers"]["model"]]

    return handler


def test_out_of_credit_jumps_to_the_free_model_and_warns(monkeypatch):
    use_session(
        monkeypatch,
        fish_by_model({"s2.1-pro": FakeResponse(402, payload={}), "s2.1-pro-free": FakeResponse(200, b"free")}),
    )
    save_chain([{"engine": "fish", "model": "s2.1-pro"}, {"engine": "fish", "model": "s2.1-pro-free"}])
    result = service.speak("Hola.")
    assert result.audio.data == b"free" and result.model == "s2.1-pro-free"
    assert result.notice.code == "VOZ-MOTOR-CREDITO"
    assert "Fish Audio (s2.1-pro-free)" in result.notice.message


def test_a_failed_engine_is_paused_and_the_next_sentence_does_not_warn_again(monkeypatch):
    session = use_session(
        monkeypatch,
        fish_by_model({"s2.1-pro": FakeResponse(402, payload={}), "s2.1-pro-free": FakeResponse(200, b"free")}),
    )
    save_chain([{"engine": "fish", "model": "s2.1-pro"}, {"engine": "fish", "model": "s2.1-pro-free"}])
    service.speak("Primera.")
    second = service.speak("Segunda.")
    assert second.notice is None
    models_called = [c["headers"]["model"] for c in session.calls]
    assert models_called == ["s2.1-pro", "s2.1-pro-free", "s2.1-pro-free"]


def test_when_everything_fails_the_browser_voice_speaks(monkeypatch):
    use_session(monkeypatch, lambda m, u, k: FakeResponse(401, payload={"detail": "invalid"}))
    save_chain([{"engine": "elevenlabs", "model": "eleven_flash_v2_5"}])
    with pytest.raises(service.NoVoiceEngineAvailable) as err:
        service.speak("Hola.")
    assert err.value.notice.code == "VOZ-MOTOR-CLAVE"
    assert "la voz del navegador" in err.value.notice.message


def test_testing_one_link_does_not_pause_it(monkeypatch):
    use_session(monkeypatch, lambda m, u, k: FakeResponse(500, payload={}))
    save_chain([{"engine": "fish", "model": "s2.1-pro"}])
    with pytest.raises(service.NoVoiceEngineAvailable):
        service.speak("Prueba.", only={"engine": "fish", "model": "s2.1-pro"})
    assert service.PAUSES.paused_until("fish:s2.1-pro") is None


def test_status_explains_what_each_link_is_missing(monkeypatch):
    monkeypatch.setattr(get_settings(), "elevenlabs_api_key", "")
    save_chain(
        [{"engine": "elevenlabs", "model": "eleven_flash_v2_5"}, {"engine": "local", "model": "multilingual-v3"}]
    )
    monkeypatch.setattr(local_clone, "is_installed", lambda: False)
    items = service.status_payload()["items"]
    assert "Credenciales" in items[0]["missing"]
    assert "instalar" in items[1]["missing"]


# ---------------------------------------------------------------------------
# REQ-007: muestra de voz, instalacion con simulacion previa y motor local
# ---------------------------------------------------------------------------
def test_wav_and_mp3_durations():
    assert local_clone.wav_duration(wav_bytes(12)) == pytest.approx(12, abs=0.01)
    assert local_clone.mp3_duration(mp3_bytes(500)) == pytest.approx(13.03, abs=0.05)
    assert local_clone.mp3_duration(mp3_bytes(3, xing_frames=1000)) == pytest.approx(26.12, abs=0.05)


def test_a_valid_sample_is_saved_outside_the_repo_and_replaces_the_previous_one(tmp_path):
    local_clone.save_sample(wav_bytes(12), "mi voz.wav")
    info = local_clone.save_sample(mp3_bytes(500), "mi voz.mp3")
    assert info == {"present": True, "seconds": pytest.approx(13.0, abs=0.1), "format": "mp3"}
    assert sorted(p.name for p in (tmp_path / "voz").iterdir()) == ["muestra.mp3"]


@pytest.mark.parametrize(
    ("data", "filename", "message"),
    [
        (wav_bytes(5), "corta.wav", "10 s como mínimo"),
        (b"no es audio", "voz.wav", "WAV válido"),
        (b"no es audio", "voz.mp3", "MP3 válido"),
        (wav_bytes(12), "voz.ogg", "WAV o MP3"),
    ],
    ids=["corta", "wav-roto", "mp3-roto", "otro-formato"],
)
def test_invalid_samples_are_rejected_with_an_explanation(data, filename, message):
    with pytest.raises(local_clone.SampleError, match=message):
        local_clone.save_sample(data, filename)
    assert local_clone.sample_path() is None


def test_plan_conflicts_only_counts_packages_that_change():
    report = {
        "install": [
            {"metadata": {"name": "numpy", "version": "1.26.4"}},
            {"metadata": {"name": "torch", "version": "2.6.0"}},
            {"metadata": {"name": "Requests", "version": "2.34.0"}},
        ]
    }
    installed = {"numpy": "2.5.1", "requests": "2.34.0"}
    assert local_clone.plan_conflicts(report, installed) == [("numpy", "2.5.1", "1.26.4")]


def test_conflict_message_suggests_python_313_when_numpy_would_change():
    message = local_clone.describe_conflicts([("numpy", "2.5.1", "1.26.4")], (3, 12))
    assert "No instalé nada" in message and "Python 3.13" in message


def run_install(monkeypatch, report, *, install_code=0, warmup_code=0):
    steps = []

    def fake_pip(args, on_line):
        steps.append(args[:2])
        if "--dry-run" in args:
            path = args[args.index("--report") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh)
            return 0
        on_line("Successfully installed chatterbox-tts")
        return install_code

    monkeypatch.setattr(local_clone, "_run_pip", fake_pip)
    monkeypatch.setattr(local_clone, "_run_warmup", lambda on_line: warmup_code)
    monkeypatch.setattr(local_clone, "installed_versions", lambda: {"numpy": "2.5.1"})
    monkeypatch.setattr(local_clone, "IS_FROZEN", False)
    local_clone.start_install()
    local_clone.INSTALL.thread.join(timeout=10)
    return steps, local_clone.install_status()


def test_install_is_cancelled_before_touching_anything_if_it_would_change_vision(monkeypatch):
    report = {"install": [{"metadata": {"name": "numpy", "version": "1.26.4"}}]}
    steps, status = run_install(monkeypatch, report)
    assert steps == [["install", "--dry-run"]]  # nunca llego a instalar
    assert status["status"] == "error" and "numpy 2.5.1 → 1.26.4" in status["message"]


def test_install_without_conflicts_installs_and_downloads_the_model(monkeypatch):
    report = {"install": [{"metadata": {"name": "chatterbox-tts", "version": "0.1.7"}}]}
    steps, status = run_install(monkeypatch, report)
    assert steps == [["install", "--dry-run"], ["install", "chatterbox-tts"]]
    assert status["status"] == "done"
    assert "Successfully installed chatterbox-tts" in status["log"]


def test_the_local_engine_explains_what_is_missing(monkeypatch):
    engine = local_clone.LocalCloneEngine()
    monkeypatch.setattr(local_clone, "is_installed", lambda: False)
    with pytest.raises(engines.VoiceEngineError) as err:
        engine.synthesize("Hola.", model="multilingual-v3", voice_id="")
    assert err.value.kind == "no_instalado"
    monkeypatch.setattr(local_clone, "is_installed", lambda: True)
    with pytest.raises(engines.VoiceEngineError) as err:
        engine.synthesize("Hola.", model="multilingual-v3", voice_id="")
    assert err.value.kind == "sin_muestra"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def test_speak_endpoint_returns_the_audio_in_base64(client, monkeypatch):
    use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"mp3-bytes"))
    save_chain([{"engine": "fish", "model": "s2.1-pro"}])
    data = client.post("/api/voice/speak", json={"text": "Hola."}).json()
    assert base64.b64decode(data["audio"]) == b"mp3-bytes"
    assert data["mime"] == "audio/mpeg" and data["engine"] == "fish" and data["notice"] is None


def test_speak_endpoint_without_engines_answers_without_audio(client):
    data = client.post("/api/voice/speak", json={"text": "Hola."}).json()
    assert data == {"audio": None, "notice": None}


def test_speak_endpoint_rejects_empty_and_huge_texts(client):
    assert client.post("/api/voice/speak", json={"text": "  "}).status_code == 400
    assert client.post("/api/voice/speak", json={"text": "a" * 2000}).status_code == 400


def test_testing_a_cloud_engine_needs_consent(client, monkeypatch):
    session = use_session(monkeypatch, lambda m, u, k: FakeResponse(200, b"x"))
    resp = client.post("/api/voice/speak", json={"text": "Prueba.", "engine": "fish"})
    assert resp.status_code == 400 and "confirmá" in resp.json()["detail"]
    assert session.calls == []


def test_config_endpoint_saves_and_clears_pauses(client):
    service.PAUSES.pause("fish:s2.1-pro", 600)
    resp = client.post(
        "/api/voice/config",
        json={"chain": [{"engine": "fish", "model": "s2.1-pro"}], "consent": {"fish": True}, "effect": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["chain"] == [{"engine": "fish", "model": "s2.1-pro"}]
    assert data["effect"] is True and data["consent"]["fish"] is True
    assert service.PAUSES.paused_until("fish:s2.1-pro") is None


def test_config_endpoint_explains_a_rejected_config(client):
    resp = client.post("/api/voice/config", json={"chain": [{"engine": "elevenlabs"}]})
    assert resp.status_code == 400 and "confirmar" in resp.json()["detail"]


def test_voices_endpoint_gives_a_friendly_error(client, monkeypatch):
    use_session(monkeypatch, lambda m, u, k: FakeResponse(401, payload={"detail": "invalid_api_key"}))
    resp = client.get("/api/voice/voices/elevenlabs")
    assert resp.status_code == 502 and "Credenciales" in resp.json()["detail"]


def test_sample_endpoint(client):
    ok = "data:audio/wav;base64," + base64.b64encode(wav_bytes(11)).decode()
    resp = client.post("/api/voice/local/sample", json={"audio": ok, "filename": "voz.wav"})
    assert resp.status_code == 200 and resp.json()["present"] is True
    short = "data:audio/wav;base64," + base64.b64encode(wav_bytes(3)).decode()
    resp = client.post("/api/voice/local/sample", json={"audio": short, "filename": "voz.wav"})
    assert resp.status_code == 400 and "10 s" in resp.json()["detail"]
    assert client.delete("/api/voice/local/sample").json()["present"] is False
