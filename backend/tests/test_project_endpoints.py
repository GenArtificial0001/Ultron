"""Endpoints HTTP que escriben o borran dentro de la carpeta de proyecto de una sesion (`main.py`, BL-006): skills locales, comandos propios, asignar carpeta y renombrar.

Los modulos de fondo (`app/tools/skill.py`, `app/commands.py`) ya tienen sus tests; esto cubre la CAPA HTTP que los llama y que decide cosas que ellos no ven: que una sesion sin carpeta
asignada reciba un 400 claro (y no escriba en cualquier lado), que un nombre invalido llegue como 400 con mensaje y no como un 500, que la autorizacion por sesion (`_assert_session_owner`,
QA-032) se consulte en cada una (antes faltaba y alguien con un `session_id` ajeno podia escribir en la carpeta de otro) y que el renombrado valide el nombre en el SERVIDOR.

Sesion con carpeta real en `tmp_path` y base maestra aislada (fixtures de conftest). Sin modelo ni red.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.persistence import master_index
from app.persistence import service as persistence

SID = "sesion-proyecto"
OWNED_BY_OTHERS = "sesion-ajena"


@pytest.fixture
def owner_checks(monkeypatch):
    """Registra cada `_assert_session_owner(session_id)` y permite simular que la sesion es de otro usuario."""
    seen: list[str] = []

    def check(session_id):
        seen.append(session_id)
        if session_id == OWNED_BY_OTHERS:
            raise main.HTTPException(status_code=403, detail="Esta sesión es de otro usuario.")

    monkeypatch.setattr(main, "_assert_session_owner", check)
    return seen


@pytest.fixture
def client(owner_checks):
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(tmp_path, tmp_master_db):
    root = tmp_path / "proyecto"
    root.mkdir()
    master_index.set_project_path(
        SID, str(root), windows_user_created=f"{persistence.current_windows_user()}_20260929120000"
    )
    return root


# ---------------------------------------------------------------------------
# Skills locales
# ---------------------------------------------------------------------------
class TestLocalSkills:
    def save(self, client, **kw):
        body = {"session_id": SID, "name": "resumen-turno", "description": "Resume", "content": "Paso 1.", **kw}
        return client.post("/api/local-skills", json=body)

    def test_it_writes_the_skill_in_the_session_project_folder(self, client, project):
        r = self.save(client)

        assert r.status_code == 200
        assert [s["name"] for s in r.json()["skills"]] == ["resumen-turno"]
        assert (project / "skills" / "resumen-turno" / "SKILL.md").is_file()

    def test_a_session_without_a_project_folder_gets_a_clear_400_and_nothing_is_written(
        self, client, tmp_master_db, tmp_path
    ):
        r = self.save(client, session_id="sin-carpeta")

        assert r.status_code == 400 and "carpeta de proyecto" in r.json()["detail"]
        assert [p.name for p in tmp_path.iterdir() if p.name != "vision.db"] == []  # la base maestra de prueba vive en el mismo tmp_path; no cuenta

    @pytest.mark.parametrize("name", ["", "   ", "../escape", "con/barra", "con:dos", "con*estrella", "con?pregunta"])
    def test_an_invalid_name_is_a_400_with_a_message_not_a_500(self, client, project, name):
        r = self.save(client, name=name)

        assert r.status_code == 400 and r.json()["detail"].strip()
        assert not (project.parent / "escape").exists()  # y nunca escribe fuera de la carpeta del proyecto

    def test_renaming_with_original_name_replaces_the_old_skill(self, client, project):
        self.save(client, name="vieja")

        r = self.save(client, name="nueva", original_name="vieja")

        assert [s["name"] for s in r.json()["skills"]] == ["nueva"]
        assert not (project / "skills" / "vieja").exists()

    def test_it_asks_for_authorization_on_the_session_it_writes_to(self, client, project, owner_checks):
        self.save(client)

        assert owner_checks == [SID]

    def test_a_foreign_session_cannot_write_and_nothing_reaches_the_disk(self, client, project):
        r = self.save(client, session_id=OWNED_BY_OTHERS)

        assert r.status_code == 403
        assert not (project / "skills").exists()

    def test_deleting_removes_it_and_returns_what_is_left(self, client, project):
        self.save(client, name="una")
        self.save(client, name="dos")

        r = client.delete(f"/api/local-skills/{SID}/una")

        assert r.status_code == 200 and [s["name"] for s in r.json()["skills"]] == ["dos"]
        assert not (project / "skills" / "una").exists() and (project / "skills" / "dos").exists()

    def test_deleting_in_a_session_without_a_folder_is_an_empty_list_not_an_error(self, client, tmp_master_db):
        r = client.delete("/api/local-skills/sin-carpeta/x")

        assert r.status_code == 200 and r.json() == {"skills": []}

    def test_deleting_asks_for_authorization_and_a_foreign_session_cannot(self, client, project, owner_checks):
        client.delete(f"/api/local-skills/{SID}/x")
        denied = client.delete(f"/api/local-skills/{OWNED_BY_OTHERS}/x")

        assert owner_checks == [SID, OWNED_BY_OTHERS] and denied.status_code == 403


# ---------------------------------------------------------------------------
# Comandos propios
# ---------------------------------------------------------------------------
class TestCustomCommands:
    def save(self, client, **kw):
        return client.post(
            "/api/commands/custom", json={"session_id": SID, "name": "informe", "prompt": "Armame el informe", **kw}
        )

    def test_it_saves_the_command_in_the_project_folder(self, client, project):
        r = self.save(client)

        assert r.status_code == 200 and "informe" in str(r.json()["custom"])
        assert any(project.rglob("informe*"))

    def test_the_same_name_updates_instead_of_duplicating(self, client, project):
        self.save(client, prompt="Primera version")

        r = self.save(client, prompt="Segunda version")

        assert str(r.json()["custom"]).count("informe") >= 1
        assert "Segunda version" in "".join(
            p.read_text(encoding="utf-8") for p in project.rglob("informe*") if p.is_file()
        )
        assert "Primera version" not in "".join(
            p.read_text(encoding="utf-8") for p in project.rglob("informe*") if p.is_file()
        )

    def test_a_session_without_a_folder_gets_a_clear_400(self, client, tmp_master_db):
        r = self.save(client, session_id="sin-carpeta")

        assert r.status_code == 400 and "carpeta de proyecto" in r.json()["detail"]

    @pytest.mark.parametrize("name", ["", "../escape", "con/barra"])
    def test_an_invalid_name_is_a_400_and_nothing_escapes_the_folder(self, client, project, name):
        r = self.save(client, name=name)

        assert r.status_code == 400
        assert not (project.parent / "escape").exists()

    def test_a_foreign_session_cannot_write_commands_into_someone_elses_folder(self, client, project):
        """QA-032: antes faltaba `_assert_session_owner` y quien conociera un session_id ajeno escribia comandos en su carpeta."""
        r = self.save(client, session_id=OWNED_BY_OTHERS)

        assert r.status_code == 403
        assert not any(project.rglob("informe*"))


# ---------------------------------------------------------------------------
# Asignar la carpeta de una sesion
# ---------------------------------------------------------------------------
class TestAssignProjectPath:
    def assign(self, client, path, session_id="nueva-sesion", title=""):
        return client.post(f"/api/sessions/{session_id}/project-path", json={"path": path, "title": title})

    def test_it_assigns_the_folder_and_creates_the_fixed_structure(self, client, tmp_master_db, tmp_path):
        target = tmp_path / "mi-proyecto"
        target.mkdir()

        r = self.assign(client, str(target), title="Mi sesión")

        assert r.status_code == 200 and r.json() == {"assigned": True, "path": str(target)}
        assert persistence.project_root_for_session("nueva-sesion") is not None

    def test_the_name_typed_in_the_new_session_modal_is_kept_as_a_manual_title(self, client, tmp_master_db, tmp_path):
        """Bug real (2026-09-16): el nombre que la persona escribia a mano se perdia, porque el primer mensaje lo pisaba con sus primeros 40 caracteres. Se siembra
        en la base de la carpeta, marcado como manual (`title_manual=1`) para que ningun criterio automatico lo vuelva a pisar."""
        import sqlite3

        from app.persistence import db, writer

        target = tmp_path / "con-nombre"
        target.mkdir()

        self.assign(client, str(target), title="Planta Norte - Slotting")
        writer.flush()

        con = sqlite3.connect(str(db.db_path_for(str(target))))
        try:
            row = con.execute("SELECT title, title_manual FROM session WHERE id = ?", ("nueva-sesion",)).fetchone()
        finally:
            con.close()
        assert row == ("Planta Norte - Slotting", 1)

    def test_without_a_name_no_title_is_seeded(self, client, tmp_master_db, tmp_path):
        """Sin nombre escrito no se siembra ninguna fila de sesion: el titulo lo pone despues el primer mensaje (o el usuario)."""
        import sqlite3

        from app.persistence import db, writer

        target = tmp_path / "sin-nombre"
        target.mkdir()

        self.assign(client, str(target), title="")
        writer.flush()

        db_file = db.db_path_for(str(target))
        rows = 0
        if db_file.exists():  # puede no existir todavia (nada la creo): en ese caso hay 0 filas, que es justo lo esperado
            con = sqlite3.connect(str(db_file))
            try:
                rows = con.execute("SELECT COUNT(*) FROM session WHERE id = ?", ("nueva-sesion",)).fetchone()[0]
            finally:
                con.close()
        assert rows == 0

    def test_assigning_a_folder_does_not_fill_it_with_empty_subfolders(self, client, tmp_master_db, tmp_path):
        """Pedido del usuario (2026-09-09): a veces solo quiere dar contexto de una carpeta de archivos, sin ejecutar comandos ni guardar nada. Las subcarpetas
        (skills/commands/decisions/sessions) se crean al vuelo cuando algo escribe dentro, no al asignar."""
        target = tmp_path / "solo-contexto"
        target.mkdir()
        (target / "datos.csv").write_text("a,b", encoding="utf-8")

        self.assign(client, str(target))

        assert sorted(p.name for p in target.iterdir()) == ["datos.csv"]

    @pytest.mark.parametrize("path", ["", "   "])
    def test_an_empty_path_is_a_400_before_touching_anything(self, client, tmp_master_db, path):
        r = self.assign(client, path)

        assert r.status_code == 400 and "no puede estar vacía" in r.json()["detail"]
        assert persistence.project_root_for_session("nueva-sesion") is None

    def test_a_folder_that_does_not_exist_is_a_400_with_a_message(self, client, tmp_master_db, tmp_path):
        r = self.assign(client, str(tmp_path / "no-existe"))

        assert r.status_code == 400 and r.json()["detail"].strip()
        assert persistence.project_root_for_session("nueva-sesion") is None

    def test_the_path_is_trimmed(self, client, tmp_master_db, tmp_path):
        target = tmp_path / "recortada"
        target.mkdir()

        r = self.assign(client, f"  {target}  ")

        assert r.status_code == 200 and r.json()["path"] == str(target)

    def test_a_foreign_session_cannot_be_assigned_a_folder(self, client, tmp_master_db, tmp_path):
        target = tmp_path / "x"
        target.mkdir()

        r = self.assign(client, str(target), session_id=OWNED_BY_OTHERS)

        assert r.status_code == 403 and persistence.project_root_for_session(OWNED_BY_OTHERS) is None


# ---------------------------------------------------------------------------
# Renombrar una sesion
# ---------------------------------------------------------------------------
class TestRenameSession:
    def rename(self, client, title, session_id=SID):
        return client.patch(f"/api/sessions/{session_id}", json={"title": title})

    @pytest.fixture(autouse=True)
    def route(self):
        for r in main.app.routes:
            if getattr(r, "name", "") == "rename_session_endpoint":
                self.method = sorted(r.methods)[0]
                self.path = r.path
                return
        pytest.fail("no se encontro la ruta de renombrar")

    def call(self, client, title, session_id=SID):
        return client.request(self.method, self.path.replace("{session_id}", session_id), json={"title": title})

    def test_it_renames_and_returns_the_saved_title(self, client, project, monkeypatch):
        saved = []
        monkeypatch.setattr(main.persistence_service, "rename_session", lambda sid, title: saved.append((sid, title)))

        r = self.call(client, "  Reunión de planta  ")

        assert r.status_code == 200 and r.json() == {"renamed": True, "title": "Reunión de planta"}
        assert saved == [(SID, "Reunión de planta")]

    @pytest.mark.parametrize("title", ["", "   ", "\n\t"])
    def test_an_empty_name_is_refused_by_the_server_and_nothing_is_saved(self, client, project, monkeypatch, title):
        saved = []
        monkeypatch.setattr(main.persistence_service, "rename_session", lambda sid, t: saved.append(t))

        r = self.call(client, title)

        assert r.status_code == 400 and "no puede estar vacío" in r.json()["detail"] and saved == []

    def test_a_long_name_is_cut_to_eighty_characters(self, client, project, monkeypatch):
        saved = []
        monkeypatch.setattr(main.persistence_service, "rename_session", lambda sid, t: saved.append(t))

        r = self.call(client, "x" * 200)

        assert len(r.json()["title"]) == 80 and len(saved[0]) == 80

    def test_exactly_eighty_characters_are_kept_whole(self, client, project, monkeypatch):
        monkeypatch.setattr(main.persistence_service, "rename_session", lambda sid, t: None)

        assert len(self.call(client, "y" * 80).json()["title"]) == 80

    def test_a_foreign_session_cannot_be_renamed(self, client, project, monkeypatch):
        saved = []
        monkeypatch.setattr(main.persistence_service, "rename_session", lambda sid, t: saved.append(t))

        r = self.call(client, "Robado", session_id=OWNED_BY_OTHERS)

        assert r.status_code == 403 and saved == []
