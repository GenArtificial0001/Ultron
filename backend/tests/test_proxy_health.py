"""Proxy autocorregible (spec proxy-autoheal REQ-001..005, REQ-009, REQ-011): al arrancar Vision verifica su salida a internet ANTES de usarla y, si la guardada murio, elige otra viva.

Incidente real (2026-10-18): la base cifrada guardaba `http://19.12.64.237:83`, un egress de la empresa que quedo caido, y el Vision instalado no cargaba los modelos. Aca el mundo es falso
(`World`: que direcciones responden, que devuelve el DNS, que dice la PAC): la suite no sale a la red. Lo que se vigila:
1. Orden de candidatos: guardado -> DNS -> PAC -> directo, sin repetidos; el listado NO sale de una lista fija en el codigo.
2. Guardado vivo: no se toca nada (ni env ni base). Guardado muerto: se cura en el env Y en la base (D1). Sin guardado: la base no se escribe nunca.
3. Un nombre que rota entre IPs (el DNS de la empresa) solo cuenta como sano si TODAS sus IPs responden.
4. Ningun proxy responde pero la conexion directa si: se sale sin proxy. Nada responde: aviso claro, la base queda como estaba.
5. Una variable de entorno del SISTEMA manda: no se prueba ni se cambia nada.
6. Nunca lanza, y los avisos pasan el guardian de jerga.
7. La vigilancia del arranque (REQ-011) se cura cuando aparece un egress, y corta a los 5 minutos."""

from __future__ import annotations

import os
import re

import pytest

from app import config
from app import config_store as cs
from app import general_settings as gs
from app import proxy_health as ph
from app import secret_store
from tests.test_friendly_errors import _JARGON

pytestmark = pytest.mark.skipif(not secret_store.is_available(), reason="DPAPI solo existe en Windows")

DEAD = "http://19.12.64.237:83"
LIVE = "http://19.12.80.237:83"


class World:
    """Lo que ve el modulo: que (host, puerto) responden, que IPs da cada nombre y que proxies dice la PAC."""

    def __init__(self) -> None:
        self.alive: set[tuple[str, int]] = set()
        self.dns: dict[str, list[str]] = {}
        self.pac: list[str] = []
        self.probed: list[tuple[str, int]] = []

    def tcp_alive(self, host: str, port: int) -> float | None:
        self.probed.append((host, port))
        return 0.01 if (host, port) in self.alive else None

    def resolve(self, host: str) -> list[str]:
        return list(self.dns.get(host, []))

    def pac_proxies(self) -> list[str]:
        return list(self.pac)


@pytest.fixture(autouse=True)
def _a_company_with_its_own_default_proxy(monkeypatch):
    """ULTRON viene SIN proxy predeterminado (`DEFAULT_PROXY = ""`). Estos tests, heredados de la version de origen, prueban la logica
    completa de la cura con una empresa que SI tiene uno (y sus excepciones): se simula esa empresa. Lo de fabrica lo prueban
    `test_ultron_ships_without_a_default_proxy*` al final."""
    monkeypatch.setattr(ph, "DEFAULT_PROXY", "http://proxy.empresa.com:83")
    monkeypatch.setattr(ph, "_FALLBACK_NO_PROXY", ".empresa.com,empresa.com,19.0.0.0/8,10.0.0.0/8")
    yield


@pytest.fixture
def world(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(env_file))
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_VISION_SET_ENV", {})
    cs.invalidate()
    config.get_settings.cache_clear()
    w = World()
    monkeypatch.setattr(ph, "_tcp_alive", w.tcp_alive)
    monkeypatch.setattr(ph, "_resolve", w.resolve)
    monkeypatch.setattr(ph, "_pac_proxies", w.pac_proxies)
    ph.reset_for_tests()
    yield w
    ph.reset_for_tests()
    cs.invalidate()
    config.get_settings.cache_clear()


def _save(**fields) -> None:
    gs.save(fields)
    config.get_settings.cache_clear()
    config.get_settings()


def _stored(key: str) -> str:
    return cs.get_general().get(key, "")


# ---------------------------------------------------------------------------------------------------------------------
# Direcciones
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://19.12.80.237:83", "http://19.12.80.237:83"),
        ("19.12.80.237:83", "http://19.12.80.237:83"),
        (" http://Proxy.Empresa.com:83/ ", "http://proxy.empresa.com:83"),
        ("http://proxy.empresa.com", "http://proxy.empresa.com:80"),
        ("https://proxy.example.com", "https://proxy.example.com:443"),
    ],
)
def test_addresses_are_normalized_to_one_form(raw: str, expected: str):
    assert ph.normalize(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "ftp://x:21", "http://", "http://host:notaport", "http://host:99999", "host:0", None]
)
def test_garbage_addresses_are_not_candidates(raw):
    assert ph.normalize(raw) is None


# ---------------------------------------------------------------------------------------------------------------------
# REQ-001: el listado se arma en vivo, en orden fijo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_list_is_saved_then_dns_then_pac_then_direct_without_repeats(world: World):
    world.dns["proxy.empresa.com"] = ["19.12.80.237", "19.12.64.237"]
    world.pac = ["19.12.80.237:83", "10.1.1.1:83"]

    cands = ph.build_candidates("http://19.12.64.237:83")

    assert [(c.source, c.url) for c in cands] == [
        ("guardado", "http://19.12.64.237:83"),
        ("dns", "http://19.12.80.237:83"),
        ("pac", "http://10.1.1.1:83"),
        ("directo", ""),
    ]


def test_without_a_saved_proxy_the_empresa_default_name_goes_first(world: World):
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]

    cands = ph.build_candidates("")

    assert [(c.source, c.url) for c in cands] == [
        ("predeterminado", "http://proxy.empresa.com:83"),
        ("dns", "http://19.12.80.237:83"),
        ("directo", ""),
    ]


def test_the_candidates_come_from_dns_and_the_pac_not_from_a_list_in_the_code(world: World):
    """Si la empresa cambia sus egress, Vision los sigue sin tocar el codigo: con DNS y PAC vacios solo quedan el guardado y directo."""
    cands = ph.build_candidates("http://10.9.9.9:83")

    assert [c.source for c in cands] == ["guardado", "directo"]
    assert not any("19.12" in c.url for c in cands)


def test_a_dns_or_pac_that_fails_does_not_break_the_list(world: World, monkeypatch):
    def boom(*_a):
        raise OSError("sin DNS")

    monkeypatch.setattr(ph, "_resolve", boom)
    monkeypatch.setattr(ph, "_pac_proxies", boom)

    cands = ph.build_candidates("http://10.9.9.9:83")

    assert [c.source for c in cands] == ["guardado", "directo"]


# ---------------------------------------------------------------------------------------------------------------------
# REQ-002/003: guardado vivo no se toca; guardado muerto se cura en el env y en la base
# ---------------------------------------------------------------------------------------------------------------------
def test_a_live_saved_proxy_is_left_exactly_as_it_is(world: World):
    _save(https_proxy=LIVE)
    world.alive = {("19.12.80.237", 83)}
    before = dict(cs.get_general())
    env_before = os.environ.get("HTTPS_PROXY")

    res = ph.check()

    assert res.action == "ok" and res.message == ""
    assert cs.get_general() == before and os.environ.get("HTTPS_PROXY") == env_before == LIVE


def test_a_dead_saved_proxy_is_healed_in_the_session_and_in_the_database(world: World):
    _save(https_proxy=DEAD, no_proxy=".empresa.com")
    world.dns["proxy.empresa.com"] = ["19.12.80.237", "19.12.64.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert res.action == "healed" and res.chosen is not None and res.chosen.url == LIVE
    assert _stored("https_proxy") == LIVE, "la cura queda guardada (D1)"
    assert os.environ["HTTPS_PROXY"] == LIVE, "y vale ya, sin reiniciar"
    assert "19.12.64.237" in res.message and "19.12.80.237" in res.message


def test_the_cure_keeps_the_saved_no_proxy_list(world: World):
    _save(https_proxy=DEAD, no_proxy=".empresa.com")
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()

    assert _stored("no_proxy") == ".empresa.com" and ".empresa.com" in os.environ["NO_PROXY"]


def test_the_saved_http_proxy_is_updated_only_if_it_was_the_dead_one(world: World):
    _save(https_proxy=DEAD, http_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()

    assert _stored("http_proxy") == LIVE


def test_a_different_saved_http_proxy_is_not_touched(world: World):
    _save(https_proxy=DEAD, http_proxy="http://10.2.2.2:8080")
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()

    assert _stored("http_proxy") == "http://10.2.2.2:8080"
    assert os.environ["HTTP_PROXY"] == "http://10.2.2.2:8080", "tampoco en esta sesion (H5)"


def test_the_pac_can_provide_the_live_one(world: World):
    _save(https_proxy=DEAD)
    world.pac = ["10.5.5.5:83"]
    world.alive = {("10.5.5.5", 83)}

    res = ph.check()

    assert res.action == "healed" and _stored("https_proxy") == "http://10.5.5.5:83"


def test_persist_false_heals_the_session_but_writes_nothing(world: World):
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check(persist=False)

    assert res.action == "healed" and os.environ["HTTPS_PROXY"] == LIVE
    assert _stored("https_proxy") == DEAD


# ---------------------------------------------------------------------------------------------------------------------
# Sin proxy guardado: la base no se escribe nunca
# ---------------------------------------------------------------------------------------------------------------------
def test_with_no_saved_proxy_and_a_healthy_default_nothing_changes(world: World):
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert res.action == "ok"
    assert "HTTPS_PROXY" not in os.environ and not cs.get_general().get("https_proxy")


def test_with_no_saved_proxy_a_default_that_rotates_to_a_dead_ip_is_replaced_for_the_session_only(world: World):
    """El DNS de la empresa alterna la IP viva y la muerta: el nombre solo es sano si todas sus IPs responden."""
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert res.action == "chosen" and os.environ["HTTPS_PROXY"] == LIVE
    assert not cs.get_general().get("https_proxy"), "no habia nada guardado: no se escribe nada"


def test_a_saved_hostname_that_rotates_to_a_dead_ip_is_healed_to_the_live_ip(world: World):
    _save(https_proxy="http://proxy.empresa.com:83")
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert res.action == "healed" and _stored("https_proxy") == LIVE


def test_a_saved_hostname_with_all_ips_alive_is_left_alone(world: World):
    _save(https_proxy="http://proxy.empresa.com:83")
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    world.alive = {("19.12.80.237", 83), ("19.12.64.237", 83)}

    res = ph.check()

    assert res.action == "ok" and _stored("https_proxy") == "http://proxy.empresa.com:83"


# ---------------------------------------------------------------------------------------------------------------------
# Directo y todo caido
# ---------------------------------------------------------------------------------------------------------------------
def test_when_no_proxy_answers_but_the_direct_connection_does_it_goes_direct(world: World):
    _save(https_proxy=DEAD)
    world.alive = {("login.microsoftonline.com", 443)}

    res = ph.check()

    assert res.action == "direct"
    assert "HTTPS_PROXY" in os.environ and os.environ["HTTPS_PROXY"] == "", (
        "vacio explicito: el SDK del proveedor de IA no pone el suyo"
    )
    # 0.33.1: salir directo vale SOLO para esta sesion. El proxy guardado se CONSERVA: puede ser el unico valido en otra red (en la empresa sin VPN) y borrarlo obligaria a cargarlo de nuevo.
    assert _stored("https_proxy") == DEAD, "el proxy guardado NO se borra: puede servir en otra red"
    assert res.persisted is False
    assert "se conserva" in res.message.lower() or "conserv" in res.message.lower()


def test_going_direct_keeps_the_saved_proxy_so_the_next_start_can_use_it_again(world: World):
    """Casa con la VPN corporativa hoy, oficina de la empresa manana: lo guardado tiene que seguir ahi cuando la red cambie."""
    _save(https_proxy=DEAD, no_proxy=".empresa.com")
    world.alive = {("login.microsoftonline.com", 443)}
    ph.check()
    assert os.environ["HTTPS_PROXY"] == ""

    # al dia siguiente, otra red: el guardado vuelve a responder
    ph.reset_for_tests()
    config.get_settings.cache_clear()
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.pop(var, None)
    config._VISION_SET_ENV.clear()
    config.get_settings()  # el arranque carga la configuracion: lo guardado se aplica al entorno, como siempre
    world.alive = {("19.12.64.237", 83)}
    res = ph.check()

    assert res.action == "ok" and _stored("https_proxy") == DEAD
    assert os.environ["HTTPS_PROXY"] == DEAD, "el proxy conservado se usa apenas responde"


def test_a_live_proxy_always_beats_direct(world: World):
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83), ("login.microsoftonline.com", 443)}

    assert ph.check().action == "healed"


def test_direct_without_a_saved_proxy_is_for_the_session_only(world: World):
    world.dns["proxy.empresa.com"] = ["19.12.64.237"]
    world.alive = {("login.microsoftonline.com", 443)}

    res = ph.check()

    assert res.action == "direct" and os.environ["HTTPS_PROXY"] == ""
    assert not cs.get_general().get("https_proxy")


def test_when_nothing_answers_it_says_so_and_changes_nothing(world: World):
    _save(https_proxy=DEAD)

    res = ph.check()

    assert res.action == "none" and res.chosen is None
    assert _stored("https_proxy") == DEAD, "sin donde ir, no se pierde lo guardado"
    assert "VPN" in res.message


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009: lo del sistema manda
# ---------------------------------------------------------------------------------------------------------------------
def test_a_proxy_set_by_the_system_is_never_probed_or_changed(world: World, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.7.7.7:8080")

    res = ph.check()

    assert res.action == "system" and world.probed == []
    assert os.environ["HTTPS_PROXY"] == "http://10.7.7.7:8080"


def test_the_sdk_default_in_the_environment_is_not_mistaken_for_the_systems(world: World, monkeypatch):
    """El SDK del proveedor de IA pone `http://proxy.empresa.com:83` si no hay nada: eso NO es una decision del sistema."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.empresa.com:83")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.empresa.com:83")
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert res.action == "chosen" and os.environ["HTTPS_PROXY"] == LIVE and os.environ["HTTP_PROXY"] == LIVE


# ---------------------------------------------------------------------------------------------------------------------
# REQ-005: nunca lanza ni bloquea
# ---------------------------------------------------------------------------------------------------------------------
def test_a_failure_inside_the_check_never_escapes(world: World, monkeypatch):
    def boom(*_a):
        raise RuntimeError("socket roto")

    monkeypatch.setattr(ph, "_tcp_alive", boom)

    res = ph.check()

    assert res.action in {"none", "error"}
    assert "Traceback" not in res.message and "socket roto" not in res.message


def test_a_probe_that_hangs_is_cut_at_the_deadline(world: World, monkeypatch):
    import time

    def slow(host, port):
        time.sleep(30)
        return 0.0

    monkeypatch.setattr(ph, "_tcp_alive", slow)
    monkeypatch.setattr(ph, "DEADLINE", 0.3)

    started = time.monotonic()
    res = ph.check()

    assert time.monotonic() - started < 3 and res.action == "none"


# ---------------------------------------------------------------------------------------------------------------------
# REQ-004: los avisos son para personas
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["healed", "direct", "none", "chosen"])
def test_every_notice_is_plain_words(world: World, scenario: str):
    if scenario in {"healed", "direct", "none"}:
        _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    if scenario in {"healed", "chosen"}:
        world.alive = {("19.12.80.237", 83)}
    if scenario == "direct":
        world.alive = {("login.microsoftonline.com", 443)}

    res = ph.check()

    assert res.action == scenario and res.message
    assert not _JARGON.search(res.message), res.message
    assert res.message.endswith(".") and "\n" not in res.message


# ---------------------------------------------------------------------------------------------------------------------
# Foto para la pantalla
# ---------------------------------------------------------------------------------------------------------------------
def test_the_snapshot_lists_every_candidate_with_its_state(world: World):
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()
    snap = ph.snapshot()

    by_url = {c["url"]: c for c in snap["candidates"]}
    assert by_url[DEAD]["alive"] is False and by_url[LIVE]["alive"] is True
    assert snap["action"] == "healed" and snap["saved"] == LIVE and snap["in_use"] == LIVE


def test_the_snapshot_before_any_check_is_empty_but_valid():
    snap = ph.snapshot()

    assert snap["candidates"] == [] and snap["action"] is None


def test_probe_one_tests_a_single_address_and_changes_nothing(world: World):
    world.alive = {("10.1.1.1", 83)}

    live = ph.probe_one("http://10.1.1.1:83")
    dead = ph.probe_one("10.9.9.9:83")

    assert live["alive"] is True and dead["alive"] is False
    assert "HTTPS_PROXY" not in os.environ and not cs.get_general().get("https_proxy")


def test_probe_one_rejects_an_unusable_address(world: World):
    with pytest.raises(ValueError):
        ph.probe_one("esto no es una direccion")


# ---------------------------------------------------------------------------------------------------------------------
# «Guardar y reconectar» (REQ-007)
# ---------------------------------------------------------------------------------------------------------------------
def test_apply_choice_saves_and_applies_a_live_address(world: World):
    cs.set_general({"llm_model": "gpt-x"})  # base cifrada activa (ya se importo)
    world.alive = {("10.1.1.1", 83)}

    out = ph.apply_choice("http://10.1.1.1:83")

    assert out == {"applied": True, "saved": True, "alive": True} and _stored("https_proxy") == "http://10.1.1.1:83"
    assert os.environ["HTTPS_PROXY"] == "http://10.1.1.1:83"


def test_apply_choice_refuses_a_dead_address_unless_forced(world: World):
    cs.set_general({"llm_model": "gpt-x"})

    refused = ph.apply_choice("http://10.9.9.9:83")
    assert refused["applied"] is False and not _stored("https_proxy")

    forced = ph.apply_choice("http://10.9.9.9:83", force=True)
    assert forced["applied"] is True and _stored("https_proxy") == "http://10.9.9.9:83"


def test_apply_choice_with_the_store_still_inactive_applies_to_the_session_but_says_it_was_not_saved(world: World):
    """Activar la base por esto ocultaria un `.env` sin importar: primero se importa en Conexiones."""
    world.alive = {("10.1.1.1", 83)}

    out = ph.apply_choice("http://10.1.1.1:83")

    assert out == {"applied": True, "saved": False, "alive": True}
    assert os.environ["HTTPS_PROXY"] == "http://10.1.1.1:83" and not cs.is_active()


def test_apply_choice_dead_leaves_the_saved_value_untouched(world: World):
    _save(https_proxy=LIVE)

    out = ph.apply_choice("http://10.9.9.9:83")

    assert out["applied"] is False and _stored("https_proxy") == LIVE


def test_apply_choice_empty_means_direct(world: World):
    _save(https_proxy=DEAD)
    world.alive = {("login.microsoftonline.com", 443)}

    out = ph.apply_choice("")

    assert out["applied"] is True and _stored("https_proxy") == "" and os.environ["HTTPS_PROXY"] == ""
    # elegir «directo» a mano SI es una decision de la persona: ahi si se guarda vacio (el unico camino que borra el proxy)


# ---------------------------------------------------------------------------------------------------------------------
# REQ-011: la vigilancia del arranque
# ---------------------------------------------------------------------------------------------------------------------
def test_the_watch_heals_when_a_proxy_shows_up_later(world: World):
    """La VPN conecta DESPUES de abrir Vision (llegada a la oficina): el vigilante lo cura apenas hay un egress vivo."""
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            world.alive = {("19.12.80.237", 83)}  # aparece en la tercera espera

    res = ph.watch_loop(interval=20, max_seconds=300, sleep=sleep, clock=lambda: float(len(sleeps) * 20))

    assert res.action == "healed" and len(sleeps) == 3 and _stored("https_proxy") == LIVE


def test_the_watch_gives_up_after_the_limit(world: World):
    _save(https_proxy=DEAD)
    sleeps: list[float] = []

    res = ph.watch_loop(interval=20, max_seconds=300, sleep=sleeps.append, clock=lambda: float(len(sleeps) * 20))

    assert res.action == "none" and len(sleeps) == 15
    assert _stored("https_proxy") == DEAD


def test_the_watch_stops_at_once_if_the_first_check_was_fine(world: World):
    _save(https_proxy=LIVE)
    world.alive = {("19.12.80.237", 83)}
    sleeps: list[float] = []

    res = ph.watch_loop(interval=20, max_seconds=300, sleep=sleeps.append, clock=lambda: 0.0)

    assert res.action == "ok" and sleeps == []


def test_only_one_watch_runs_at_a_time(world: World, monkeypatch):
    started: list[object] = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            started.append(target)

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(ph.threading, "Thread", FakeThread)

    assert ph.start_watch() is True
    assert ph.start_watch() is False and len(started) == 1
    assert ph.watching() is True


# ---------------------------------------------------------------------------------------------------------------------
# Que no se cuele jerga ni texto crudo en el modulo
# ---------------------------------------------------------------------------------------------------------------------
def test_the_module_never_prints_or_logs_a_password_shaped_proxy(world: World, caplog):
    _save(https_proxy="http://usuario:clave-secreta@19.12.64.237:83")
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    with caplog.at_level("DEBUG"):
        res = ph.check()
    snap = str(ph.snapshot())

    assert "clave-secreta" not in res.message and "clave-secreta" not in caplog.text and "clave-secreta" not in snap


_ = re  # el guardian de jerga se importa de test_friendly_errors y usa `re`


# ---------------------------------------------------------------------------------------------------------------------
# Huecos que encontraron las mutaciones (D7, H2, H6, H9, H10, A1, W5)
# ---------------------------------------------------------------------------------------------------------------------
def test_a_check_that_ran_out_of_time_never_applies_anything_afterwards(world: World, monkeypatch):
    """Pasado el plazo el chequeo se da por perdido: el hilo que sigue vivo NO puede cambiar el entorno despues (D7)."""
    import threading
    import time

    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    gate = threading.Event()

    def slow(host, port):
        gate.wait(5)
        return 0.01 if (host, port) == ("19.12.80.237", 83) else None

    monkeypatch.setattr(ph, "_tcp_alive", slow)
    monkeypatch.setattr(ph, "DEADLINE", 0.3)

    res = ph.check()
    gate.set()  # ahora las pruebas "terminan": todas dicen que 19.12.80.237 esta vivo
    time.sleep(0.5)

    assert res.action == "none"
    assert os.environ.get("HTTPS_PROXY") != LIVE and _stored("https_proxy") == DEAD


def test_with_the_store_active_but_no_saved_proxy_the_cure_writes_nothing(world: World):
    """Hay base cifrada con otras cosas guardadas, pero ningun proxy: la sesion se cura y la base queda igual (H2)."""
    cs.set_general({"llm_model": "gpt-x"})
    world.dns["proxy.empresa.com"] = ["19.12.64.237", "19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}
    before = dict(cs.get_general())

    res = ph.check()

    assert res.action == "chosen" and os.environ["HTTPS_PROXY"] == LIVE
    assert cs.get_general() == before


def test_a_cure_to_a_proxy_also_leaves_the_internal_empresa_hosts_going_direct(world: World):
    """Sin lista de excepciones, lo interno de la empresa (el propio el proveedor de IA) saldria por el proxy: la cura la completa (H6)."""
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()

    assert "empresa.com" in _stored("no_proxy")
    assert "empresa.com" in os.environ["NO_PROXY"] and "127.0.0.1" in os.environ["NO_PROXY"]


def test_the_login_in_the_saved_address_is_kept_when_the_proxy_changes_and_never_shown(world: World, capsys):
    """Un proxy con usuario y clave: al curar se cambia SOLO el equipo; las credenciales se conservan, pero nunca se muestran (H9, A1)."""
    _save(https_proxy="http://usuario:clave-secreta@19.12.64.237:83")
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    res = ph.check()

    assert _stored("https_proxy") == "http://usuario:clave-secreta@19.12.80.237:83"
    assert os.environ["HTTPS_PROXY"] == "http://usuario:clave-secreta@19.12.80.237:83"
    shown = capsys.readouterr()
    assert "clave-secreta" not in res.message and "clave-secreta" not in shown.out + shown.err
    assert "clave-secreta" not in str(ph.snapshot())


def test_a_failure_while_saving_never_undoes_the_cure_of_the_session(world: World, monkeypatch):
    """Si la base no se puede escribir, esta sesion igual queda curada y el aviso lo dice (H10)."""
    _save(https_proxy=DEAD)
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    def boom(_updates):
        raise OSError("disco lleno C:\\interno")

    monkeypatch.setattr(gs, "save", boom)

    res = ph.check()

    assert res.action == "healed" and res.persisted is False
    assert os.environ["HTTPS_PROXY"] == LIVE
    assert "durante esta sesión" in res.message and "interno" not in res.message


def test_the_watch_thread_is_a_daemon_so_it_never_keeps_vision_open(world: World, monkeypatch):
    """Un hilo comun mantendria el proceso vivo al cerrar Vision hasta que termine la vigilancia (W5)."""
    seen: dict = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            seen["daemon"] = daemon

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(ph.threading, "Thread", FakeThread)

    assert ph.start_watch() is True and seen["daemon"] is True


def test_a_dns_lookup_that_hangs_past_the_deadline_cannot_apply_anything_when_it_wakes_up(world: World, monkeypatch):
    """El hilo abandonado por el plazo sigue vivo: si el DNS despierta DESPUES, ya no puede tocar el entorno ni la base (D7)."""
    import threading
    import time

    _save(https_proxy=DEAD)
    gate = threading.Event()

    def slow_dns(host):
        gate.wait(5)
        return ["19.12.80.237"]

    monkeypatch.setattr(ph, "_resolve", slow_dns)
    world.alive = {("19.12.80.237", 83)}
    monkeypatch.setattr(ph, "DEADLINE", 0.3)

    res = ph.check()
    gate.set()  # el DNS por fin contesta: ahora todo "responde"
    time.sleep(0.6)

    assert res.action == "none"
    assert os.environ.get("HTTPS_PROXY") != LIVE and _stored("https_proxy") == DEAD


@pytest.mark.parametrize(
    "saved", ["http://usuario:clave-secreta@19.12.64.237:99999", "usuario:clave-secreta@host con espacios:83"]
)
def test_a_saved_address_that_is_not_valid_is_replaced_without_showing_or_choking_on_its_password(
    world: World, saved: str, caplog
):
    """Un valor guardado a mano y roto (con clave) no puede tirar el aviso ni salir en pantalla: se reemplaza por uno vivo."""
    cs.set_general({"https_proxy": saved})
    config.get_settings.cache_clear()
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    with caplog.at_level("DEBUG"):
        res = ph.check()

    assert res.action == "healed", res.message
    assert (
        "clave-secreta" not in res.message
        and "clave-secreta" not in caplog.text
        and "clave-secreta" not in str(ph.snapshot())
    )
    assert os.environ["HTTPS_PROXY"] == "http://usuario:clave-secreta@19.12.80.237:83", (
        "se cambia el equipo y se conserva el login"
    )


def test_the_persons_own_no_proxy_list_is_never_replaced_by_the_cure(world: World):
    """Si la persona ya escribio sus excepciones, la cura no las pisa con la lista del SDK (H6b)."""
    _save(https_proxy=DEAD, no_proxy=".midominio.com")
    world.dns["proxy.empresa.com"] = ["19.12.80.237"]
    world.alive = {("19.12.80.237", 83)}

    ph.check()

    assert _stored("no_proxy") == ".midominio.com"
    assert ".midominio.com" in os.environ["NO_PROXY"] and "googleapis" not in os.environ["NO_PROXY"]


def test_the_snapshot_explains_why_the_saved_proxy_is_not_the_one_in_use(world: World):
    """Guardado muerto + salida directa: la pantalla muestra dos valores distintos, y tiene que decir por que no es un error."""
    _save(https_proxy=DEAD)
    world.alive = {("login.microsoftonline.com", 443)}
    ph.check()

    snap = ph.snapshot()

    assert snap["saved"] == DEAD and snap["in_use"] == ""
    assert snap["kept_saved"] is True


def test_the_snapshot_has_no_kept_flag_when_saved_and_in_use_agree(world: World):
    _save(https_proxy=LIVE)
    world.alive = {("19.12.80.237", 83)}
    ph.check()

    assert ph.snapshot()["kept_saved"] is False


# ---------------------------------------------------------------------------------------------------------------------
# La lectura REAL de la PAC de Windows (bug grave de la 0.33.0/0.33.1: cerraba el servidor a los ~6 s con 0xc0000374)
# ---------------------------------------------------------------------------------------------------------------------
# Todos los tests de arriba reemplazan `_pac_proxies` por un falso: ninguno ejecutaba `_ask_windows_pac`. Ahi se liberaba con `GlobalFree` un puntero que `ctypes` ya habia convertido a
# `str` (por declarar el campo como `LPWSTR`): corrupcion del heap, y el proceso moria SIN traceback. Solo se ve ejecutandolo de verdad, y en un proceso APARTE: si la memoria se
# corrompe, mata al proceso que lo ejecuta, y un test comun se llevaria puesto a pytest entero.
_PAC_CHILD = """
import ctypes, json, sys
sys.path.insert(0, {root!r})
events = []
_real_wstring_at = ctypes.wstring_at
def _spy_read(*args, **kwargs):
    events.append(["read"])
    return _real_wstring_at(*args, **kwargs)
ctypes.wstring_at = _spy_read
_RealWinDLL = ctypes.WinDLL
class _Spy:
    def __init__(self, real):
        object.__setattr__(self, "_real", real)
    def __setattr__(self, name, value):
        setattr(self._real, name, value)
    def __call__(self, *args):
        result = self._real(*args)
        events.append(["free", result])
        return result
class _K32:
    def __init__(self, real):
        self._real = real
        self.GlobalFree = _Spy(real.GlobalFree)
    def __getattr__(self, name):
        return getattr(self._real, name)
def _windll(name, *args, **kwargs):
    real = _RealWinDLL(name, *args, **kwargs)
    return _K32(real) if str(name).lower() == "kernel32" else real
ctypes.WinDLL = _windll
from app import proxy_health as ph
got = None
for _ in range(30):
    got = ph._ask_windows_pac("http://127.0.0.1:{port}/p.pac", "https://login.microsoftonline.com/")
print("RESULTADO", got)
print("EVENTOS", json.dumps(events))
"""


def _serve_pac(body: bytes):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- nombre fijado por la libreria
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kwargs):  # sin ruido en la salida de pytest
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(os.name != "nt", reason="WinHTTP solo existe en Windows")
@pytest.mark.parametrize(
    ("pac", "expected", "gives_a_pointer"),
    [
        ('function FindProxyForURL(u, h) { return "PROXY 10.1.1.1:83; PROXY 10.2.2.2:83; DIRECT"; }', "['10.1.1.1:83', '10.2.2.2:83']", True),
        ('function FindProxyForURL(u, h) { return "PROXY 10.1.1.1:83"; }', "['10.1.1.1:83']", True),
        ('function FindProxyForURL(u, h) { return "DIRECT"; }', "[]", False),
        ('function FindProxyForURL(u, h) { return "PROXY 127.0.0.1:9999"; }', "[]", True),
    ],
)
def test_reading_a_real_pac_returns_its_proxies_and_does_not_corrupt_memory(pac: str, expected: str, gives_a_pointer: bool):
    """Lo que se vigila: el proceso que lee la PAC SIGUE VIVO (codigo 0) tras 30 lecturas seguidas, devuelve lo que la PAC dice (sin los proxies de loopback del agente local), y la memoria
    que entrega WinHTTP se maneja bien: el texto se LEE ANTES de liberar y cada `GlobalFree` devuelve NULL (= libero lo que debia; si recibe una direccion que no es la reservada devuelve
    el puntero y no libera, y si se libera dos veces mata al proceso)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    server = _serve_pac(pac.encode("utf-8"))
    try:
        port = server.server_address[1]
        root = str(Path(__file__).resolve().parent.parent)
        res = subprocess.run([sys.executable, "-c", _PAC_CHILD.format(root=root, port=port)], capture_output=True, text=True, timeout=120)
    finally:
        server.shutdown()

    assert res.returncode == 0, f"el proceso murio con {hex(res.returncode & 0xFFFFFFFF)} (corrupcion de memoria): {res.stderr[-300:]}"
    assert f"RESULTADO {expected}" in res.stdout
    events = json.loads(next(line for line in res.stdout.splitlines() if line.startswith("EVENTOS ")).split(" ", 1)[1])
    reads = [i for i, e in enumerate(events) if e[0] == "read"]
    frees = [i for i, e in enumerate(events) if e[0] == "free"]
    if not gives_a_pointer:
        assert not reads and not frees, "con DIRECT no hay nada que leer ni que liberar"
        return
    assert len(reads) == 30 and len(frees) >= 30, (len(reads), len(frees))
    # cada liberacion tiene que venir DESPUES de haber leido el texto de esa vuelta (leer despues de liberar es leer memoria ya devuelta)
    last_read = -1
    for i, e in enumerate(events):
        if e[0] == "read":
            last_read = i
        elif e[0] == "free":
            assert last_read >= 0 and last_read < i, "se libero antes de leer"
    assert all(events[i][1] in (None, 0) for i in frees), "GlobalFree tiene que devolver NULL (libero lo que correspondia)"


def test_ultron_ships_without_a_default_proxy(monkeypatch, world):
    """De fabrica: sin proxy guardado ni predeterminado, solo cuentan la PAC de Windows y salir directo; nada se inventa."""
    monkeypatch.setattr(ph, "DEFAULT_PROXY", "")
    monkeypatch.setattr(ph, "_FALLBACK_NO_PROXY", "")
    world.pac = []
    assert [(c.source, c.url) for c in ph.build_candidates("")] == [("directo", "")]


def test_ultron_follows_the_dns_of_a_saved_proxy_name(monkeypatch, world):
    monkeypatch.setattr(ph, "DEFAULT_PROXY", "")
    world.dns["proxy.mi-red.test"] = ["10.1.1.1"]
    world.pac = []
    sources = [(c.source, c.url) for c in ph.build_candidates("http://proxy.mi-red.test:8080")]
    assert sources[0] == ("guardado", "http://proxy.mi-red.test:8080")
    assert ("dns", "http://10.1.1.1:8080") in sources and sources[-1] == ("directo", "")
