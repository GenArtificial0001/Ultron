"""Unit de las tools de filesystem (read/write/edit/glob/grep). Todo en
`tmp_path`; el diagnostico LSP (`diagnose_python_file`, proceso externo) se
stubea. Incluye un `xfail(strict)` que documenta un bug REAL: `read` no
parsea Excel aunque `_read_tabular` exista (codigo muerto)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

from app.tools import filesystem as fs


@pytest.fixture(autouse=True)
def no_lsp(monkeypatch):
    async def fake(path):
        return ""

    monkeypatch.setattr(fs, "diagnose_python_file", fake)


def aexec(fn, args, session=None):
    return asyncio.run(fn(args, session))


# ---- resolucion de rutas / directorio base --------------------------------
def test_base_dir_without_session_is_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    assert fs._base_dir_for_session(None) == tmp_path
    assert fs._base_dir_for_session(SimpleNamespace(session_id="")) == tmp_path


def test_base_dir_uses_project_root_of_session(monkeypatch, tmp_path):
    import app.persistence.service as persistence

    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: tmp_path / sid)
    assert fs._base_dir_for_session(SimpleNamespace(session_id="quimica")) == tmp_path / "quimica"


def test_base_dir_falls_back_when_session_has_no_project(monkeypatch, tmp_path):
    import app.persistence.service as persistence

    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: None)
    monkeypatch.setattr(fs, "get_settings", lambda: SimpleNamespace(workspace_path=tmp_path / "ws"))
    assert fs._base_dir_for_session(SimpleNamespace(session_id="s")) == tmp_path / "ws"


def test_resolve_keeps_absolute_and_anchors_relative(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "_base_dir_for_session", lambda s: tmp_path)
    assert fs._resolve(str(tmp_path / "a.txt")) == tmp_path / "a.txt"
    assert fs._resolve("sub/b.txt") == tmp_path / "sub" / "b.txt"


# ---- read ------------------------------------------------------------------
def test_read_missing_arg_and_missing_file(tmp_path):
    assert fs._read_execute({}).startswith("Error: falta")
    assert "no existe" in fs._read_execute({"path": str(tmp_path / "nada")})


def test_read_numbers_lines_and_paginates(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("uno\ndos\ntres\ncuatro", encoding="utf-8")
    assert fs._read_execute({"path": str(f)}) == "1: uno\n2: dos\n3: tres\n4: cuatro"
    assert fs._read_execute({"path": str(f), "offset": 2, "limit": 2}) == "2: dos\n3: tres"


def test_read_empty_file_and_offset_out_of_range(tmp_path):
    f = tmp_path / "vacio.txt"
    f.write_text("")
    assert "vacio" in fs._read_execute({"path": str(f)})
    f.write_text("x")
    assert "fuera de rango" in fs._read_execute({"path": str(f), "offset": 99})


def test_read_directory_lists_entries(tmp_path):
    (tmp_path / "carpeta").mkdir()
    (tmp_path / "b.txt").write_text("x")
    assert fs._read_execute({"path": str(tmp_path)}).splitlines() == ["b.txt", "carpeta/"]
    empty = tmp_path / "vacia"
    empty.mkdir()
    assert fs._read_execute({"path": str(empty)}) == "(directorio vacio)"


def test_read_survives_undecodable_bytes(tmp_path):
    f = tmp_path / "raro.txt"
    f.write_bytes(b"ok \xff\xfe fin")
    assert "fin" in fs._read_execute({"path": str(f)})


def test_read_reports_os_errors(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("x")

    def deny(self, *a, **k):
        raise PermissionError("denegado")

    monkeypatch.setattr(type(f), "read_text", deny)
    assert fs._read_execute({"path": str(f)}) == "Error leyendo archivo: denegado"


def test_read_parses_excel_into_tabular_summary(tmp_path):
    """Regresion QA-001: `_read_tabular` existia pero era codigo muerto, asi que
    `read` sobre un .xlsx devolvia mojibake y el modelo pedia exportar a CSV."""
    f = tmp_path / "datos.xlsx"
    pd.DataFrame({"parte": ["A1", "B2"], "qty": [1, 2]}).to_excel(f, index=False)
    out = fs._read_execute({"path": str(f)})
    assert "2 filas x 2 columnas" in out
    assert "Columnas: parte (" in out and "qty (int64)" in out and "A1" in out
    assert "1: " not in out  # no cae al camino de texto numerado


@pytest.mark.parametrize("ext", [".csv", ".tsv", ".parquet", ".xlsx", ".XLSX"])
def test_read_routes_every_tabular_extension(tmp_path, ext):
    """QA-001: la deteccion es por extension (case-insensitive), no solo Excel."""
    df = pd.DataFrame({"a": [1, 2, 3]})
    f = tmp_path / f"d{ext}"
    if ext.lower() == ".parquet":
        pytest.importorskip("pyarrow")
        df.to_parquet(f)
    elif ext.lower() == ".xlsx":
        df.to_excel(f, index=False)
    else:
        df.to_csv(f, sep="\t" if ext == ".tsv" else ",", index=False)
    assert "3 filas x 1 columnas" in fs._read_execute({"path": str(f)})


def test_read_forwards_sheet_and_pagination_to_tabular(tmp_path):
    """QA-001: el `sheet` del schema tiene que llegar a `_read_tabular`."""
    f = tmp_path / "libro.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame({"a": [1]}).to_excel(w, sheet_name="Uno", index=False)
        pd.DataFrame({"z": list(range(10))}).to_excel(w, sheet_name="Dos", index=False)
    assert "mostrando: 'Uno'" in fs._read_execute({"path": str(f)})
    out = fs._read_execute({"path": str(f), "sheet": "Dos", "offset": 3, "limit": 2})
    assert "10 filas x 1 columnas" in out and "Filas 3-4 de 10" in out


def test_read_schema_declares_sheet():
    assert "sheet" in fs.read_tool.parameters["properties"]


def test_read_still_treats_non_tabular_files_as_text(tmp_path):
    f = tmp_path / "notas.txt"
    f.write_text("uno\ndos", encoding="utf-8")
    assert fs._read_execute({"path": str(f)}) == "1: uno\n2: dos"


# ---- _read_tabular (funcion aislada) ----------------------------------------
def test_read_tabular_csv_summary_and_pagination(tmp_path):
    f = tmp_path / "d.csv"
    pd.DataFrame({"a": range(10), "b": list("abcdefghij")}).to_csv(f, index=False)
    out = fs._read_tabular(f, offset=3, limit=2, sheet=None)
    assert "10 filas x 2 columnas" in out and "Filas 3-4 de 10" in out
    assert "a (int64)" in out and "Hojas:" not in out


def test_read_tabular_tsv_uses_tab_separator(tmp_path):
    f = tmp_path / "d.tsv"
    f.write_text("x\ty\n1\t2\n", encoding="utf-8")
    assert "1 filas x 2 columnas" in fs._read_tabular(f, 1, 10, None)


def test_read_tabular_excel_lists_sheets_and_selects_one(tmp_path):
    f = tmp_path / "libro.xlsx"
    with pd.ExcelWriter(f) as w:
        pd.DataFrame({"a": [1]}).to_excel(w, sheet_name="Uno", index=False)
        pd.DataFrame({"z": [7, 8, 9]}).to_excel(w, sheet_name="Dos", index=False)
    default = fs._read_tabular(f, 1, 10, None)
    assert "Hojas: Uno, Dos" in default and "mostrando: 'Uno'" in default
    second = fs._read_tabular(f, 1, 10, "Dos")
    assert "3 filas x 1 columnas" in second


def test_read_tabular_empty_and_corrupt(tmp_path):
    empty = tmp_path / "e.csv"
    empty.write_text("a,b\n", encoding="utf-8")
    assert "(sin filas)" in fs._read_tabular(empty, 1, 10, None)
    bad = tmp_path / "roto.xlsx"
    bad.write_bytes(b"no soy un zip")
    assert fs._read_tabular(bad, 1, 10, None).startswith("Error parseando 'roto.xlsx'")


# ---- write -----------------------------------------------------------------
def test_write_creates_parents_and_reports(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    out = aexec(fs._write_execute, {"path": str(target), "content": "hola"})
    assert target.read_text(encoding="utf-8") == "hola"
    assert "4 caracteres" in out


def test_write_missing_path_and_empty_content_allowed(tmp_path):
    assert aexec(fs._write_execute, {"content": "x"}).startswith("Error: falta")
    aexec(fs._write_execute, {"path": str(tmp_path / "v.txt")})
    assert (tmp_path / "v.txt").read_text() == ""


def test_write_appends_lsp_diagnostics(tmp_path, monkeypatch):
    async def diag(path):
        return "Diagnosticos: linea 1 error"

    monkeypatch.setattr(fs, "diagnose_python_file", diag)
    out = aexec(fs._write_execute, {"path": str(tmp_path / "x.py"), "content": "x ="})
    assert out.endswith("Diagnosticos: linea 1 error")


def test_write_relative_path_goes_to_session_project(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_base_dir_for_session", lambda s: tmp_path / "proj")
    aexec(fs._write_execute, {"path": "notas.md", "content": "x"}, SimpleNamespace(session_id="s"))
    assert (tmp_path / "proj" / "notas.md").is_file()


# ---- write: evento "skill-created" (REQ-002/003/004, spec update-skills) --
# BUG REAL reportado por el usuario: cuando Vision crea una skill sola
# durante el chat (via esta misma tool generica `write`, no hay una tool
# dedicada), ninguna de las 3 listas de skills del frontend se actualizaba
# porque nada avisaba que el filesystem de skills habia cambiado. Estos
# tests fijan el contrato: escribir un archivo llamado SKILL.md (en
# cualquier capitalizacion) dispara `session.push_event("skill-created", ...)`
# con el scope correcto; cualquier otro nombre de archivo no dispara nada.
class FakeSession:
    def __init__(self, sid="s1"):
        self.session_id = sid
        self.events: list[tuple[str, dict]] = []

    def push_event(self, kind, payload):
        self.events.append((kind, payload))


def test_write_skill_md_emits_skill_created_event_global_scope(tmp_path, monkeypatch):
    global_dir = tmp_path / "global-skills"
    global_dir.mkdir()
    monkeypatch.setattr(
        fs,
        "get_settings",
        lambda: SimpleNamespace(
            global_skills_path=global_dir,
            skills_path=tmp_path / "no-existe-1",
            workspace_path=tmp_path,
        ),
    )
    import app.project_context as project_context
    monkeypatch.setattr(project_context, "get_project_config", lambda: SimpleNamespace(skills_repo_path=""))
    target = global_dir / "Enterprise-Search" / "SKILL.md"
    s = FakeSession()
    aexec(fs._write_execute, {"path": str(target), "content": "---\nname: x\n---\n"}, s)
    assert s.events == [("skill-created", {"path": str(target), "scope": "global"})]


def test_write_skill_md_emits_skill_created_event_local_scope(tmp_path, monkeypatch):
    import app.persistence.service as persistence

    monkeypatch.setattr(
        fs,
        "get_settings",
        lambda: SimpleNamespace(
            global_skills_path=tmp_path / "no-existe-1",
            skills_path=tmp_path / "no-existe-2",
            workspace_path=tmp_path,
        ),
    )
    import app.project_context as project_context
    monkeypatch.setattr(project_context, "get_project_config", lambda: SimpleNamespace(skills_repo_path=""))
    project_root = tmp_path / "proyecto-quimica"
    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: project_root)
    target = project_root / persistence.SKILLS_SUBDIR / "MiSkill" / "SKILL.md"
    s = FakeSession(sid="quimica")
    aexec(fs._write_execute, {"path": str(target), "content": "x"}, s)
    assert s.events == [("skill-created", {"path": str(target), "scope": "local"})]


def test_write_skill_md_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fs,
        "get_settings",
        lambda: SimpleNamespace(
            global_skills_path=tmp_path / "no-existe-1",
            skills_path=tmp_path / "no-existe-2",
            workspace_path=tmp_path,
        ),
    )
    import app.project_context as project_context
    monkeypatch.setattr(project_context, "get_project_config", lambda: SimpleNamespace(skills_repo_path=""))
    import app.persistence.service as persistence

    monkeypatch.setattr(persistence, "project_root_for_session", lambda sid: None)
    target = tmp_path / "carpeta-random" / "skill.MD"
    s = FakeSession()
    aexec(fs._write_execute, {"path": str(target), "content": "x"}, s)
    assert s.events and s.events[0][0] == "skill-created" and s.events[0][1]["scope"] == "unknown"


def test_write_non_skill_file_does_not_emit_event(tmp_path):
    s = FakeSession()
    aexec(fs._write_execute, {"path": str(tmp_path / "notas.md"), "content": "x"}, s)
    assert s.events == []


def test_write_skill_md_without_session_does_not_crash(tmp_path):
    # Sin session (turno sin contexto de sesion real) -- debe seguir
    # escribiendo el archivo con normalidad, simplemente sin emitir evento
    # (no hay `session.push_event` a quien llamar).
    target = tmp_path / "SKILL.md"
    out = aexec(fs._write_execute, {"path": str(target), "content": "x"}, None)
    assert target.is_file() and "Archivo escrito" in out


# ---- edit ------------------------------------------------------------------
def test_edit_validation_errors(tmp_path):
    assert aexec(fs._edit_execute, {"path": "x"}).startswith("Error: se requieren")
    assert "no existe" in aexec(fs._edit_execute, {"path": str(tmp_path / "n"), "old_string": "a"})
    f = tmp_path / "a.txt"
    f.write_text("hola")
    assert "no se encontro" in aexec(fs._edit_execute, {"path": str(f), "old_string": "zzz", "new_string": "y"})


def test_edit_single_replacement(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("uno dos", encoding="utf-8")
    out = aexec(fs._edit_execute, {"path": str(f), "old_string": "uno", "new_string": "UNO"})
    assert f.read_text(encoding="utf-8") == "UNO dos" and "1 reemplazo" in out


def test_edit_ambiguous_match_requires_replace_all(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x x x", encoding="utf-8")
    out = aexec(fs._edit_execute, {"path": str(f), "old_string": "x", "new_string": "y"})
    assert "aparece 3 veces" in out and f.read_text() == "x x x"  # no toca nada
    out = aexec(fs._edit_execute, {"path": str(f), "old_string": "x", "new_string": "y", "replace_all": True})
    assert f.read_text() == "y y y" and "3 reemplazo" in out


def test_edit_appends_diagnostics(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("a = 1")

    async def diag(path):
        return "DIAG"

    monkeypatch.setattr(fs, "diagnose_python_file", diag)
    assert aexec(fs._edit_execute, {"path": str(f), "old_string": "1", "new_string": "2"}).endswith("DIAG")


# ---- glob ------------------------------------------------------------------
def test_glob_validation_and_no_results(tmp_path):
    assert fs._glob_execute({}).startswith("Error: falta")
    assert "no existe" in fs._glob_execute({"pattern": "*", "path": str(tmp_path / "nada")})
    assert fs._glob_execute({"pattern": "*.zzz", "path": str(tmp_path)}) == "(sin resultados)"


def test_glob_orders_by_mtime_desc_and_caps_at_200(tmp_path):
    import os

    for i in range(205):
        p = tmp_path / f"f{i:03}.txt"
        p.write_text("x")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    lines = fs._glob_execute({"pattern": "*.txt", "path": str(tmp_path)}).splitlines()
    assert len(lines) == 200 and lines[0].endswith("f204.txt")


def test_glob_uses_session_base_when_no_path(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    monkeypatch.setattr(fs, "_base_dir_for_session", lambda s: tmp_path)
    assert "a.py" in fs._glob_execute({"pattern": "*.py"})


# ---- grep ------------------------------------------------------------------
def test_grep_validation(tmp_path):
    assert fs._grep_execute({}).startswith("Error: falta")
    assert "no existe" in fs._grep_execute({"pattern": "x", "path": str(tmp_path / "n")})
    assert fs._grep_execute({"pattern": "(", "path": str(tmp_path)}).startswith("Error: patron regex invalido")


def test_grep_finds_lines_with_numbers_and_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("import os\nprint('hola')\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("print('no')\n", encoding="utf-8")
    out = fs._grep_execute({"pattern": r"print\(", "path": str(tmp_path), "include": "*.py"})
    assert out.splitlines() == [f"{tmp_path / 'a.py'}:2: print('hola')"]
    assert fs._grep_execute({"pattern": "zzz", "path": str(tmp_path)}) == "(sin coincidencias)"


def test_grep_truncates_long_lines_and_caps_results(tmp_path):
    (tmp_path / "long.txt").write_text("needle " + "x" * 1000, encoding="utf-8")
    line = fs._grep_execute({"pattern": "needle", "path": str(tmp_path)})
    assert len(line.split(": ", 1)[1]) <= 300
    (tmp_path / "long.txt").unlink()
    for i in range(4):
        (tmp_path / f"m{i}.txt").write_text("hit\n" * 100, encoding="utf-8")
    assert len(fs._grep_execute({"pattern": "hit", "path": str(tmp_path)}).splitlines()) == 300


def test_grep_skips_unreadable_files(tmp_path, monkeypatch):
    (tmp_path / "ok.txt").write_text("hit", encoding="utf-8")
    (tmp_path / "mal.txt").write_text("hit", encoding="utf-8")
    real = type(tmp_path).read_text

    def flaky(self, *a, **k):
        if self.name == "mal.txt":
            raise OSError("bloqueado")
        return real(self, *a, **k)

    monkeypatch.setattr(type(tmp_path), "read_text", flaky)
    out = fs._grep_execute({"pattern": "hit", "path": str(tmp_path)})
    assert "ok.txt" in out and "mal.txt" not in out


def test_tool_flags():
    assert fs.write_tool.dangerous and fs.edit_tool.dangerous
    assert not (fs.read_tool.dangerous or fs.glob_tool.dangerous or fs.grep_tool.dangerous)
