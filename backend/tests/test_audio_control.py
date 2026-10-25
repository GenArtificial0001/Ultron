"""Control del microfono de Windows (`app/audio_control.py`, spec control-microfono-windows REQ-001..004, 006..009).

El sistema de audio es un FAKE (`set_backend`): ningun test toca el hardware ni el volumen real de quien corre la suite. Lo que se fija son las REGLAS que
la persona ve, no la implementacion de Core Audio: solo microfonos conectados, «activar» no toca el volumen, 0 silencia y conserva el nivel, subir desde 0
desilencia, el predeterminado va primero, un micro que no deja leer su estado nunca figura como silenciado, y una escritura que Windows no aplico se avisa.

La parte `ctypes` real se probo a mano contra el hardware de la PC del usuario con restauracion exacta (ver specs/control-microfono-windows/progress.md)."""

from __future__ import annotations

import pytest

from app import audio_control
from app.audio_control import AudioControlError, Microphone

pytestmark = pytest.mark.unit


class FakeAudio:
    """Un Windows de mentira: guarda silencio/volumen por microfono y registra cada ESCRITURA (para REQ-008)."""

    def __init__(self, mics: list[Microphone]):
        self.mics = {m.id: m for m in mics}
        self.writes: list[tuple] = []
        self.ignore_writes = False  # simula un driver que dice OK y no aplica nada
        self.fail_with: Exception | None = None

    def list_microphones(self) -> list[Microphone]:
        if self.fail_with:
            raise self.fail_with
        return list(self.mics.values())

    def set_mute(self, mic_id: str, muted: bool) -> None:
        self.writes.append(("mute", mic_id, muted))
        if self.fail_with:
            raise self.fail_with
        if not self.ignore_writes:
            m = self.mics[mic_id]
            self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, muted, m.volume)

    def set_volume(self, mic_id: str, scalar: float) -> None:
        self.writes.append(("volume", mic_id, round(scalar, 2)))
        if self.fail_with:
            raise self.fail_with
        if not self.ignore_writes:
            m = self.mics[mic_id]
            self.mics[mic_id] = Microphone(m.id, m.name, m.is_default, m.muted, round(scalar * 100))


ARRAY = Microphone(
    "id-array", "Microphone Array (Cirrus Logic)", True, True, 58
)  # el caso real del usuario: predeterminado y SILENCIADO
HEADSET = Microphone("id-headset", "Microphone (Logi USB Headset)", False, False, 80)
BLUETOOTH = Microphone("id-bt", "Headset (JBL WAVE BUDS)", False, False, 100)
NO_CONTROL = Microphone("id-raro", "Micrófono con driver raro", False, None, None)


@pytest.fixture
def fake():
    backend = FakeAudio([HEADSET, ARRAY, BLUETOOTH, NO_CONTROL])
    audio_control.set_backend(backend)
    yield backend
    audio_control.set_backend(None)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: listar
# ---------------------------------------------------------------------------------------------------------------------
class TestListing:
    def test_the_default_microphone_comes_first_then_the_rest_by_name(self, fake):
        names = [m.name for m in audio_control.list_microphones()]

        assert names[0] == "Microphone Array (Cirrus Logic)"
        assert names[1:] == sorted(names[1:], key=str.lower)

    def test_each_microphone_reports_name_default_muted_and_volume(self, fake):
        by_id = {m.id: m.as_dict() for m in audio_control.list_microphones()}

        assert by_id["id-array"] == {
            "id": "id-array",
            "name": "Microphone Array (Cirrus Logic)",
            "is_default": True,
            "muted": True,
            "volume": 58,
        }
        assert by_id["id-headset"]["muted"] is False and by_id["id-headset"]["volume"] == 80

    def test_a_microphone_whose_driver_does_not_expose_its_state_is_unknown_never_muted(self, fake):
        """REQ-009: «no se pudo leer» no es «silenciado». Si lo fuera, Vision le diria a la persona que active un micro que quiza ya anda."""
        raro = next(m for m in audio_control.list_microphones() if m.id == "id-raro")

        assert raro.muted is None and raro.volume is None

    def test_with_no_microphones_the_list_is_empty_not_an_error(self):
        audio_control.set_backend(FakeAudio([]))
        try:
            assert audio_control.list_microphones() == []
        finally:
            audio_control.set_backend(None)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002: activar
# ---------------------------------------------------------------------------------------------------------------------
class TestActivate:
    def test_unmuting_leaves_the_volume_exactly_as_it_was(self, fake):
        result = audio_control.set_mute("id-array", False)

        assert result.muted is False and result.volume == 58
        assert fake.writes == [("mute", "id-array", False)]  # UNA sola escritura: no se toco el volumen

    def test_muting_leaves_the_volume_as_it_was(self, fake):
        result = audio_control.set_mute("id-headset", True)

        assert result.muted is True and result.volume == 80

    def test_it_never_changes_which_microphone_is_the_windows_default(self, fake):
        """Decision D1 del usuario: activar solo desilencia."""
        audio_control.set_mute("id-headset", False)
        audio_control.set_volume("id-bluetooth-no-existe-no-importa" if False else "id-bt", 30)

        assert [m.id for m in audio_control.list_microphones() if m.is_default] == ["id-array"]

    def test_a_driver_that_says_ok_but_does_not_apply_the_change_is_reported(self, fake):
        """La escritura se RELEE: nunca se afirma algo que Windows no aplico."""
        fake.ignore_writes = True

        with pytest.raises(AudioControlError, match="no aplicó el cambio"):
            audio_control.set_mute("id-array", False)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-003 / REQ-004: volumen
# ---------------------------------------------------------------------------------------------------------------------
class TestVolume:
    def test_a_volume_from_1_to_100_sets_the_level_and_removes_the_mute(self, fake):
        result = audio_control.set_volume("id-array", 40)  # estaba silenciado a 58

        assert result.volume == 40 and result.muted is False

    def test_zero_mutes_and_keeps_the_previous_level(self, fake):
        """Windows NO silencia solo al bajar a 0: lo hace Vision. Y conserva el nivel para que «activar» despues no deje un micro activo pero mudo."""
        result = audio_control.set_volume("id-headset", 0)

        assert result.muted is True and result.volume == 80
        assert ("volume", "id-headset", 0.0) not in fake.writes  # jamas se escribe un nivel 0

    def test_raising_it_from_zero_unmutes(self, fake):
        audio_control.set_volume("id-headset", 0)

        result = audio_control.set_volume("id-headset", 25)

        assert result.muted is False and result.volume == 25

    @pytest.mark.parametrize(("asked", "expected"), [(150, 100), (-5, 0), (100, 100), (1, 1)])
    def test_out_of_range_values_are_clamped(self, fake, asked, expected):
        result = audio_control.set_volume("id-bt", asked)

        assert (result.volume == expected) if expected else (result.muted is True)

    def test_a_volume_change_that_windows_did_not_apply_is_reported(self, fake):
        fake.ignore_writes = True

        with pytest.raises(AudioControlError, match="no aplicó el cambio de volumen"):
            audio_control.set_volume("id-array", 40)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-006 / REQ-007 / REQ-008: id correcto, fallas y «no tocar lo que no se pidio»
# ---------------------------------------------------------------------------------------------------------------------
class TestSafety:
    def test_only_the_asked_microphone_is_written_never_a_namesake(self, fake):
        fake.mics["id-headset-2"] = Microphone(
            "id-headset-2", HEADSET.name, False, False, 55
        )  # mismo nombre, otro dispositivo

        audio_control.set_volume("id-headset", 10)

        assert {w[1] for w in fake.writes} == {"id-headset"}
        assert fake.mics["id-headset-2"].volume == 55

    def test_a_microphone_that_was_unplugged_is_refused_before_writing_anything(self, fake):
        with pytest.raises(AudioControlError, match="ya no está conectado"):
            audio_control.set_mute("id-que-se-desenchufo", False)

        assert fake.writes == []

    def test_a_volume_change_on_an_unplugged_microphone_is_refused_before_writing_anything(self, fake):
        """Mismo resguardo que al silenciar: si el micro se desenchufo entre que se abrio la pantalla y el movimiento del deslizador, no se escribe nada."""
        with pytest.raises(AudioControlError, match="ya no está conectado"):
            audio_control.set_volume("id-que-se-desenchufo", 40)

        assert fake.writes == []

    def test_reading_never_writes(self, fake):
        """REQ-008: listar (que es lo que hace Vision al abrir Audio o un modo) no modifica ningun dispositivo."""
        audio_control.list_microphones()
        audio_control.list_microphones()

        assert fake.writes == []

    def test_a_microphone_without_volume_control_gives_a_plain_message_not_a_crash(self, fake):
        fake.fail_with = AudioControlError(
            "Este micrófono no deja que ULTRON cambie su volumen. Usá el Panel de control de Windows."
        )

        with pytest.raises(AudioControlError, match="Panel de control"):
            audio_control.set_volume("id-raro", 50)

    def test_a_system_failure_becomes_a_message_for_a_person_without_codes(self, fake):
        fake.fail_with = OSError("SetMute fallo: HRESULT 0x80070005")

        with pytest.raises(AudioControlError) as info:
            audio_control.list_microphones()

        message = str(info.value)
        assert "HRESULT" not in message and "0x" not in message  # el detalle tecnico va al log, no a la pantalla
        assert "Panel de control" in message

    def test_a_system_that_is_not_windows_says_so_in_plain_words(self, monkeypatch):
        audio_control.set_backend(None)
        monkeypatch.setattr(audio_control.sys, "platform", "linux")

        with pytest.raises(audio_control.AudioUnsupported, match="solo está disponible en Windows"):
            audio_control.list_microphones()


# ---------------------------------------------------------------------------------------------------------------------
# Capa REAL (ctypes + Core Audio): SOLO LECTURA. Nunca escribe: la suite no puede cambiar el audio de quien la corre.
# Se saltea sola fuera de Windows o sin microfonos conectados. Lo que NO cubre (escribir de verdad) se probo a mano contra el hardware del usuario.
# ---------------------------------------------------------------------------------------------------------------------
class TestRealWindowsReadOnly:
    @pytest.fixture
    def real(self):
        import sys

        if sys.platform != "win32":
            pytest.skip("Core Audio solo existe en Windows")
        audio_control.set_backend(None)
        try:
            mics = audio_control.list_microphones()
        except AudioControlError as exc:  # sin servicio de audio (maquina virtual, servicio apagado)
            pytest.skip(f"este equipo no expone Core Audio: {exc}")
        if not mics:
            pytest.skip("este equipo no tiene microfonos conectados")
        yield mics
        audio_control.set_backend(None)

    def test_the_real_list_has_the_expected_shape(self, real):
        for mic in real:
            assert mic.id and mic.name
            assert mic.muted is None or isinstance(mic.muted, bool)
            assert mic.volume is None or 0 <= mic.volume <= 100
            assert (mic.muted is None) == (mic.volume is None)  # o se pudo leer el estado entero o no se pudo nada: nunca a medias

    def test_at_most_one_microphone_is_the_default_and_it_comes_first(self, real):
        defaults = [i for i, m in enumerate(real) if m.is_default]

        assert len(defaults) <= 1
        assert defaults in ([], [0])

    def test_listing_twice_gives_the_same_result_and_leaks_nothing(self, real):
        first = [m.as_dict() for m in audio_control.list_microphones()]
        for _ in range(20):  # 20 aperturas de COM seguidas: si faltara un Release/CoUninitialize esto se degrada o se cuelga
            audio_control.list_microphones()

        assert [m.as_dict() for m in audio_control.list_microphones()] == first
