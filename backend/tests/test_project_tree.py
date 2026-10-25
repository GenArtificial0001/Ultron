"""BL-020: el arbol de la carpeta del proyecto que se inyecta en el prompt de CADA turno (`app/project_context.py`).

`build_local_project_tree` y `build_project_context_prompt` corren antes de cada respuesta del agente: si son lentos o
inconsistentes, lo es cada turno. Estos tests fijan primero el comportamiento actual (formato, orden, limites) para poder
optimizar sin cambiarlo, y luego el costo (contado en llamadas al sistema, no en milisegundos: nada depende de la maquina).

Todo con carpetas temporales; el indice maestro y la config global se redirigen a un temp (fixture autouse `tmp_master_db`)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import project_context as pc
from app.persistence import master_index as mi


def make(root: Path, *rel: str, text: str = "x") -> None:
    """Crea archivos (con carpetas intermedias) bajo `root`; un nombre que termina en `/` crea solo la carpeta."""
    for r in rel:
        p = root / r
        if r.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")


def tree(root: Path) -> list[str]:
    return pc.build_local_project_tree(str(root))["tree"].splitlines()


# ---------------------------------------------------------------------------------------------------------------------
# A) FORMATO Y ORDEN del arbol (linea base: deben pasar con el codigo actual)
# ---------------------------------------------------------------------------------------------------------------------
class TestTreeFormat:
    def test_folders_come_before_files_and_each_group_is_alphabetical_ignoring_case(self, tmp_path):
        make(tmp_path, "zeta.txt", "Alfa.txt", "beta/", "Carpeta/", "alfa2.txt")

        assert tree(tmp_path) == ["beta/", "Carpeta/", "Alfa.txt", "alfa2.txt", "zeta.txt"]

    def test_children_are_indented_two_spaces_per_level(self, tmp_path):
        make(tmp_path, "a/b/c.txt", "a/x.txt")

        assert tree(tmp_path) == ["a/", "  b/", "    c.txt", "  x.txt"]

    def test_hidden_entries_and_ignored_folders_are_skipped(self, tmp_path):
        make(tmp_path, ".oculto", ".git/config", "node_modules/m.js", "__pycache__/x.pyc", "dist/o.js", "visible.txt")

        assert tree(tmp_path) == ["visible.txt"]

    def test_an_empty_folder_gives_an_empty_tree(self, tmp_path):
        assert tree(tmp_path) == []

    def test_the_result_carries_the_resolved_path(self, tmp_path):
        assert pc.build_local_project_tree(str(tmp_path))["path"] == str(tmp_path)

    def test_a_missing_or_non_directory_path_raises_file_not_found(self, tmp_path):
        f = tmp_path / "archivo.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            pc.build_local_project_tree(str(tmp_path / "no-existe"))
        with pytest.raises(FileNotFoundError):
            pc.build_local_project_tree(str(f))

    def test_an_unreadable_subfolder_does_not_abort_the_walk(self, tmp_path, monkeypatch):
        make(tmp_path, "cerrada/secreto.txt", "abierta/ok.txt", "z.txt")
        real_iterdir = Path.iterdir
        real_scandir = pc.os.scandir if hasattr(pc, "os") else None

        def deny(self):
            if self.name == "cerrada":
                raise PermissionError("sin acceso")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", deny)
        if real_scandir is not None:  # la version optimizada usa os.scandir

            def deny_scandir(path):
                if Path(path).name == "cerrada":
                    raise PermissionError("sin acceso")
                return real_scandir(path)

            monkeypatch.setattr(pc.os, "scandir", deny_scandir)

        lines = tree(tmp_path)

        assert "cerrada/" in lines and "  secreto.txt" not in lines, "se lista la carpeta pero no su contenido"
        assert "  ok.txt" in lines and "z.txt" in lines, "lo demas se sigue listando"


# ---------------------------------------------------------------------------------------------------------------------
# A) LIMITE de entradas
# ---------------------------------------------------------------------------------------------------------------------
class TestTreeLimit:
    def test_exactly_at_the_limit_nothing_is_truncated(self, tmp_path):
        make(tmp_path, *[f"f{i:03d}.txt" for i in range(pc.MAX_TREE_ENTRIES)])

        lines = tree(tmp_path)

        assert len(lines) == pc.MAX_TREE_ENTRIES and not any("truncado" in line for line in lines)

    def test_one_over_the_limit_adds_a_single_truncation_notice(self, tmp_path):
        make(tmp_path, *[f"f{i:03d}.txt" for i in range(pc.MAX_TREE_ENTRIES + 1)])

        lines = tree(tmp_path)

        assert len(lines) == pc.MAX_TREE_ENTRIES + 1
        assert lines[-1] == f"... (arbol truncado, mas de {pc.MAX_TREE_ENTRIES} entradas)"

    def test_the_first_entries_shown_are_the_alphabetically_first(self, tmp_path):
        make(tmp_path, *[f"f{i:04d}.txt" for i in range(pc.MAX_TREE_ENTRIES + 50)])

        lines = tree(tmp_path)

        assert lines[0] == "f0000.txt" and lines[pc.MAX_TREE_ENTRIES - 1] == f"f{pc.MAX_TREE_ENTRIES - 1:04d}.txt"


# ---------------------------------------------------------------------------------------------------------------------
# E2) COSTO: cuantas veces se consulta el sistema de archivos (no milisegundos)
# ---------------------------------------------------------------------------------------------------------------------
class TestTreeCost:
    """Antes: `sorted(d.iterdir(), key=lambda c: (c.is_file(), ...))` hacia UN stat por cada archivo de la carpeta aunque solo se
    muestren 400 (carpeta de 20.000 archivos: 1091 ms contra 19 ms con scandir). Se cuenta, no se cronometra: el numero de
    llamadas no depende de la velocidad del disco ni de la maquina que corre la suite."""

    def _count_stats(self, monkeypatch) -> dict[str, int]:
        calls = {"is_file": 0, "is_dir": 0, "stat": 0}
        for name in ("is_file", "is_dir", "stat"):
            real = getattr(Path, name)

            def counting(self, *a, _name=name, _real=real, **k):
                calls[_name] += 1
                return _real(self, *a, **k)

            monkeypatch.setattr(Path, name, counting)
        return calls

    def test_the_number_of_filesystem_queries_does_not_grow_with_the_folder_size(self, tmp_path, monkeypatch):
        make(tmp_path, *[f"f{i:05d}.csv" for i in range(3000)])
        calls = self._count_stats(monkeypatch)

        tree(tmp_path)

        total = sum(calls.values())
        assert total < pc.MAX_TREE_ENTRIES * 3, f"{total} consultas para una carpeta de 3000 archivos (tope de arbol: {pc.MAX_TREE_ENTRIES})"

    def test_a_small_folder_costs_a_handful_of_queries(self, tmp_path, monkeypatch):
        make(tmp_path, "a.txt", "b/c.txt")
        calls = self._count_stats(monkeypatch)

        tree(tmp_path)

        assert sum(calls.values()) < 30

    def test_the_output_is_identical_for_a_big_folder(self, tmp_path):
        """El arreglo de costo no puede cambiar ni el orden ni el contenido: 450 archivos, se ven los primeros 400 y UN aviso."""
        make(tmp_path, *[f"f{i:04d}.txt" for i in range(450)])

        lines = tree(tmp_path)

        assert lines[:3] == ["f0000.txt", "f0001.txt", "f0002.txt"] and len(lines) == pc.MAX_TREE_ENTRIES + 1


# ---------------------------------------------------------------------------------------------------------------------
# E3) El aviso de truncado sale UNA vez
# ---------------------------------------------------------------------------------------------------------------------
class TestTruncationNotice:
    def test_nested_folders_produce_one_notice_not_one_per_level(self, tmp_path):
        """Antes cada nivel de la recursion agregaba su aviso al volver: con 6 niveles salian 2 (uno indentado y otro no)."""
        cur = tmp_path
        for level in range(6):
            make(cur, *[f"a{i:03d}.txt" for i in range(90)])
            cur = cur / f"nivel{level}"
            cur.mkdir()

        notices = [line for line in tree(tmp_path) if "truncado" in line]

        assert len(notices) == 1, notices

    def test_the_single_notice_is_not_indented(self, tmp_path):
        make(tmp_path, *[f"d{i:03d}/x.txt" for i in range(300)])

        notices = [line for line in tree(tmp_path) if "truncado" in line]

        assert notices == [f"... (arbol truncado, mas de {pc.MAX_TREE_ENTRIES} entradas)"]


# ---------------------------------------------------------------------------------------------------------------------
# E4) El README no se repite en el prompt
# ---------------------------------------------------------------------------------------------------------------------
class TestReadmeNotDuplicated:
    def test_a_readme_appears_once_in_the_key_docs_with_its_real_name(self, tmp_path):
        """En Windows `readme.md` existe para un `README.md` (el FS no distingue mayusculas): se leia dos veces."""
        make(tmp_path, "README.md", text="contenido unico")

        docs = pc.build_local_project_tree(str(tmp_path))["key_docs"]

        assert list(docs) == ["README.md"] and docs["README.md"] == "contenido unico"

    def test_a_lowercase_readme_keeps_its_real_lowercase_name(self, tmp_path):
        make(tmp_path, "readme.md", text="en minusculas")

        assert list(pc.build_local_project_tree(str(tmp_path))["key_docs"]) == ["readme.md"]

    def test_the_prompt_block_contains_the_readme_text_only_once(self, no_sharepoint, tmp_path):
        make(tmp_path, "README.md", text="MARCADOR-UNICO-" + "x" * 200)
        mi.set_project_path("s1", str(tmp_path), windows_user_created="u_20260929120000")

        out = pc.build_project_context_prompt("s1")

        assert out.count("MARCADOR-UNICO-") == 1, "el README ocupaba tokens dos veces en cada turno"
        assert out.count("### README.md") + out.count("### readme.md") == 1


# ---------------------------------------------------------------------------------------------------------------------
# Bordes del arreglo (BL-020)
# ---------------------------------------------------------------------------------------------------------------------
class TestFixEdges:
    def test_ignored_and_hidden_entries_do_not_count_toward_the_limit(self, tmp_path):
        """400 visibles + ocultos/ignorados al final del orden: NO hay aviso (lo que no se ve no cuenta)."""
        make(tmp_path, *[f"f{i:04d}.txt" for i in range(pc.MAX_TREE_ENTRIES)], ".oculto1", ".oculto2", "node_modules/m.js")

        lines = tree(tmp_path)

        assert len(lines) == pc.MAX_TREE_ENTRIES and not any("truncado" in line for line in lines)

    def test_truncation_inside_a_subfolder_still_gives_one_notice_at_the_end(self, tmp_path):
        make(tmp_path, *[f"sub/f{i:04d}.txt" for i in range(pc.MAX_TREE_ENTRIES + 20)])

        lines = tree(tmp_path)

        assert lines[0] == "sub/" and lines[-1].startswith("... (arbol truncado")
        assert sum("truncado" in line for line in lines) == 1

    def test_key_docs_keep_the_order_of_the_configured_list(self, tmp_path):
        make(tmp_path, "pyproject.toml", "AGENTS.md", "README.md", text="x")

        assert list(pc.build_local_project_tree(str(tmp_path))["key_docs"]) == ["README.md", "AGENTS.md", "pyproject.toml"]

    def test_a_differently_cased_key_doc_is_found_with_its_real_name(self, tmp_path):
        """`Readme.MD` (ni README.md ni readme.md): el nombre pedido no coincide letra a letra pero es el mismo documento."""
        make(tmp_path, "Readme.MD", text="mixto")

        assert pc.build_local_project_tree(str(tmp_path))["key_docs"] == {"Readme.MD": "mixto"}

    def test_two_readmes_that_differ_only_in_case_are_read_once_on_a_case_insensitive_fs(self, tmp_path):
        """En Windows no se pueden crear los dos a la vez; aqui se verifica que, con uno, la lista nunca lo repite."""
        make(tmp_path, "README.md", text="uno")

        assert len(pc.build_local_project_tree(str(tmp_path))["key_docs"]) == 1

    def test_each_key_doc_file_is_read_exactly_once(self, tmp_path, monkeypatch):
        """El resultado (un dict por nombre real) esconderia una lectura doble: se cuentan las lecturas. Antes README.md y readme.md
        resolvian al MISMO archivo y se leia dos veces (costo de E/S y de tokens en cada turno)."""
        make(tmp_path, "README.md", "AGENTS.md", text="x")
        reads: list[str] = []
        real = Path.read_text

        def spy(self, *a, **k):
            reads.append(self.name)
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", spy)

        pc.build_local_project_tree(str(tmp_path))

        assert sorted(reads) == ["AGENTS.md", "README.md"], reads

    def test_a_folder_named_like_a_key_doc_is_never_opened_for_reading(self, tmp_path, monkeypatch):
        """Leer una carpeta como archivo lanza PermissionError y el except lo tapa: el resultado seria igual, asi que se verifica el intento."""
        make(tmp_path, "README.md/")
        reads: list[str] = []
        real = Path.read_text

        def spy(self, *a, **k):
            reads.append(self.name)
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", spy)

        pc.build_local_project_tree(str(tmp_path))

        assert reads == []

    def test_a_child_cut_off_exactly_at_the_limit_is_announced_not_dropped_silently(self, tmp_path):
        """BL-020 E5: con exactamente MAX carpetas, el hijo de la ULTIMA se omitia sin ningun aviso (nada se omite en silencio)."""
        make(tmp_path, *[f"d{i:04d}/" for i in range(pc.MAX_TREE_ENTRIES)])
        make(tmp_path, f"d{pc.MAX_TREE_ENTRIES - 1:04d}/hijo.txt")

        lines = tree(tmp_path)

        assert len(lines) == pc.MAX_TREE_ENTRIES + 1 and lines[-1].startswith("... (arbol truncado")

    def test_no_false_notice_when_the_last_folder_at_the_limit_is_empty(self, tmp_path):
        make(tmp_path, *[f"d{i:04d}/" for i in range(pc.MAX_TREE_ENTRIES)])

        lines = tree(tmp_path)

        assert len(lines) == pc.MAX_TREE_ENTRIES and not any("truncado" in line for line in lines)

    def test_no_false_notice_when_the_last_folder_only_holds_hidden_or_ignored_entries(self, tmp_path):
        make(tmp_path, *[f"d{i:04d}/" for i in range(pc.MAX_TREE_ENTRIES)])
        make(tmp_path, f"d{pc.MAX_TREE_ENTRIES - 1:04d}/.oculto", f"d{pc.MAX_TREE_ENTRIES - 1:04d}/node_modules/m.js")

        assert not any("truncado" in line for line in tree(tmp_path))

    def test_an_unlistable_root_still_returns_the_tree_with_no_key_docs(self, tmp_path, monkeypatch):
        make(tmp_path, "a.txt")
        real = pc.os.listdir

        def deny(path):
            raise PermissionError("sin acceso")

        monkeypatch.setattr(pc.os, "listdir", deny)

        info = pc.build_local_project_tree(str(tmp_path))

        assert info["tree"] == "a.txt" and info["key_docs"] == {}
        assert real is not None  # el original sigue accesible para otros tests

    def test_the_tree_is_built_from_a_single_directory_listing_per_folder(self, tmp_path, monkeypatch):
        make(tmp_path, "a/x.txt", "b/y.txt", "c.txt")
        scans: list[str] = []
        real = pc.os.scandir

        def spy(path):
            scans.append(Path(path).name)
            return real(path)

        monkeypatch.setattr(pc.os, "scandir", spy)

        tree(tmp_path)

        assert sorted(scans) == sorted([tmp_path.name, "a", "b"]), "un listado por carpeta, ni mas ni menos"


# ---------------------------------------------------------------------------------------------------------------------
# A) DOCS CLAVE
# ---------------------------------------------------------------------------------------------------------------------
class TestKeyDocs:
    def test_present_key_docs_are_read(self, tmp_path):
        make(tmp_path, "README.md", text="# Titulo")
        make(tmp_path, "AGENTS.md", text="reglas")
        make(tmp_path, "otro.md", text="no es clave")

        docs = pc.build_local_project_tree(str(tmp_path))["key_docs"]

        assert docs == {"README.md": "# Titulo", "AGENTS.md": "reglas"}, "un doc por archivo, con su nombre real en disco"

    def test_a_long_doc_is_cut_at_the_limit(self, tmp_path):
        make(tmp_path, "README.md", text="a" * (pc.MAX_KEY_DOC_CHARS + 500))

        assert len(pc.build_local_project_tree(str(tmp_path))["key_docs"]["README.md"]) == pc.MAX_KEY_DOC_CHARS

    def test_a_doc_with_bad_bytes_is_still_read_with_replacement_characters(self, tmp_path):
        (tmp_path / "README.md").write_bytes(b"ok \xff\xfe fin")

        text = pc.build_local_project_tree(str(tmp_path))["key_docs"]["README.md"]

        assert text.startswith("ok ") and text.endswith(" fin")

    def test_an_unreadable_doc_does_not_stop_the_others(self, tmp_path, monkeypatch):
        make(tmp_path, "README.md", text="malo")
        make(tmp_path, "AGENTS.md", text="bueno")
        real = Path.read_text

        def flaky(self, *a, **k):
            if self.name == "README.md":
                raise OSError("bloqueado")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", flaky)

        assert pc.build_local_project_tree(str(tmp_path))["key_docs"] == {"AGENTS.md": "bueno"}, "el README ilegible no se cuela por otro nombre"

    def test_a_folder_named_like_a_key_doc_is_not_read_as_a_file(self, tmp_path):
        make(tmp_path, "README.md/")

        assert pc.build_local_project_tree(str(tmp_path))["key_docs"] == {}


# ---------------------------------------------------------------------------------------------------------------------
# B) El bloque que llega al system prompt
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def no_sharepoint(monkeypatch):
    monkeypatch.setattr(
        pc,
        "get_project_config",
        lambda: SimpleNamespace(
            sharepoint_drive_id="", sharepoint_drive_name="", sharepoint_folder_id="", sharepoint_folder_path=""
        ),
    )


class TestPromptBlock:
    def test_without_a_session_or_sharepoint_the_block_is_empty(self, no_sharepoint):
        assert pc.build_project_context_prompt() == "" and pc.build_project_context_prompt(None) == ""

    def test_a_session_without_a_folder_gives_no_local_block(self, no_sharepoint):
        assert pc.build_project_context_prompt("sesion-sin-carpeta") == ""

    def test_with_a_folder_the_block_has_the_tree_and_the_key_docs(self, no_sharepoint, tmp_path):
        make(tmp_path, "src/app.py", "README.md", text="# Mi proyecto")
        mi.set_project_path("s1", str(tmp_path), windows_user_created="u_20260929120000")

        out = pc.build_project_context_prompt("s1")

        assert out.startswith("<project_context>") and out.endswith("</project_context>")
        assert f"## Proyecto local activo: {tmp_path}" in out
        assert "src/\n  app.py" in out and "### README.md" in out and "# Mi proyecto" in out

    def test_a_folder_that_no_longer_exists_degrades_to_a_plain_note(self, no_sharepoint, tmp_path):
        mi.set_project_path("s1", str(tmp_path / "borrada"), windows_user_created="u_20260929120000")

        out = pc.build_project_context_prompt("s1")

        assert "No se pudo cargar el proyecto local" in out and "Traceback" not in out

    def test_an_active_sharepoint_folder_is_announced_without_a_local_one(self, monkeypatch):
        cfg = SimpleNamespace(
            sharepoint_drive_id="d1",
            sharepoint_drive_name="Docs",
            sharepoint_folder_id="f1",
            sharepoint_folder_path="Ventas/2026",
        )
        monkeypatch.setattr(pc, "get_project_config", lambda: cfg)

        out = pc.build_project_context_prompt()

        assert "biblioteca 'Docs'" in out and "Ventas/2026" in out and "drive_id=d1" in out and "folder_id=f1" in out

    def test_sharepoint_root_is_named_when_there_is_no_folder_path(self, monkeypatch):
        cfg = SimpleNamespace(
            sharepoint_drive_id="d1", sharepoint_drive_name="Docs", sharepoint_folder_id="", sharepoint_folder_path=""
        )
        monkeypatch.setattr(pc, "get_project_config", lambda: cfg)

        out = pc.build_project_context_prompt()

        assert "carpeta '(raiz)'" in out and "folder_id=root" in out
