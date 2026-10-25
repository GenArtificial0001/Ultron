"""BL-011: la herramienta `glob` no llena su lista de resultados con archivos de dependencias.

Medido en este repo (2026-10-03): `glob('**/*.py')` devolvia 200 resultados y 91 eran de `.venv/site-packages`; `**/version.py` devolvia 50 y 49 eran de dependencias. No es un problema de velocidad (mediana 35 ms)
sino de utilidad: el tope de 200 se gastaba en codigo ajeno y el codigo del proyecto quedaba fuera de la lista. Misma regla que `grep` (spec grep-no-se-cuelga): no entra a carpetas de dependencias/cache por DEBAJO de la base,
y si la base misma es una de ellas, se busca ahi; nada se omite en silencio (una nota lo dice). Todo con carpetas temporales."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools import filesystem as fs


def touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def glob(base: Path, pattern: str = "**/*.py", **extra) -> str:
    return fs._glob_execute({"pattern": pattern, "path": str(base), **extra})


def result_paths(out: str) -> list[str]:
    return [line.replace("\\", "/") for line in out.splitlines() if line and not line.startswith(("[", "("))]


HEAVY = [
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
]


class TestSkipsDependencyFolders:
    @pytest.mark.parametrize("heavy", HEAVY)
    def test_files_inside_dependency_folders_are_not_listed(self, tmp_path, heavy):
        touch(tmp_path / "src" / "codigo.py")
        touch(tmp_path / heavy / "lib" / "ajeno.py")

        paths = result_paths(glob(tmp_path))

        assert [Path(p).name for p in paths] == ["codigo.py"]

    def test_the_cap_of_200_is_spent_on_project_code_not_on_dependencies(self, tmp_path):
        for i in range(250):
            touch(tmp_path / ".venv" / "Lib" / "site-packages" / f"dep{i}.py")
        for i in range(30):
            touch(tmp_path / "src" / f"mio{i}.py")

        paths = result_paths(glob(tmp_path))

        assert len(paths) == 30 and all("/src/" in p for p in paths)

    def test_nested_dependency_folders_are_skipped_at_any_depth(self, tmp_path):
        touch(tmp_path / "a" / "b" / "node_modules" / "m.js")
        touch(tmp_path / "a" / "ok.js")

        assert [Path(p).name for p in result_paths(glob(tmp_path, "**/*.js"))] == ["ok.js"]

    def test_a_folder_that_only_contains_the_word_is_still_searched(self, tmp_path):
        touch(tmp_path / "mi_venv_de_prueba" / "a.py")
        touch(tmp_path / "git_notes" / "b.py")

        assert sorted(Path(p).name for p in result_paths(glob(tmp_path))) == ["a.py", "b.py"]

    def test_if_the_base_is_itself_a_dependency_folder_it_is_searched(self, tmp_path):
        base = tmp_path / ".venv" / "Lib"
        touch(base / "pkg" / "modulo.py")

        assert [Path(p).name for p in result_paths(glob(base))] == ["modulo.py"]

    def test_inside_an_explicit_dependency_folder_nested_ones_are_still_skipped(self, tmp_path):
        base = tmp_path / ".venv"
        touch(base / "a.py")
        touch(base / "pkg" / "__pycache__" / "b.py")

        assert [Path(p).name for p in result_paths(glob(base))] == ["a.py"]


class TestNothingIsOmittedSilently:
    def test_skipping_dependency_folders_is_announced(self, tmp_path):
        touch(tmp_path / "src" / "a.py")
        touch(tmp_path / "node_modules" / "x.js")

        out = glob(tmp_path, "**/*")

        assert "[NOTA" in out and "node_modules" in out and "path" in out

    def test_no_results_after_skipping_does_not_claim_there_is_nothing(self, tmp_path):
        touch(tmp_path / ".venv" / "x.py")

        out = glob(tmp_path)

        assert out.startswith("(sin resultados)") and "[NOTA" in out and ".venv" in out

    def test_nothing_skipped_means_no_note(self, tmp_path):
        touch(tmp_path / "a.py")

        assert "[NOTA" not in glob(tmp_path)
        assert glob(tmp_path, "**/*.zzz") == "(sin resultados)"

    def test_each_skipped_folder_is_named_once(self, tmp_path):
        for i in range(4):
            touch(tmp_path / f"p{i}" / "node_modules" / "x.js")
        touch(tmp_path / "ok.js")

        assert glob(tmp_path, "**/*.js").count("node_modules") == 1

    def test_the_note_is_not_counted_as_a_result_and_the_cap_still_applies(self, tmp_path):
        for i in range(210):
            touch(tmp_path / "src" / f"f{i}.py")
        touch(tmp_path / ".git" / "x.py")

        out = glob(tmp_path)

        assert len(result_paths(out)) == 200 and "[NOTA" in out


class TestNoteIsSeparatedFromResults:
    """G10: la nota va despues de una linea en blanco, para que el modelo no la lea como un archivo mas de la lista.
    `result_paths` ignora las lineas que empiezan con `[`, asi que por si solo no detectaba que se perdiera esa separacion."""

    def test_a_blank_line_precedes_the_note(self, tmp_path):
        touch(tmp_path / "src" / "a.py")
        touch(tmp_path / "node_modules" / "x.py")

        lines = glob(tmp_path).splitlines()
        note_at = next(i for i, line in enumerate(lines) if line.startswith("[NOTA"))

        assert lines[note_at - 1] == ""
        assert lines[note_at - 2].replace("\\", "/").endswith("src/a.py")


class TestUnchangedBehaviour:
    def test_newest_files_come_first(self, tmp_path):
        old = touch(tmp_path / "viejo.py")
        new = touch(tmp_path / "nuevo.py")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))

        assert [Path(p).name for p in result_paths(glob(tmp_path))] == ["nuevo.py", "viejo.py"]

    def test_the_cap_is_200(self, tmp_path):
        for i in range(260):
            touch(tmp_path / f"f{i:03d}.py")

        assert len(result_paths(glob(tmp_path))) == 200

    def test_a_non_recursive_pattern_only_looks_at_the_top_level(self, tmp_path):
        touch(tmp_path / "a.py")
        touch(tmp_path / "sub" / "b.py")

        assert [Path(p).name for p in result_paths(glob(tmp_path, "*.py"))] == ["a.py"]

    def test_directories_can_match_too(self, tmp_path):
        (tmp_path / "carpeta").mkdir()

        assert [Path(p).name for p in result_paths(glob(tmp_path, "*"))] == ["carpeta"]

    def test_missing_pattern_and_missing_base_are_reported(self, tmp_path):
        assert fs._glob_execute({"pattern": ""}).startswith("Error: falta")
        assert fs._glob_execute({"pattern": "*", "path": str(tmp_path / "no-existe")}).startswith("Error: no existe")

    def test_a_file_that_disappears_while_sorting_does_not_break_the_search(self, tmp_path, monkeypatch):
        touch(tmp_path / "a.py")
        touch(tmp_path / "b.py")
        real = Path.stat

        def flaky(self, *a, **k):
            if self.name == "a.py":
                raise FileNotFoundError("se borro justo ahora")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "stat", flaky)

        out = glob(tmp_path)

        assert "b.py" in out

    def test_the_tool_description_tells_the_model_what_is_skipped(self):
        d = fs.glob_tool.description.lower()
        assert "node_modules" in d and "path" in d
