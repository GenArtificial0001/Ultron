"""BL-009: export de una sesion a Markdown (`app/persistence/transcript.py`) y su uso real por la auto-purga.

`test_session_purge.py` prueba la POLITICA de la purga con `transcript` simulado. Aca se usan las capas REALES (base temporal, indice maestro
temporal) para verificar lo que la politica da por supuesto: que cada export deja un respaldo propio. Sin red ni sleep; el reloj del export se
congela con una subclase de `datetime` para reproducir "dos exports en el mismo segundo" sin esperar."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from app.persistence import db, purge
from app.persistence import service as svc
from app.persistence import transcript as tr

USER = "jorge"


@pytest.fixture(autouse=True)
def real_layers(monkeypatch):
    """Escritura inmediata (sin hilo) y usuario fijo para que `assign_project_path` y los listados coincidan."""
    monkeypatch.setattr(svc, "submit", lambda job: job())
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "proyecto"
    root.mkdir()
    return root


def freeze(monkeypatch, when: datetime):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(tr, "datetime", Frozen)


def make_session(sid: str, folder, title: str = "", text: str = "hola", at: float | None = None):
    at = at if at is not None else time.time()
    svc.assign_project_path(sid, str(folder))
    path = db.db_path_for(str(folder))
    db.ensure_session(path, sid, at)
    if title:
        db.rename_session(path, sid, title, at)
    db.insert_message(path, f"{sid}-u", sid, "user", at)
    db.insert_part(path, f"{sid}-u1", f"{sid}-u", sid, "text", {"text": text}, at)
    return path


# ---------------------------------------------------------------------------------------------------------------------
# A) Formato de cada parte
# ---------------------------------------------------------------------------------------------------------------------
class TestFormatPart:
    def test_text(self):
        assert tr.format_part("text", {"text": "hola"}) == "hola\n\n"

    def test_synthetic_text_is_hidden(self):
        assert tr.format_part("text", {"text": "interno", "synthetic": True}) == ""

    def test_reasoning_is_omitted(self):
        assert tr.format_part("reasoning", {"text": "pienso"}) == ""

    def test_unknown_types_are_omitted(self):
        assert tr.format_part("algo-nuevo", {"x": 1}) == ""

    def test_image_is_a_placeholder_not_the_base64(self):
        assert tr.format_part("image", {"url": "data:image/png;base64," + "A" * 1000}) == "*[imagen adjunta]*\n\n"

    def test_file_shows_path_and_size(self):
        out = tr.format_part("file", {"path": "attachment/r.pdf", "name": "r.pdf", "size": 2048})
        assert "attachment/r.pdf" in out and "KB" in out and "eliminado" not in out

    def test_a_deleted_file_is_marked(self):
        assert "(eliminado)" in tr.format_part("file", {"path": "attachment/r.pdf", "deleted": True})

    def test_file_without_a_path_falls_back_to_its_name_then_a_question_mark(self):
        assert "r.pdf" in tr.format_part("file", {"name": "r.pdf"})
        assert "?" in tr.format_part("file", {})

    def test_tool_with_json_arguments_and_a_result(self):
        out = tr.format_part("tool", {"name": "grep", "arguments": '{"pattern": "x"}', "result": "ok"})
        assert "**Tool: grep**" in out and '"pattern": "x"' in out and "**Output:**" in out and "ok" in out

    def test_tool_arguments_as_a_dict(self):
        assert '"a": 1' in tr.format_part("tool", {"name": "t", "arguments": {"a": 1}})

    def test_tool_arguments_that_are_not_valid_json_are_shown_as_text(self):
        out = tr.format_part("tool", {"name": "t", "arguments": "no es json {"})
        assert "no es json {" in out

    def test_tool_error_is_shown_when_there_is_no_result(self):
        out = tr.format_part("tool", {"name": "t", "error": "fallo"})
        assert "**Error:**" in out and "fallo" in out and "**Output:**" not in out

    def test_when_there_is_both_a_result_and_an_error_only_the_result_is_shown(self):
        """`elif`: el resultado manda; mostrar los dos duplicaria informacion contradictoria en el respaldo."""
        out = tr.format_part("tool", {"name": "t", "result": "salida", "error": "ruido"})
        assert "**Output:**" in out and "salida" in out and "**Error:**" not in out and "ruido" not in out

    def test_an_empty_result_is_still_an_output_block(self):
        assert "**Output:**" in tr.format_part("tool", {"name": "t", "result": ""})

    def test_unicode_is_kept_in_tool_arguments(self):
        assert "áéí" in tr.format_part("tool", {"name": "t", "arguments": {"x": "áéí"}})


class TestFencesInToolOutput:
    """BL-028 (ADR-011, H4): un resultado o error de herramienta que contiene ``` cerraba el bloque de codigo antes de tiempo y el resto del .md quedaba como codigo.
    La valla debe ser mas larga que la racha de backticks mas larga del contenido (regla de CommonMark)."""

    @staticmethod
    def _balanced(md: str) -> bool:
        """Cada valla de apertura tiene su cierre de la MISMA longitud o mayor: se recorre como lo haria un parser de Markdown."""
        import re

        open_len = None
        for m in re.finditer(r"^(`{3,})([^`\n]*)$", md, re.M):
            ticks = len(m.group(1))
            if open_len is None:
                open_len = ticks
            elif ticks >= open_len and not m.group(2).strip():
                open_len = None
        return open_len is None

    @pytest.mark.parametrize("field", ["result", "error"])
    def test_backticks_inside_the_output_do_not_break_the_block(self, field):
        out = tr.format_part("tool", {"name": "bash", field: "antes\n```\ndespues"})

        assert self._balanced(out) and "despues" in out and "antes" in out

    def test_a_longer_run_of_backticks_gets_an_even_longer_fence(self):
        out = tr.format_part("tool", {"name": "bash", "result": "x\n````\ny"})

        assert self._balanced(out) and "`````" in out

    def test_normal_output_keeps_the_usual_three_backtick_fence(self):
        """Sin backticks en el contenido NO cambia nada: el formato de siempre (y los respaldos ya exportados)."""
        out = tr.format_part("tool", {"name": "bash", "result": "hola"})

        assert "**Output:**\n```\nhola\n```\n" in out and "````" not in out

    def test_inline_single_backticks_do_not_trigger_a_longer_fence(self):
        out = tr.format_part("tool", {"name": "bash", "result": "usa `ls` y `cd`"})

        assert "````" not in out and "```\nusa `ls` y `cd`\n```" in out

    def test_json_arguments_with_backticks_in_values_stay_balanced(self):
        out = tr.format_part("tool", {"name": "write", "arguments": {"content": "```py\nprint(1)\n```"}})

        assert self._balanced(out)

    def test_the_longest_run_wins_even_when_a_shorter_one_comes_first(self):
        """Sin esto una implementacion que solo mira la PRIMERA racha pasaria: con ``` primero y ````` despues quedaria una valla de 4 y se rompe igual."""
        assert tr._fence_for("``` y despues ````` mas") == "``````"
        out = tr.format_part("tool", {"name": "bash", "result": "uno\n```\ndos\n`````\ntres"})
        assert self._balanced(out) and "``````" in out

    def test_arguments_with_backticks_stay_balanced_whatever_fence_length_is_chosen(self):
        """`json.dumps` escapa los saltos de linea: un ``` dentro de un valor nunca queda al inicio de una linea y por si solo no podria cerrar el bloque.
        Aun asi `_fence_for` cuenta TODA racha de backticks (tambien las que estan en medio de una linea) y devuelve una valla de 4: mas larga de lo
        estrictamente necesario, pero valida y segura. Se afirma lo que importa (el bloque queda balanceado), no la longitud exacta, para no fijar un detalle."""
        out = tr.format_part("tool", {"name": "write", "arguments": {"content": "a\n```\nb"}})
        assert self._balanced(out) and "a\\n```\\nb" in out

    def test_the_fence_helper_picks_the_minimum_safe_length(self):
        assert tr._fence_for("sin nada") == "```"
        assert tr._fence_for("``") == "```"
        assert tr._fence_for("```") == "````"
        assert tr._fence_for("a ```` b ``` c") == "`````"


class TestHeadersAndMessages:
    def test_assistant_header_variants(self):
        assert tr.format_assistant_header(None, None) == "## Assistant\n\n"
        assert tr.format_assistant_header("gpt", None) == "## Assistant · gpt\n\n"
        assert tr.format_assistant_header("gpt", 3.456) == "## Assistant · gpt · 3.5s\n\n"
        assert tr.format_assistant_header(None, 0.0) == "## Assistant · 0.0s\n\n"

    def test_user_and_assistant_messages_have_their_headings(self):
        assert tr.format_message("user", [("text", {"text": "q"})]).startswith("## User\n\n")
        assert tr.format_message("assistant", [("text", {"text": "r"})], "m").startswith("## Assistant · m\n\n")

    def test_system_and_tool_roles_are_omitted(self):
        assert tr.format_message("system", [("text", {"text": "x"})]) == ""
        assert tr.format_message("tool", [("text", {"text": "x"})]) == ""

    def test_transcript_has_metadata_then_messages_separated_by_rules(self):
        out = tr.format_transcript(
            "Mi sesion",
            "sid-1",
            0.0,
            60.0,
            [
                {"role": "user", "parts": [("text", {"text": "uno"})]},
                {"role": "assistant", "parts": [("text", {"text": "dos"})], "model": "m"},
            ],
        )
        assert out.startswith("# Mi sesion\n\n**Session ID:** sid-1\n")
        assert "**Created:**" in out and "**Updated:**" in out
        assert out.index("## User") < out.index("uno") < out.index("## Assistant · m") < out.index("dos")
        assert out.count("---\n\n") == 3  # tras el encabezado y tras cada mensaje

    def test_date_format_is_month_day_year_12_hour(self):
        assert tr._fmt_dt(datetime(2026, 10, 2, 15, 4, 5).timestamp()) == "10/02/2026, 03:04:05 PM"


# ---------------------------------------------------------------------------------------------------------------------
# B) Nombre de archivo a partir del titulo
# ---------------------------------------------------------------------------------------------------------------------
class TestFilenameTitle:
    def test_a_placeholder_title_gives_no_prefix(self):
        assert tr._filename_title_component(db.DEFAULT_TITLE_PREFIX + "2026-10-02T05:00:00") is None

    def test_unsafe_characters_are_replaced(self):
        assert tr._filename_title_component('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_accents_and_spaces_are_kept(self):
        assert tr._filename_title_component("Reporte de ñandú") == "Reporte de ñandú"

    def test_it_is_cut_at_60_characters(self):
        assert len(tr._filename_title_component("x" * 100)) == 60

    def test_surrounding_spaces_are_trimmed_and_blank_gives_none(self):
        assert tr._filename_title_component("  hola  ") == "hola"
        assert tr._filename_title_component("   ") is None
        assert tr._filename_title_component("") is None


# ---------------------------------------------------------------------------------------------------------------------
# C) Lectura desde la base
# ---------------------------------------------------------------------------------------------------------------------
class TestBuildExportMessages:
    def test_a_missing_database_gives_no_messages(self, tmp_path):
        assert tr.build_export_messages(tmp_path / "no-existe.db", "s1") == []

    def test_messages_come_in_chronological_order_with_their_parts_in_order(self, folder):
        path = make_session("s1", folder, text="pregunta", at=100.0)
        db.insert_message(path, "s1-a", "s1", "assistant", 200.0)
        db.insert_part(path, "s1-a1", "s1-a", "s1", "text", {"text": "primero"}, 200.0)
        db.insert_part(path, "s1-a2", "s1-a", "s1", "tool", {"name": "grep", "result": "r"}, 201.0)
        db.insert_part(path, "s1-a3", "s1-a", "s1", "text", {"text": "ultimo"}, 202.0)

        msgs = tr.build_export_messages(path, "s1")

        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert [p[0] for p in msgs[1]["parts"]] == ["text", "tool", "text"]
        assert msgs[1]["parts"][0][1]["text"] == "primero" and msgs[1]["parts"][2][1]["text"] == "ultimo"

    def test_only_the_requested_session_is_exported(self, folder):
        path = make_session("s1", folder, text="de s1")
        make_session("s2", folder, text="de s2")

        texts = [p[1]["text"] for m in tr.build_export_messages(path, "s1") for p in m["parts"]]

        assert texts == ["de s1"]

    def test_the_database_connection_is_released(self, folder):
        path = make_session("s1", folder)
        tr.build_export_messages(path, "s1")
        path.rename(path.with_suffix(".movida"))  # en Windows falla si quedo abierta


class TestExportToMarkdown:
    def test_without_a_folder_it_returns_none(self):
        assert tr.export_session_to_markdown("sin-carpeta") is None
        assert tr.export_session_to_file("sin-carpeta") is None

    def test_a_session_that_is_not_in_its_database_returns_none(self, folder):
        svc.assign_project_path("fantasma", str(folder))
        db.ensure_session(db.db_path_for(str(folder)), "otra", 1.0)

        assert tr.export_session_to_markdown("fantasma") is None
        assert tr.export_session_to_file("fantasma") is None

    def test_a_real_session_exports_its_title_and_conversation(self, folder):
        make_session("s1", folder, title="Reporte ZCC", text="analiza el stock")

        md = tr.export_session_to_markdown("s1")

        assert md.startswith("# Reporte ZCC\n") and "**Session ID:** s1" in md and "analiza el stock" in md


# ---------------------------------------------------------------------------------------------------------------------
# D) Export a archivo
# ---------------------------------------------------------------------------------------------------------------------
class TestExportToFile:
    def test_it_creates_the_sessions_folder_and_writes_utf8(self, folder, monkeypatch):
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))
        make_session("s1", folder, title="Reporte", text="ñandú ✓")

        out = tr.export_session_to_file("s1")

        assert out == folder / svc.SESSIONS_SUBDIR / "Reporte_session-2026-10-02_05-00-00.md"
        assert "ñandú ✓" in out.read_text(encoding="utf-8")

    def test_a_session_without_its_own_title_gets_no_prefix(self, folder, monkeypatch):
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))
        make_session("s1", folder)

        assert tr.export_session_to_file("s1").name == "session-2026-10-02_05-00-00.md"

    def test_exporting_twice_in_different_seconds_keeps_both_files(self, folder, monkeypatch):
        make_session("s1", folder, title="Reporte")
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))
        first = tr.export_session_to_file("s1")
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 1))
        second = tr.export_session_to_file("s1")

        assert first != second and first.exists() and second.exists()

    def test_exporting_the_same_session_twice_in_one_second_never_overwrites(self, folder, monkeypatch):
        make_session("s1", folder, title="Reporte")
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))

        first = tr.export_session_to_file("s1")
        first_text = first.read_text(encoding="utf-8")
        second = tr.export_session_to_file("s1")

        assert first != second, "la docstring promete que CADA export crea un archivo NUEVO"
        assert first.read_text(encoding="utf-8") == first_text and second.exists()

    def test_two_untitled_sessions_exported_in_one_second_keep_separate_files(self, folder, monkeypatch):
        make_session("sA", folder, text="TEXTO-UNICO-DE-A")
        make_session("sB", folder, text="TEXTO-UNICO-DE-B")
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))

        a = tr.export_session_to_file("sA")
        b = tr.export_session_to_file("sB")

        assert a != b
        assert "TEXTO-UNICO-DE-A" in a.read_text(encoding="utf-8") and "TEXTO-UNICO-DE-B" in b.read_text(
            encoding="utf-8"
        )


class TestNeverOverwrites:
    """Bordes del arreglo (ADR-011): `_write_new_file` crea con modo exclusivo y numera si el nombre esta ocupado."""

    def test_a_free_name_is_used_as_is(self, tmp_path):
        out = tr._write_new_file(tmp_path, "informe.md", "uno")
        assert out.name == "informe.md" and out.read_text(encoding="utf-8") == "uno"

    def test_a_taken_name_gets_consecutive_numeric_suffixes(self, tmp_path):
        names = [tr._write_new_file(tmp_path, "informe.md", f"c{i}").name for i in range(4)]

        assert names == ["informe.md", "informe-2.md", "informe-3.md", "informe-4.md"]

    def test_each_file_keeps_its_own_content(self, tmp_path):
        paths = [tr._write_new_file(tmp_path, "informe.md", f"contenido-{i}") for i in range(3)]

        assert [p.read_text(encoding="utf-8") for p in paths] == ["contenido-0", "contenido-1", "contenido-2"]

    def test_a_file_that_already_existed_is_never_touched(self, tmp_path):
        (tmp_path / "informe.md").write_text("ORIGINAL", encoding="utf-8")

        out = tr._write_new_file(tmp_path, "informe.md", "nuevo")

        assert (tmp_path / "informe.md").read_text(encoding="utf-8") == "ORIGINAL" and out.name == "informe-2.md"

    def test_a_gap_in_the_numbering_is_filled_in_order(self, tmp_path):
        (tmp_path / "informe.md").write_text("a", encoding="utf-8")
        (tmp_path / "informe-3.md").write_text("c", encoding="utf-8")

        assert tr._write_new_file(tmp_path, "informe.md", "b").name == "informe-2.md"

    def test_the_suffix_goes_before_the_extension_and_keeps_dots_in_the_name(self, tmp_path):
        (tmp_path / "a.b.md").write_text("x", encoding="utf-8")

        assert tr._write_new_file(tmp_path, "a.b.md", "y").name == "a.b-2.md"

    def test_it_gives_up_with_a_clear_error_after_the_attempt_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tr, "MAX_FILENAME_ATTEMPTS", 3)
        for name in ("informe.md", "informe-2.md", "informe-3.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")

        with pytest.raises(FileExistsError, match="3 intentos"):
            tr._write_new_file(tmp_path, "informe.md", "y")

        assert sorted(p.name for p in tmp_path.iterdir()) == ["informe-2.md", "informe-3.md", "informe.md"]

    def test_the_last_allowed_attempt_still_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tr, "MAX_FILENAME_ATTEMPTS", 3)
        for name in ("informe.md", "informe-2.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")

        assert tr._write_new_file(tmp_path, "informe.md", "y").name == "informe-3.md"

    def test_the_returned_path_is_the_one_that_was_written(self, tmp_path):
        (tmp_path / "informe.md").write_text("x", encoding="utf-8")
        out = tr._write_new_file(tmp_path, "informe.md", "propio")
        assert out.parent == tmp_path and out.read_text(encoding="utf-8") == "propio"

    def test_utf8_content_survives(self, tmp_path):
        out = tr._write_new_file(tmp_path, "i.md", "ñandú ✓ 日本")
        assert out.read_text(encoding="utf-8") == "ñandú ✓ 日本"

    def test_many_exports_of_one_session_in_one_second_all_survive(self, folder, monkeypatch):
        make_session("s1", folder, title="Reporte", text="contenido")
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))

        paths = [tr.export_session_to_file("s1") for _ in range(5)]

        assert len({p.name for p in paths}) == 5 and all(p.exists() for p in paths)


# ---------------------------------------------------------------------------------------------------------------------
# E) La auto-purga REAL: nunca borra sin un respaldo propio
# ---------------------------------------------------------------------------------------------------------------------
class TestPurgeKeepsABackupPerSession:
    def _old_sessions(self, folder, *items):
        old = time.time() - (purge.RETENTION_DAYS + 5) * 86400
        path = None
        for sid, text in items:
            path = make_session(sid, folder, text=text, at=old)
            with db._connect(path) as conn:
                conn.execute("UPDATE session SET updated_at = ? WHERE id = ?", (old, sid))
        return path

    def test_one_old_session_is_exported_then_deleted(self, folder):
        path = self._old_sessions(folder, ("sA", "TEXTO-UNICO-DE-A"))

        assert purge._purge_one_project_folder(str(folder)) == 1

        assert db.list_sessions(path) == []
        [backup] = list((folder / svc.SESSIONS_SUBDIR).glob("*.md"))
        assert "TEXTO-UNICO-DE-A" in backup.read_text(encoding="utf-8")

    def test_two_old_untitled_sessions_in_the_same_second_both_keep_their_backup(self, folder, monkeypatch):
        path = self._old_sessions(folder, ("sA", "TEXTO-UNICO-DE-A"), ("sB", "TEXTO-UNICO-DE-B"))
        freeze(monkeypatch, datetime(2026, 10, 2, 5, 0, 0))  # la purga recorre las candidatas en milisegundos

        assert purge._purge_one_project_folder(str(folder)) == 2

        assert db.list_sessions(path) == []
        backups = list((folder / svc.SESSIONS_SUBDIR).glob("*.md"))
        alltext = "".join(b.read_text(encoding="utf-8") for b in backups)
        assert "TEXTO-UNICO-DE-A" in alltext and "TEXTO-UNICO-DE-B" in alltext, (
            f"respaldos en disco: {[b.name for b in backups]}"
        )

    def test_a_session_whose_export_fails_is_not_deleted(self, folder, monkeypatch):
        path = self._old_sessions(folder, ("sA", "TEXTO-UNICO-DE-A"))
        monkeypatch.setattr(purge, "export_session_to_file", lambda sid: None)

        assert purge._purge_one_project_folder(str(folder)) == 0
        assert [s["id"] for s in db.list_sessions(path)] == ["sA"], "sin respaldo no se borra"
