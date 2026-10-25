"""El lanzador nunca deja la instalacion de Vision a medias (spec lanzador-extraccion-segura, REQ-001..011).

Caso real (2026-10-21): la carpeta `%LOCALAPPDATA%\\Vision\\app` quedo con 77 archivos menos y `llm/loop.py` en 0 bytes; como el marcador de version coincidia con el `.exe`, el lanzador decia «al dia» y nadie la reparaba. Tres fallas encadenadas:
1) borra y copia sin red de seguridad, 2) el marcador no se invalida antes de empezar y «sana» solo mira que la carpeta exista, 3) el puerto se chequea DESPUES de extraer (un segundo Vision abierto reescribe la carpeta en uso).

Todo en `tmp_path`: el `.exe` se simula con una carpeta `embed/` (lo que PyInstaller deja en `sys._MEIPASS`), `LOCALAPPDATA` apunta a una carpeta temporal y `sys.frozen` se fija a mano. Nada toca la instalacion real ni la red."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

import pytest

_LAUNCHER = Path(__file__).resolve().parents[2] / "launch_vision.py"
_spec = importlib.util.spec_from_file_location("launch_vision_safe_extraction_under_test", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

EMBED_HASH = "hash-de-la-version-nueva"
CODE = {
    "backend/app/main.py": "print('main')\n" * 40,
    "backend/app/version.py": '__version__ = "9.9.9"\n',
    "backend/app/scheduler/__init__.py": "",
    "backend/app/scheduler/triggers.py": "def validate_trigger(kind, trigger):\n    return None\n" * 5,
    "backend/app/llm/loop.py": "# loop\n" * 300,
    "backend/requirements.txt": "fastapi\n",
    "frontend/index.html": "<html></html>\n",
    "frontend/js/app.js": "console.log('app');\n" * 20,
}
USER_DATA = {
    "backend/.venv/Scripts/python.exe": "no soy un python de verdad",
    "backend/keys/clave.json": '{"secreto": 1}',
    "backend/data/vision.db": "datos de la persona",
    "backend/logs/app.log": "log",
    "backend/workspace/nota.txt": "mi nota",
    "backend/browser/perfil.dat": "perfil",
}


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    """Todo lo que hay bajo `root`: ruta relativa -> contenido (para comparar «no cambio nada»)."""
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Un `.exe` simulado (`embed/` en `sys._MEIPASS`) y un `%LOCALAPPDATA%` temporal. Devuelve las rutas utiles."""
    meipass = tmp_path / "meipass"
    write_tree(meipass / "embed", CODE)
    (meipass / "embed" / ".embed-hash").write_text(EMBED_HASH, encoding="utf-8")
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("VISION_DATA_DIR", raising=False)
    monkeypatch.delenv("VISION_DEV_MODE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    install = local / launcher.DATA_FOLDER_NAME / "app"
    return type(
        "Env",
        (),
        {"install": install, "local_vision": local / launcher.DATA_FOLDER_NAME, "embed": meipass / "embed", "meipass": meipass},
    )()


def install_ok(e) -> None:
    """Una instalacion SANA y al dia: el codigo + el marcador igual al del `.exe` + los datos de la persona."""
    write_tree(e.install, CODE)
    (e.install / ".vision-embed-hash").write_text(EMBED_HASH, encoding="utf-8")
    write_tree(e.install, USER_DATA)


def extract() -> tuple[Path, Path]:
    return launcher._extract_embedded_code()


# ------------------------------------------------------------------------------------------------------------------ REQ-001: sana = no toca nada
def test_a_healthy_install_is_left_exactly_as_it_is(env, capsys):
    install_ok(env)
    before = snapshot(env.install)

    backend, frontend = extract()

    assert snapshot(env.install) == before, "no se copio ni se borro nada"
    assert backend == env.install / "backend" and frontend == env.install / "frontend"
    assert "sin reextraer" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------------------------ REQ-002: detecta lo roto aunque el marcador coincida
def test_a_missing_file_is_detected_and_repaired_even_if_the_marker_matches(env, capsys):
    install_ok(env)
    (env.install / "backend/app/scheduler/triggers.py").unlink()  # exactamente lo que le paso al usuario

    extract()

    assert (env.install / "backend/app/scheduler/triggers.py").read_text(encoding="utf-8") == CODE[
        "backend/app/scheduler/triggers.py"
    ]
    out = capsys.readouterr().out
    assert "sin reextraer" not in out
    assert "incompleta" in out.lower(), "se le dice a la persona que se reparo"


def test_an_empty_file_is_detected_and_repaired_even_if_the_marker_matches(env, capsys):
    install_ok(env)
    (env.install / "backend/app/llm/loop.py").write_text("", encoding="utf-8")  # 0 bytes, como le paso al usuario

    extract()

    assert (env.install / "backend/app/llm/loop.py").read_text(encoding="utf-8") == CODE["backend/app/llm/loop.py"]
    assert "incompleta" in capsys.readouterr().out.lower()


def test_a_missing_frontend_file_is_also_detected(env):
    install_ok(env)
    (env.install / "frontend/js/app.js").unlink()

    extract()

    assert (env.install / "frontend/js/app.js").exists()


def test_an_init_py_that_is_legitimately_empty_is_not_a_reason_to_reextract(env, capsys):
    """`__init__.py` vacio es normal (el original tambien lo es): no puede disparar una reparacion en cada arranque."""
    install_ok(env)

    extract()

    assert "sin reextraer" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------------------------ REQ-003: version nueva
def test_a_new_version_replaces_the_code_with_exactly_the_embedded_one(env):
    write_tree(
        env.install,
        {
            "backend/app/main.py": "version vieja\n",
            "backend/app/archivo_viejo.py": "ya no existe en la nueva\n",
            "frontend/index.html": "viejo",
        },
    )
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")

    extract()

    assert (env.install / "backend/app/main.py").read_text(encoding="utf-8") == CODE["backend/app/main.py"]
    assert not (env.install / "backend/app/archivo_viejo.py").exists(), (
        "lo que ya no es de esta version se va (como antes)"
    )
    for rel, text in CODE.items():
        assert (env.install / rel).read_text(encoding="utf-8") == text, rel
    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_a_first_install_extracts_everything(env):
    extract()

    for rel, text in CODE.items():
        assert (env.install / rel).read_text(encoding="utf-8") == text, rel
    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


# ------------------------------------------------------------------------------------------------------------------ REQ-004: el marcador se invalida antes y se escribe al final
def test_the_marker_is_removed_before_copying_anything(env, monkeypatch):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    seen: dict[str, bool] = {}
    real = launcher._swap_in_new_code if hasattr(launcher, "_swap_in_new_code") else None
    assert real is not None, "tiene que existir el paso de reemplazo que se mide"

    def spy(*args, **kwargs):
        seen["marker_exists_when_swapping"] = (env.install / ".vision-embed-hash").exists()
        return real(*args, **kwargs)

    monkeypatch.setattr(launcher, "_swap_in_new_code", spy)

    extract()

    assert seen["marker_exists_when_swapping"] is False, (
        "durante el reemplazo no puede haber un marcador que diga «al dia»"
    )


def test_if_the_copy_fails_there_is_no_marker_so_the_next_start_repairs_it(env, monkeypatch):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("se corto la copia")

    monkeypatch.setattr(launcher.shutil, "copytree", boom)

    with pytest.raises(OSError):
        extract()

    assert not (env.install / ".vision-embed-hash").exists(), "sin marcador: la proxima apertura vuelve a intentarlo"


# ------------------------------------------------------------------------------------------------------------------ REQ-005: una copia que falla deja lo anterior intacto
def test_a_copy_that_fails_halfway_leaves_the_previous_install_untouched(env, monkeypatch):
    write_tree(
        env.install,
        {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))},
    )
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before = snapshot(env.install / "backend/app")
    real_copytree = launcher.shutil.copytree
    calls = {"n": 0}

    def fail_on_second(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # la 1.a copia sale bien y la 2.a se corta: el peor momento
            raise OSError("disco lleno a mitad de la copia")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(launcher.shutil, "copytree", fail_on_second)

    with pytest.raises(OSError):
        extract()

    assert snapshot(env.install / "backend/app") == before, "la instalacion anterior no se toco"
    assert calls["n"] >= 2, "la falla se provoco de verdad en la 2.a copia (si no, el test no prueba nada)"
    assert snapshot(env.install / "frontend") == {
        k.removeprefix("frontend/"): "VIEJO " + v for k, v in CODE.items() if k.startswith("frontend/")
    }, "ni el frontend viejo se toco"


def test_a_failed_copy_cleans_up_its_temporary_folder(env, monkeypatch):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("falla")))

    with pytest.raises(OSError):
        extract()

    assert not [p for p in env.local_vision.iterdir() if p.name.startswith("app.nuevo")], "no deja basura"


# ------------------------------------------------------------------------------------------------------------------ REQ-006: los datos de la persona no se tocan
def test_the_users_data_is_never_touched_by_an_extraction(env):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before = {k: v for k, v in snapshot(env.install).items() if k in USER_DATA}
    assert before, "el test arma datos de la persona"

    extract()

    after = snapshot(env.install)
    for rel, text in before.items():
        assert after[rel] == text, f"se toco un dato de la persona: {rel}"


def test_the_users_data_is_never_touched_by_a_repair_either(env):
    install_ok(env)
    (env.install / "backend/app/llm/loop.py").unlink()
    before = {k: v for k, v in snapshot(env.install).items() if k in USER_DATA}

    extract()

    after = snapshot(env.install)
    for rel, text in before.items():
        assert after[rel] == text, f"se toco un dato de la persona: {rel}"


def test_an_extra_file_of_the_user_inside_the_code_folders_is_not_the_concern_of_a_healthy_start(env):
    """En un arranque sano no se borra nada (ni siquiera un archivo ajeno dentro de `backend/app`)."""
    install_ok(env)
    (env.install / "backend/app/mio.py").write_text("lo puse yo", encoding="utf-8")

    extract()

    assert (env.install / "backend/app/mio.py").exists()


# ------------------------------------------------------------------------------------------------------------------ REQ-007: Vision ya corriendo = no se toca nada
def listening_port() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_if_vision_is_already_running_nothing_is_extracted_or_changed(env, monkeypatch):
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items()})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before = snapshot(env.install)
    sock, port = listening_port()
    monkeypatch.setattr(launcher, "_configured_port", lambda home=None: port)
    started = {"venv": False}
    monkeypatch.setattr(launcher, "_ensure_venv", lambda *a, **k: started.__setitem__("venv", True))
    try:
        with pytest.raises(RuntimeError, match="ya esta en uso"):
            launcher._main()
    finally:
        sock.close()

    assert snapshot(env.install) == before, "ni un archivo cambio, ni el marcador"
    assert started["venv"] is False, "corto antes de gastar tiempo en el resto"


def test_the_port_is_checked_before_the_code_is_extracted(env, monkeypatch):
    """El orden importa: si primero se extrae, un segundo Vision reescribe la carpeta del primero."""
    order: list[str] = []
    monkeypatch.setattr(launcher, "_configured_port", lambda home=None: 8020)
    monkeypatch.setattr(launcher, "_ensure_port_free", lambda *a, **k: order.append("puerto"))
    real = launcher._resolve_backend_frontend_dirs

    def spy():
        order.append("extraer")
        return real()

    monkeypatch.setattr(launcher, "_resolve_backend_frontend_dirs", spy)
    monkeypatch.setattr(
        launcher, "_ensure_venv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corto aca: ya se midio el orden"))
    )

    with pytest.raises(RuntimeError, match="corto aca"):
        launcher._main()

    assert order[:2] == ["puerto", "extraer"], order


# ------------------------------------------------------------------------------------------------------------------ REQ-008 / REQ-009: candado
def lock_path(e) -> Path:
    return e.local_vision / ".vision-extract.lock"


def test_a_live_lock_from_another_launcher_prevents_a_second_extraction(env):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before = snapshot(env.install)
    lock_path(env).write_text(
        f"{os.getpid()} {time.time()}", encoding="utf-8"
    )  # un lanzador VIVO (este mismo proceso) la esta haciendo

    with pytest.raises(RuntimeError, match="otra"):
        extract()

    assert snapshot(env.install) == before, "el segundo no toco nada"
    assert lock_path(env).exists(), "y no le saco el candado al primero"


def test_a_stale_lock_never_blocks_a_start(env):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    lock_path(env).write_text("99999999 1.0", encoding="utf-8")  # proceso inexistente y hora de hace mucho
    old = time.time() - 3600
    os.utime(lock_path(env), (old, old))

    extract()

    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_a_garbage_lock_file_never_blocks_a_start(env):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    lock_path(env).write_text("esto no es un candado valido", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock_path(env), (old, old))

    extract()

    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_the_lock_is_released_after_a_successful_extraction(env):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")

    extract()

    assert not lock_path(env).exists()


def test_the_lock_is_released_even_if_the_extraction_fails(env, monkeypatch):
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("falla")))

    with pytest.raises(OSError):
        extract()

    assert not lock_path(env).exists(), "si no, el siguiente arranque quedaria bloqueado"


def test_a_healthy_start_does_not_even_create_the_lock(env):
    install_ok(env)

    extract()

    assert not lock_path(env).exists()


# ------------------------------------------------------------------------------------------------------------------ REQ-010: modo desarrollo
def test_development_mode_extracts_nothing_and_touches_no_lock(env, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    backend, frontend = launcher._resolve_backend_frontend_dirs()

    assert backend.name == "backend" and frontend.name == "frontend"
    assert not env.install.exists() and not lock_path(env).exists()


# ------------------------------------------------------------------------------------------------------------------ seguridad: nombres de archivo del .exe
def test_an_embedded_path_that_tries_to_escape_the_install_folder_is_refused(env, tmp_path):
    outside = tmp_path / "fuera.txt"
    evil = env.embed / "backend" / "app"
    evil.mkdir(parents=True, exist_ok=True)
    assert launcher._is_safe_relative(Path("backend/app/main.py")) is True
    assert launcher._is_safe_relative(Path("../fuera.txt")) is False
    assert launcher._is_safe_relative(Path("backend/../../fuera.txt")) is False
    assert launcher._is_safe_relative(Path("C:/Windows/system.ini")) is False
    assert not outside.exists()


# ------------------------------------------------------------------------------------------------------------------ cierra mutaciones L6, L10, L11, L13 y L16
def test_a_fresh_lock_of_a_dead_process_does_not_block(env):
    """L10: el candado es RECIENTE (la rama «viejo» no aplica), pero el proceso que lo puso ya no existe: tiene que ignorarse."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    lock_path(env).write_text("99999999 " + str(time.time()), encoding="utf-8")  # reciente, pero de un proceso inexistente

    extract()

    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_an_old_lock_of_a_live_process_does_not_block_either(env):
    """L11: el proceso (este mismo) esta vivo, pero el candado tiene horas: un candado colgado jamas puede impedir arrancar."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    lock_path(env).write_text(f"{os.getpid()} 1.0", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock_path(env), (old, old))

    extract()

    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_a_half_written_staging_folder_from_a_previous_failure_is_cleaned_up(env):
    """L6: una extraccion anterior que se corto dejo `app.nuevo` con basura; la proxima la limpia y el resultado queda perfecto."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    leftover = env.local_vision / "app.nuevo"
    write_tree(leftover, {"backend/app/basura.py": "resto de una copia cortada\n"})

    extract()

    assert not leftover.exists(), "no queda la carpeta temporal"
    assert not (env.install / "backend/app/basura.py").exists(), "y su basura no se mezclo con la instalacion"


def test_a_successful_extraction_leaves_no_temporary_folder_behind(env):
    """L6: ni siquiera cuando todo sale bien queda `app.nuevo` ni `.viejo`."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")

    extract()

    leftovers = [p.name for p in env.local_vision.iterdir() if p.name.startswith("app.nuevo")]
    leftovers += [p.name for p in env.install.rglob("*.viejo")]
    assert leftovers == []


def test_a_copy_that_looks_fine_but_is_incomplete_is_refused_and_changes_nothing(env, monkeypatch):
    """L16: la copia «termina bien» pero le falta un archivo (disco lleno silencioso, antivirus). Se verifica ANTES de reemplazar: lo instalado no se toca."""
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before = snapshot(env.install)
    real_stage = launcher._stage_new_code

    def stage_but_lose_a_file(embed_src, staging_dir):
        real_stage(embed_src, staging_dir)
        (staging_dir / "backend/app/scheduler/triggers.py").unlink()  # se «perdio» un archivo sin dar error

    monkeypatch.setattr(launcher, "_stage_new_code", stage_but_lose_a_file)

    with pytest.raises(RuntimeError, match="incompleta"):
        extract()

    assert snapshot(env.install) == {k: v for k, v in before.items() if k != ".vision-embed-hash"}, "lo instalado no cambio (el marcador viejo ya se habia quitado, a proposito)"


def test_if_putting_a_folder_in_place_fails_the_previous_one_comes_back(env, monkeypatch):
    """L13: el reemplazo es por RENOMBRE; si poner la carpeta nueva falla, la que andaba vuelve a su lugar (no se borra primero)."""
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before_app = snapshot(env.install / "backend/app")
    real_replace = os.replace

    def fail_when_putting_new_app(src, dst, *a, **k):
        if Path(src).name == "app" and Path(src).parent.name == "backend" and "app.nuevo" in str(src):
            raise OSError("no se pudo poner la carpeta nueva")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(launcher.os, "replace", fail_when_putting_new_app)

    with pytest.raises(OSError):
        extract()

    assert snapshot(env.install / "backend/app") == before_app, "volvio la carpeta que andaba, entera"
    assert not (env.install / "backend" / "app.viejo").exists()


def test_a_new_version_says_it_is_updating_not_that_the_install_was_incomplete(env, capsys):
    """L18: con una version nueva el mensaje es «Actualizando...»; decir «estaba incompleta» seria falso y asustaria sin motivo."""
    write_tree(env.install, {"backend/app/main.py": "version vieja\n", "frontend/index.html": "viejo"})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")

    extract()

    out = capsys.readouterr().out
    assert "Actualizando el codigo de ULTRON" in out
    assert "incompleta" not in out.lower()


def test_a_first_install_says_it_is_updating_not_that_the_install_was_incomplete(env, capsys):
    extract()

    out = capsys.readouterr().out
    assert "Actualizando el codigo de ULTRON" in out and "incompleta" not in out.lower()


# ------------------------------------------------------------------------------------------------------------------ REQ-012 (0.35.6): un antivirus o el indexador de Windows puede tener abierto un archivo recien copiado
def _flaky_replace(monkeypatch, fail_times: int, only_when=lambda src, dst: True):
    """`os.replace` que falla con `PermissionError` (WinError 5, «acceso denegado») las primeras `fail_times` veces que coincide con `only_when`, como cuando el antivirus escanea un archivo recien escrito."""
    real = os.replace
    state = {"left": fail_times, "calls": 0}

    def replace(src, dst, *a, **k):
        if only_when(src, dst):
            state["calls"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                raise PermissionError(5, "Acceso denegado", str(src))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(launcher.os, "replace", replace)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)  # los reintentos no hacen esperar al test
    return state


def _putting_new_code(src, dst) -> bool:
    return "app.nuevo" in str(src)


def test_a_brief_access_denied_while_putting_the_new_code_in_place_is_retried_and_succeeds(env, monkeypatch):
    """Caso real (visto en la suite del 2026-10-22): `PermissionError: [WinError 5]` al renombrar `app.nuevo\frontend`. Un fallo pasajero NO tiene que impedir que Vision abra."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    state = _flaky_replace(monkeypatch, fail_times=2, only_when=_putting_new_code)

    extract()

    assert state["calls"] > 2, "se reintento de verdad (si no, el test no prueba nada)"
    for rel, text in CODE.items():
        assert (env.install / rel).read_text(encoding="utf-8") == text, rel
    assert (env.install / ".vision-embed-hash").read_text(encoding="utf-8") == EMBED_HASH


def test_a_brief_access_denied_while_moving_the_old_code_away_is_retried_too(env, monkeypatch):
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    state = _flaky_replace(monkeypatch, fail_times=2, only_when=lambda src, dst: str(dst).endswith(".viejo"))

    extract()

    assert state["calls"] > 2
    assert (env.install / "backend/app/main.py").read_text(encoding="utf-8") == CODE["backend/app/main.py"]


def test_an_access_denied_that_never_goes_away_gives_up_keeps_the_old_install_and_says_so(env, monkeypatch):
    """Los reintentos son pocos y acotados: si el archivo sigue bloqueado, se rinde, la instalacion anterior queda intacta y el error llega entero (nunca se queda colgado)."""
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before_app = snapshot(env.install / "backend/app")
    state = _flaky_replace(monkeypatch, fail_times=10_000, only_when=_putting_new_code)

    with pytest.raises(PermissionError):
        extract()

    assert 2 <= state["calls"] <= 12, f"reintenta un numero acotado de veces, no infinito: {state['calls']}"
    assert snapshot(env.install / "backend/app") == before_app, "volvio la carpeta que andaba, entera"
    assert not (env.install / ".vision-embed-hash").exists(), "sin marcador: la proxima apertura vuelve a intentarlo"
    assert not lock_path(env).exists() and not [p for p in env.local_vision.iterdir() if p.name.startswith("app.nuevo")]


def test_an_error_that_is_not_a_permission_problem_is_not_retried(env, monkeypatch):
    """Solo se reintenta lo que puede ser pasajero (acceso denegado). Cualquier otro error falla de inmediato, como antes."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def replace(src, dst, *a, **k):
        if _putting_new_code(src, dst):
            calls["n"] += 1
            raise OSError(28, "No queda espacio en el dispositivo")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(launcher.os, "replace", replace)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    with pytest.raises(OSError):
        extract()

    assert calls["n"] == 1, "un error que no es de permisos no se reintenta"


# ------------------------------------------------------------------------------------------------------------------ cierra mutaciones T4, T7 y T8 (reintento acotado)
def test_the_retries_wait_a_growing_time_between_attempts(env, monkeypatch):
    """T4: sin espera, los reintentos se gastarian en microsegundos y no darian tiempo al antivirus de soltar el archivo."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    _flaky_replace(monkeypatch, fail_times=3, only_when=_putting_new_code)
    waits: list[float] = []
    monkeypatch.setattr(launcher.time, "sleep", lambda s: waits.append(s))

    extract()

    assert len(waits) == 3 and all(w > 0 for w in waits), waits
    assert waits == sorted(waits) and waits[0] < waits[-1], f"la espera crece entre intentos: {waits}"
    assert sum(waits) < 10, "pero el total es acotado: la persona no espera minutos"


def test_if_putting_the_new_folder_fails_for_good_the_restore_of_the_old_one_also_retries(env, monkeypatch):
    """T7: poner la carpeta nueva falla para siempre Y la restauracion de la vieja tambien falla un par de veces (el mismo antivirus): igual tiene que volver la que andaba."""
    write_tree(env.install, {k: "VIEJO " + v for k, v in CODE.items() if k.startswith(("backend/app", "backend/scripts", "frontend"))})
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    before_app = snapshot(env.install / "backend/app")
    real = os.replace
    restore = {"left": 2, "seen": 0}

    def replace(src, dst, *a, **k):
        if _putting_new_code(src, dst):
            raise PermissionError(5, "Acceso denegado", str(src))  # la carpeta nueva nunca entra
        if str(src).endswith("app.viejo") and str(dst).endswith("app"):  # la restauracion de la vieja
            restore["seen"] += 1
            if restore["left"] > 0:
                restore["left"] -= 1
                raise PermissionError(5, "Acceso denegado", str(src))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(launcher.os, "replace", replace)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    with pytest.raises(PermissionError):
        extract()

    assert restore["seen"] >= 3, "la restauracion tambien reintento"
    assert snapshot(env.install / "backend/app") == before_app, "volvio la carpeta que andaba, entera"


def test_a_brief_access_denied_on_requirements_txt_is_retried(env, monkeypatch):
    """T8: el ultimo renombre del reemplazo (`requirements.txt`) tambien puede topar con el antivirus."""
    install_ok(env)
    (env.install / ".vision-embed-hash").write_text("hash-viejo", encoding="utf-8")
    state = _flaky_replace(monkeypatch, fail_times=2, only_when=lambda src, dst: str(dst).endswith("requirements.txt"))

    extract()

    assert state["calls"] > 2
    assert (env.install / "backend/requirements.txt").read_text(encoding="utf-8") == CODE["backend/requirements.txt"]
