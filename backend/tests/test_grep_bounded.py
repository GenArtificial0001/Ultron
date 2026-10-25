"""spec grep-no-se-cuelga: `grep` no recorre dependencias ni binarios, tiene tope de tiempo y respeta Detener.

Todo con carpetas temporales. El paso del tiempo se simula con `fs._grep_now` (reloj propio de la herramienta), asi que
ningun test espera de verdad ni depende de la velocidad de la maquina."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools import filesystem as fs

_RESULT = re.compile(r"^(.+?):(\d+): ")


def write(p: Path, text: str = "hit\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def grep(base: Path, pattern: str = "hit", session=None, **extra) -> str:
    return fs._grep_execute({"pattern": pattern, "path": str(base), **extra}, session)


def result_lines(out: str) -> list[str]:
    """Solo las lineas de resultado `ruta:linea: texto` (no las notas ni el aviso de sin coincidencias)."""
    return [line for line in out.splitlines() if _RESULT.match(line)]


def hit_files(out: str) -> list[str]:
    """Rutas (con `/`) que tienen al menos un resultado. Se corta en `:<linea>:` y no en el primer `:`, porque en
    Windows la ruta empieza con `C:`."""
    return sorted({_RESULT.match(line).group(1).replace("\\", "/") for line in result_lines(out)})


HEAVY = [".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"]


# REQ-001
@pytest.mark.parametrize("heavy", HEAVY)
def test_dependency_folders_are_not_searched(tmp_path, heavy):
    write(tmp_path / "src" / "codigo.py")
    write(tmp_path / heavy / "lib" / "ajeno.py")

    out = grep(tmp_path)

    assert hit_files(out) == [str(tmp_path / "src" / "codigo.py").replace("\\", "/")]


def test_a_heavy_folder_is_skipped_at_any_depth(tmp_path):
    write(tmp_path / "a" / "b" / "c" / "node_modules" / "x.js")
    write(tmp_path / "a" / "ok.py")

    assert [Path(f).name for f in hit_files(grep(tmp_path))] == ["ok.py"]


def test_a_folder_that_merely_contains_the_word_is_still_searched(tmp_path):
    """`mi_venv_de_prueba` o `node_modules_old` no son las carpetas de dependencias: se compara el nombre exacto."""
    write(tmp_path / "mi_venv_de_prueba" / "a.py")
    write(tmp_path / "git_notes" / "b.py")

    assert sorted(Path(f).name for f in hit_files(grep(tmp_path))) == ["a.py", "b.py"]


# REQ-002
def test_if_the_base_folder_is_itself_a_dependency_folder_it_is_searched(tmp_path):
    base = tmp_path / ".venv" / "Lib"
    write(base / "pkg" / "modulo.py")

    assert [Path(f).name for f in hit_files(grep(base))] == ["modulo.py"]


def test_inside_an_explicit_dependency_folder_nested_ones_are_still_skipped(tmp_path):
    base = tmp_path / ".venv"
    write(base / "a.py")
    write(base / "pkg" / "__pycache__" / "b.py")

    assert [Path(f).name for f in hit_files(grep(base))] == ["a.py"]


# REQ-003
def test_binary_files_are_skipped(tmp_path):
    write(tmp_path / "texto.txt")
    (tmp_path / "libreria.dll").write_bytes(b"MZ\x00\x00hit\x00\x01\x02")

    assert [Path(f).name for f in hit_files(grep(tmp_path))] == ["texto.txt"]


def test_a_binary_is_detected_by_content_not_by_extension(tmp_path):
    (tmp_path / "parece_texto.txt").write_bytes(b"hit\x00basura")
    write(tmp_path / "real.dll", "hit en un archivo de texto con extension rara\n")

    assert [Path(f).name for f in hit_files(grep(tmp_path))] == ["real.dll"]


def test_a_nul_byte_after_the_first_8kb_does_not_make_a_text_file_binary(tmp_path):
    """Mismo criterio que git: solo se mira el comienzo. Un texto largo con un NUL perdido mas adelante se busca igual."""
    (tmp_path / "log.txt").write_bytes(b"x" * 9000 + b"\nhit\n" + b"\x00")

    assert [Path(f).name for f in hit_files(grep(tmp_path))] == ["log.txt"]


# REQ-004 + REQ-005
def test_big_files_are_skipped_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "GREP_MAX_FILE_BYTES", 100)
    write(tmp_path / "chico.txt")
    write(tmp_path / "enorme.log", "hit\n" + "x" * 500)

    out = grep(tmp_path)

    assert [Path(f).name for f in hit_files(out)] == ["chico.txt"]
    assert "[NOTA" in out and "enorme.log" in out, "un archivo grande omitido tiene que figurar en la nota"


# REQ-005
def test_skipping_dependency_folders_is_announced_and_says_how_to_search_there(tmp_path):
    write(tmp_path / "src" / "a.py")
    write(tmp_path / "node_modules" / "x.js")

    out = grep(tmp_path)

    assert "[NOTA" in out and "node_modules" in out and "path" in out


def test_no_matches_after_skipping_something_does_not_claim_there_is_nothing(tmp_path):
    write(tmp_path / ".venv" / "x.py", "hit\n")
    write(tmp_path / "src" / "a.py", "otra cosa\n")

    out = grep(tmp_path)

    assert out.startswith("(sin coincidencias)") and "[NOTA" in out and ".venv" in out


def test_nothing_skipped_means_no_note(tmp_path):
    write(tmp_path / "a.py")

    assert "[NOTA" not in grep(tmp_path)
    assert grep(tmp_path, pattern="zzz") == "(sin coincidencias)"


def test_each_skipped_folder_is_named_once_even_if_it_appears_many_times(tmp_path):
    for i in range(5):
        write(tmp_path / f"p{i}" / "node_modules" / "x.js")
    write(tmp_path / "ok.py")

    assert grep(tmp_path).count("node_modules") == 1


# REQ-006
def fake_clock(monkeypatch, step: float):
    t = {"now": 1000.0}

    def now():
        t["now"] += step
        return t["now"]

    monkeypatch.setattr(fs, "_grep_now", now)


def test_the_search_stops_at_the_time_budget_and_says_how_far_it_got(tmp_path, monkeypatch):
    for i in range(40):
        write(tmp_path / f"f{i:02d}.txt")
    monkeypatch.setattr(fs, "GREP_TIME_BUDGET_S", 10.0)
    fake_clock(monkeypatch, step=1.0)  # cada consulta al reloj "pasa" 1 s -> alcanza para ~10 archivos

    out = grep(tmp_path)

    found = result_lines(out)
    assert 0 < len(found) < 40, "debe devolver lo que alcanzo a encontrar, no todo ni nada"
    assert "[NOTA" in out and "10" in out and "revis" in out.lower()


def test_within_the_budget_nothing_is_cut(tmp_path, monkeypatch):
    for i in range(5):
        write(tmp_path / f"f{i}.txt")
    fake_clock(monkeypatch, step=0.001)

    out = grep(tmp_path)

    assert len(result_lines(out)) == 5 and "[NOTA" not in out


# REQ-007
def test_stop_button_cuts_the_search_and_says_so(tmp_path):
    for i in range(30):
        write(tmp_path / f"f{i:02d}.txt")
    state = {"reads": 0}

    class Session:
        @property
        def stop_requested(self):
            state["reads"] += 1
            return state["reads"] > 3  # el usuario aprieta Detener a mitad de la busqueda

    out = grep(tmp_path, session=Session())

    assert len(result_lines(out)) < 30
    assert "detenid" in out.lower()


def test_a_session_without_stop_support_still_works(tmp_path):
    write(tmp_path / "a.txt")

    assert "a.txt" in grep(tmp_path, session=SimpleNamespace(session_id="s"))


# REQ-008
def test_results_are_in_a_deterministic_order(tmp_path):
    for name in ["z.txt", "a.txt", "m.txt"]:
        write(tmp_path / name)
    write(tmp_path / "b" / "k.txt")

    out = grep(tmp_path)

    assert [Path(_RESULT.match(line).group(1)).name for line in result_lines(out)] == ["a.txt", "m.txt", "z.txt", "k.txt"]


def test_the_300_results_cap_still_applies_and_is_not_confused_with_a_cut(tmp_path):
    for i in range(4):
        write(tmp_path / f"m{i}.txt", "hit\n" * 100)

    out = grep(tmp_path)

    assert len(result_lines(out)) == 300


# REQ-009
def test_the_tool_description_tells_the_model_what_is_skipped():
    d = fs.grep_tool.description.lower()
    assert "node_modules" in d and "binari" in d and "60" in d
