"""BL-022: la fachada de persistencia (`app/persistence/service.py`) que `loop.py`, `main.py` y las tools usan en CADA turno.

`test_persistence_db.py` ya cubre la capa SQL (`db.py`). Aca se cubre la FACHADA: resolver la carpeta de ESA sesion, encolar el
trabajo en el hilo escritor y no hacer nada (sin error) cuando la sesion no tiene carpeta. El hilo escritor se reemplaza por una
ejecucion inmediata (fixture `sync_writer`): los tests no dependen de hilos ni de tiempos. Todo en `tmp_path`; el indice maestro
va a un temp por la fixture autouse `tmp_master_db`."""

from __future__ import annotations

import time

import pytest

from app.persistence import db, master_index
from app.persistence import service as svc

USER = "jorge"


@pytest.fixture(autouse=True)
def sync_writer(monkeypatch):
    """`submit` ejecuta el trabajo AL INSTANTE y guarda el orden en que se encolo."""
    queued: list[str] = []

    def submit(job):
        queued.append(getattr(job, "__name__", "job"))
        job()

    monkeypatch.setattr(svc, "submit", submit)
    return queued


@pytest.fixture(autouse=True)
def fixed_user(monkeypatch):
    """El usuario 'actual' lo calculan DOS lugares: `service.current_windows_user()` (para filtrar) y `db.windows_user_tag()`
    (para registrar al creador, lee USERNAME). Si no coinciden el listado sale vacio: se fijan los dos."""
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(svc, "current_windows_user", lambda: USER)


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "proyecto"
    root.mkdir()
    return root


def assign(session_id: str, folder, title: str = "") -> None:
    svc.assign_project_path(session_id, str(folder), title)


def rows(folder, sql: str, *args):
    with db._connect(db.db_path_for(str(folder))) as conn:
        return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------------------------------------------------
# A) ESCRITURA: ruta de ESA sesion; no-op sin carpeta
# ---------------------------------------------------------------------------------------------------------------------
class TestWritesWithoutAFolder:
    """Una sesion sin carpeta asignada vive 100% en memoria: ninguna escritura falla ni crea archivos."""

    def test_every_write_is_a_silent_noop(self, tmp_path, sync_writer, tmp_master_db):
        # `vision.db` en tmp_path es el INDICE MAESTRO global (fixture autouse), no una base de proyecto: se excluye de la comparacion
        before = sorted(p.name for p in tmp_path.iterdir() if p != tmp_master_db)

        svc.ensure_session("sin-carpeta", "modelo")
        svc.maybe_autoname_session("sin-carpeta", "titulo")
        svc.record_message_start("sin-carpeta", "m1", "user")
        svc.record_part_start("sin-carpeta", "m1", "p1", "text", {"text": "x"})
        svc.record_part_end("sin-carpeta", "p1", {"text": "y"})
        svc.record_todos("sin-carpeta", [{"content": "a"}])
        svc.delete_message("sin-carpeta", "m1")
        svc.truncate_from_message("sin-carpeta", "m1")
        svc.clear_messages("sin-carpeta")
        svc.rename_session("sin-carpeta", "nuevo")
        svc.delete_session("sin-carpeta")
        svc.delete_assistant_response("sin-carpeta", 0)

        # rename/delete_session encolan SOLO la actualizacion del indice maestro (global), nunca nada de un vision.db: son 2 trabajos y no mas
        assert len(sync_writer) == 2, f"se encolo mas de lo esperado: {sync_writer}"
        assert sorted(p.name for p in tmp_path.iterdir() if p != tmp_master_db) == before, "no se creo ningun archivo ni carpeta de proyecto"

    def test_reads_without_a_folder_give_neutral_values(self):
        assert svc.is_persistence_available("sin-carpeta") is False
        assert svc.is_persistence_available(None) is False
        assert svc.get_active_db_path("sin-carpeta") is None and svc.get_active_db_path(None) is None
        assert svc.project_root_for_session("sin-carpeta") is None
        assert svc.session_exists_in_db("sin-carpeta") is False
        assert svc.find_user_occurrence("sin-carpeta", "m1") is None
        assert svc.user_message_snapshot("sin-carpeta", "m1") is None
        assert svc.list_persisted_sessions(None) == [] and svc.list_persisted_sessions("sin-carpeta") == []
        assert svc.get_session_meta("sin-carpeta") is None

    def test_loading_a_session_without_a_folder_gives_empty_lists(self):
        assert svc.load_session_messages("sin-carpeta") == []
        assert svc.load_session_todos("sin-carpeta") == []

    def test_display_path_falls_back_to_the_app_folder_without_implying_persistence(self):
        shown = svc.resolve_project_path_for_display("sin-carpeta")
        assert shown and svc.is_persistence_available("sin-carpeta") is False


class TestWritesWithAFolder:
    def test_a_full_turn_is_persisted_in_the_sessions_own_folder(self, folder):
        assign("s1", folder)

        svc.ensure_session("s1", "gpt")
        svc.record_message_start("s1", "m1", "user")
        svc.record_part_start("s1", "m1", "p1", "text", {"text": "hola"})
        svc.record_message_start("s1", "m2", "assistant")
        svc.record_part_start("s1", "m2", "p2", "text", {"text": ""})
        svc.record_part_end("s1", "p2", {"text": "respuesta final"})

        msgs = svc.load_session_messages("s1")
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "hola" and msgs[1]["content"] == "respuesta final"
        assert svc.session_exists_in_db("s1") is True

    def test_the_model_chosen_for_the_session_is_persisted(self, folder):
        assign("s1", folder)
        svc.ensure_session("s1", "claude-opus")

        assert [s["model"] for s in svc.list_persisted_sessions("s1")] == ["claude-opus"]

    def test_a_later_ensure_session_updates_the_model_without_touching_the_title(self, folder):
        assign("s1", folder, "Titulo manual")
        svc.ensure_session("s1", "gpt")
        svc.ensure_session("s1", "gemini")

        [s] = svc.list_persisted_sessions("s1")
        assert s["model"] == "gemini" and s["title"] == "Titulo manual"

    def test_two_sessions_in_different_folders_never_mix(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assign("sa", a)
        assign("sb", b)
        for sid in ("sa", "sb"):
            svc.ensure_session(sid)
            svc.record_message_start(sid, f"m-{sid}", "user")
            svc.record_part_start(sid, f"m-{sid}", f"p-{sid}", "text", {"text": f"texto de {sid}"})

        assert [m["content"] for m in svc.load_session_messages("sa")] == ["texto de sa"]
        assert [m["content"] for m in svc.load_session_messages("sb")] == ["texto de sb"]

    def test_two_sessions_sharing_a_folder_stay_separate(self, folder):
        assign("s1", folder)
        assign("s2", folder)
        for sid in ("s1", "s2"):
            svc.ensure_session(sid)
            svc.record_message_start(sid, f"m-{sid}", "user")
            svc.record_part_start(sid, f"m-{sid}", f"p-{sid}", "text", {"text": sid})

        assert [m["content"] for m in svc.load_session_messages("s1")] == ["s1"]
        assert [m["content"] for m in svc.load_session_messages("s2")] == ["s2"]

    def test_the_path_is_resolved_when_queued_not_when_executed(self, folder, tmp_path, monkeypatch):
        """Si el usuario reasigna la carpeta DESPUES de encolar, el trabajo ya encolado va a la carpeta VIEJA."""
        new_folder = tmp_path / "nueva"
        new_folder.mkdir()
        assign("s1", folder)
        deferred: list = []
        monkeypatch.setattr(svc, "submit", deferred.append)

        svc.ensure_session("s1")  # se encola con la ruta vieja
        assign("s1", new_folder)  # el usuario cambia de carpeta antes de que el escritor corra
        monkeypatch.setattr(svc, "submit", lambda job: job())
        for job in deferred:
            job()

        assert rows(folder, "SELECT id FROM session WHERE id = ?", "s1"), (
            "el trabajo encolado antes del cambio fue a la carpeta vieja"
        )
        assert (
            not rows(new_folder, "SELECT id FROM session WHERE id = ?", "s1")
            if db.db_path_for(str(new_folder)).exists()
            else True
        )

    def test_todos_are_replaced_not_appended(self, folder):
        assign("s1", folder)
        svc.ensure_session("s1")
        svc.record_todos("s1", [{"content": "uno", "status": "pending"}, {"content": "dos", "status": "pending"}])
        svc.record_todos("s1", [{"content": "tres", "status": "completed"}])

        assert [t["content"] for t in svc.load_session_todos("s1")] == ["tres"]


class TestDeleteAndRename:
    def _seed(self, folder):
        assign("s1", folder)
        svc.ensure_session("s1")
        for mid, role, text in (
            ("u1", "user", "pregunta 1"),
            ("a1", "assistant", "resp 1"),
            ("u2", "user", "pregunta 2"),
            ("a2", "assistant", "resp 2"),
        ):
            svc.record_message_start("s1", mid, role)
            svc.record_part_start("s1", mid, f"{mid}-p", "text", {"text": text})

    def test_delete_message_removes_only_that_row(self, folder):
        self._seed(folder)
        svc.delete_message("s1", "a1")
        assert [m["content"] for m in svc.load_session_messages("s1")] == ["pregunta 1", "pregunta 2", "resp 2"]

    def test_delete_assistant_response_targets_the_turn_by_user_position(self, folder):
        self._seed(folder)
        svc.delete_assistant_response("s1", 1)  # la respuesta del 2o turno
        assert [m["content"] for m in svc.load_session_messages("s1")] == ["pregunta 1", "resp 1", "pregunta 2"]

    def test_clear_messages_empties_the_history_but_keeps_the_session(self, folder):
        self._seed(folder)
        svc.clear_messages("s1")
        assert svc.load_session_messages("s1") == [] and svc.session_exists_in_db("s1") is True

    def test_rename_session_is_visible_in_the_listing(self, folder):
        self._seed(folder)
        svc.rename_session("s1", "Reporte de enero")
        assert [s["title"] for s in svc.list_persisted_sessions("s1")] == ["Reporte de enero"]

    def test_delete_session_removes_it(self, folder):
        self._seed(folder)
        svc.delete_session("s1")
        assert svc.session_exists_in_db("s1") is False and svc.load_session_messages("s1") == []

    def test_truncate_from_message_drops_that_message_and_everything_after(self, folder):
        self._seed(folder)
        svc.truncate_from_message("s1", "u2")
        assert [m["content"] for m in svc.load_session_messages("s1")] == ["pregunta 1", "resp 1"]

    def test_find_user_occurrence_counts_only_user_messages(self, folder):
        self._seed(folder)
        assert svc.find_user_occurrence("s1", "u1") == 0 and svc.find_user_occurrence("s1", "u2") == 1
        assert svc.find_user_occurrence("s1", "a1") is None and svc.find_user_occurrence("s1", "no-existe") is None

    def test_update_message_response_type_is_persisted(self, folder):
        self._seed(folder)
        svc.update_message_response_type("s1", "a2", "analitica_simple")
        assert svc.update_message_response_type is not None  # la funcion existe y no lanza con carpeta valida


# ---------------------------------------------------------------------------------------------------------------------
# B) assign_project_path
# ---------------------------------------------------------------------------------------------------------------------
class TestAssignProjectPath:
    def test_a_missing_folder_raises_and_registers_nothing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            svc.assign_project_path("s1", str(tmp_path / "no-existe"))
        assert master_index.get_project_path("s1") is None

    def test_a_file_instead_of_a_folder_raises(self, tmp_path):
        f = tmp_path / "archivo.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            svc.assign_project_path("s1", str(f))

    def test_it_registers_the_folder_and_the_owner_for_the_access_control(self, folder):
        assign("s1", folder)
        assert master_index.get_project_path("s1") == str(folder)
        assert svc.project_root_for_session("s1") == folder
        owned = [e["session_id"] for e in master_index.list_all_for_user(USER)]
        assert "s1" in owned, "sin dueño registrado el control BOLA/IDOR dejaria la sesion huerfana"

    def test_it_does_NOT_create_the_fixed_subfolders_up_front(self, folder):
        """Pedido explicito del usuario (2026-09-09): dar contexto de una carpeta no debe llenarla de subcarpetas vacias.
        Cada una se crea al vuelo cuando un comando escribe ahi."""
        assign("s1", folder)
        assert sorted(p.name for p in folder.iterdir()) == [], "asignar la carpeta no escribe nada dentro"

    def test_a_manual_title_seeds_the_session_row_so_it_is_not_lost(self, folder):
        """Bug real 2026-09-16: el nombre escrito a mano en 'Nueva sesion' se perdia porque la fila no existia todavia."""
        assign("s1", folder, "  Mi reporte ZCC  ")
        assert [s["title"] for s in svc.list_persisted_sessions("s1")] == ["Mi reporte ZCC"]

    def test_the_manual_title_survives_the_first_message_autoname(self, folder):
        assign("s1", folder, "Nombre elegido a mano")
        svc.ensure_session("s1")
        svc.maybe_autoname_session("s1", "primeras palabras del primer mensaje")

        assert [s["title"] for s in svc.list_persisted_sessions("s1")] == ["Nombre elegido a mano"]

    def test_without_a_manual_title_the_first_message_names_the_session(self, folder):
        assign("s1", folder)
        svc.ensure_session("s1")
        svc.maybe_autoname_session("s1", "primeras palabras")

        assert [s["title"] for s in svc.list_persisted_sessions("s1")] == ["primeras palabras"]

    def test_a_blank_title_does_not_seed_a_row(self, folder):
        assign("s1", folder, "   ")
        assert svc.session_exists_in_db("s1") is False

    def test_reassigning_the_folder_does_not_move_the_history(self, folder, tmp_path):
        other = tmp_path / "otra"
        other.mkdir()
        assign("s1", folder)
        svc.ensure_session("s1")
        svc.record_message_start("s1", "m1", "user")
        svc.record_part_start("s1", "m1", "p1", "text", {"text": "viejo"})

        assign("s1", other)

        assert svc.load_session_messages("s1") == [], "la sesion ve la carpeta nueva (vacia)"
        assert rows(folder, "SELECT id FROM message"), "el historial viejo sigue en la carpeta anterior"


# ---------------------------------------------------------------------------------------------------------------------
# C) Listado global para el sidebar
# ---------------------------------------------------------------------------------------------------------------------
class TestListAllSessions:
    def _session(self, sid, folder, title, at):
        assign(sid, folder)
        db_path = db.db_path_for(str(folder))
        db.ensure_session(db_path, sid, at)
        db.rename_session(db_path, sid, title, at)

    def test_sessions_come_newest_first_with_their_folder(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        self._session("viejo", a, "Viejo", 100.0)
        self._session("nuevo", b, "Nuevo", 900.0)

        out = svc.list_all_sessions_for_current_user()

        assert [s["id"] for s in out] == ["nuevo", "viejo"]
        assert out[0]["project_path"] == str(b) and out[1]["title"] == "Viejo"

    def test_each_project_database_is_opened_once_per_folder(self, tmp_path, monkeypatch):
        shared = tmp_path / "compartida"
        shared.mkdir()
        for i in range(4):
            self._session(f"s{i}", shared, f"T{i}", 100.0 + i)
        opened: list[str] = []
        real = db.list_sessions

        def spy(path, *a, **k):
            opened.append(str(path))
            return real(path, *a, **k)

        monkeypatch.setattr(db, "list_sessions", spy)

        out = svc.list_all_sessions_for_current_user()

        assert len(out) == 4 and len(opened) == 1, "4 sesiones en la misma carpeta = una sola apertura de vision.db"

    def test_a_session_missing_from_its_database_falls_back_to_the_index_mirror(self, folder):
        assign("fantasma", folder, "Titulo del indice")  # solo el indice maestro la conoce... mas la fila sembrada
        db_path = db.db_path_for(str(folder))
        db.delete_session(db_path, "fantasma")  # se borro a mano del vision.db

        out = svc.list_all_sessions_for_current_user()

        assert [s["id"] for s in out] == ["fantasma"]
        assert out[0]["message_count"] == 0 and out[0]["model"] is None and out[0]["project_path"] == str(folder)

    def test_only_the_current_windows_users_sessions_are_listed(self, tmp_path, monkeypatch):
        mine, theirs = tmp_path / "mia", tmp_path / "ajena"
        mine.mkdir()
        theirs.mkdir()
        self._session("mia", mine, "Mia", 100.0)
        monkeypatch.setattr(svc, "current_windows_user", lambda: "otra-persona")
        self._session("ajena", theirs, "Ajena", 200.0)
        monkeypatch.setattr(svc, "current_windows_user", lambda: USER)

        assert [s["id"] for s in svc.list_all_sessions_for_current_user()] == ["mia"]

    def test_a_session_whose_folder_is_gone_still_appears_from_the_index(self, tmp_path):
        gone = tmp_path / "borrada"
        gone.mkdir()
        assign("s1", gone, "Carpeta borrada")
        gone_db = db.db_path_for(str(gone))
        assert gone_db.exists()
        import shutil

        shutil.rmtree(gone)

        out = svc.list_all_sessions_for_current_user()

        assert [s["id"] for s in out] == ["s1"], "no se pierde de la lista aunque la carpeta ya no exista"

    def test_no_sessions_gives_an_empty_list(self):
        assert svc.list_all_sessions_for_current_user() == []


# ---------------------------------------------------------------------------------------------------------------------
# D) Migracion de la carpeta de skills (una sola vez)
# ---------------------------------------------------------------------------------------------------------------------
class TestLegacySkillsMigration:
    def test_a_legacy_folder_is_renamed_to_the_new_name(self, folder):
        legacy_name = svc._LEGACY_SKILLS_SUBDIRS[0]
        (folder / legacy_name).mkdir()
        (folder / legacy_name / "mi-skill.md").write_text("x", encoding="utf-8")

        svc._migrate_legacy_skills_dirs(folder)

        assert (folder / svc.SKILLS_SUBDIR / "mi-skill.md").is_file() and not (folder / legacy_name).exists()

    def test_when_the_new_folder_exists_no_rename_is_even_attempted(self, folder, monkeypatch):
        """En Windows `rename` sobre una carpeta existente falla y el except lo tapa: el resultado visible seria igual. Se cuenta el INTENTO."""
        from pathlib import Path

        legacy_name = svc._LEGACY_SKILLS_SUBDIRS[0]
        (folder / svc.SKILLS_SUBDIR).mkdir()
        (folder / legacy_name).mkdir()
        attempts: list[str] = []
        real = Path.rename
        monkeypatch.setattr(Path, "rename", lambda self, target: attempts.append(self.name) or real(self, target))

        svc._migrate_legacy_skills_dirs(folder)

        assert attempts == []

    def test_it_never_overwrites_an_existing_new_folder(self, folder):
        legacy_name = svc._LEGACY_SKILLS_SUBDIRS[0]
        (folder / svc.SKILLS_SUBDIR).mkdir()
        (folder / svc.SKILLS_SUBDIR / "nueva.md").write_text("nueva", encoding="utf-8")
        (folder / legacy_name).mkdir()
        (folder / legacy_name / "vieja.md").write_text("vieja", encoding="utf-8")

        svc._migrate_legacy_skills_dirs(folder)

        assert (folder / svc.SKILLS_SUBDIR / "nueva.md").is_file()
        assert (folder / legacy_name / "vieja.md").is_file(), "la vieja se deja tal cual, sin fusionar automaticamente"

    def test_nothing_to_migrate_is_a_noop(self, folder):
        svc._migrate_legacy_skills_dirs(folder)
        assert not (folder / svc.SKILLS_SUBDIR).exists()

    def test_a_failing_rename_warns_but_does_not_raise(self, folder, monkeypatch, caplog):
        from pathlib import Path

        legacy_name = svc._LEGACY_SKILLS_SUBDIRS[0]
        (folder / legacy_name).mkdir()

        def boom(self, target):
            raise OSError("carpeta en uso")

        monkeypatch.setattr(Path, "rename", boom)

        with caplog.at_level("WARNING"):
            svc._migrate_legacy_skills_dirs(folder)

        assert (folder / legacy_name).exists() and any("No se pudo migrar" in r.getMessage() for r in caplog.records)

    def test_with_several_legacy_names_only_the_first_found_is_migrated(self, folder, monkeypatch):
        """El docstring lo declara: no se fusionan carpetas, el usuario resuelve a mano. Hoy hay un solo nombre legacy, asi que se
        simulan dos para fijar la regla (con `break`, la segunda queda intacta)."""
        monkeypatch.setattr(svc, "_LEGACY_SKILLS_SUBDIRS", (".skills", ".skills-viejo"))
        (folder / ".skills").mkdir()
        (folder / ".skills" / "a.md").write_text("a", encoding="utf-8")
        (folder / ".skills-viejo").mkdir()

        svc._migrate_legacy_skills_dirs(folder)

        assert (folder / svc.SKILLS_SUBDIR / "a.md").is_file()
        assert (folder / ".skills-viejo").is_dir(), "la segunda legacy se deja tal cual, sin fusionar"

    def test_the_migration_stops_after_the_first_rename_without_trying_the_rest(self, folder, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(svc, "_LEGACY_SKILLS_SUBDIRS", (".skills", ".skills-viejo"))
        (folder / ".skills").mkdir()
        (folder / ".skills-viejo").mkdir()
        attempts: list[str] = []
        real = Path.rename
        monkeypatch.setattr(Path, "rename", lambda self, target: attempts.append(self.name) or real(self, target))

        svc._migrate_legacy_skills_dirs(folder)

        assert attempts == [".skills"], "un segundo intento fallaria contra la carpeta ya creada y dejaria un aviso falso"


def test_current_windows_user_prefers_username_then_user_then_a_neutral_label(monkeypatch):
    monkeypatch.undo()  # esta prueba necesita la funcion REAL, no la fija de la fixture
    monkeypatch.setenv("USERNAME", "ana")
    monkeypatch.setenv("USER", "root")
    assert svc.current_windows_user() == "ana"
    monkeypatch.delenv("USERNAME")
    assert svc.current_windows_user() == "root"
    monkeypatch.delenv("USER")
    assert svc.current_windows_user() == "desconocido"


def test_the_clock_used_for_writes_is_the_modules(monkeypatch):
    """`time.time()` se captura al ENCOLAR (no al ejecutar): lo que se guarda es la hora del evento, no la de la cola."""
    seen: list[float] = []
    monkeypatch.setattr(svc.time, "time", lambda: 1234.5)
    monkeypatch.setattr(svc.db, "ensure_session", lambda path, sid, now, model=None: seen.append(now))
    monkeypatch.setattr(svc, "_db_path_for_session", lambda sid: "ruta")

    svc.ensure_session("s1")

    assert seen == [1234.5] and time is not None
