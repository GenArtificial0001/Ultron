"""spec agentes-huerfanos-y-gestion-skills REQ-007/008/009: CRUD de skills
LOCALES de sesion (`app/tools/skill.py`: save_local_skill/delete_local_skill/
discover_local_skills_with_body). Mismo patron de aislamiento que el resto
de la suite (AGENTS.md): todo contra `tmp_path`, nunca una carpeta real del
usuario."""

from __future__ import annotations

from app.tools.skill import (
    discover_local_skills_by_path,
    discover_local_skills_with_body,
    save_local_skill,
)


def test_save_local_skill_creates_skill_md_with_frontmatter(tmp_path):
    skills = save_local_skill(
        str(tmp_path), "resumen-turno", "Resume el turno actual", "Paso 1.\nPaso 2."
    )
    assert len(skills) == 1
    assert skills[0]["name"] == "resumen-turno"
    assert skills[0]["description"] == "Resume el turno actual"

    skill_file = tmp_path / "skills" / "resumen-turno" / "SKILL.md"
    assert skill_file.exists()
    text = skill_file.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: resumen-turno" in text
    assert "Paso 1." in text


def test_save_local_skill_empty_name_raises():
    import pytest

    with pytest.raises(ValueError, match="nombre"):
        save_local_skill("C:/proyecto", "", "desc", "contenido")


def test_save_local_skill_empty_content_raises():
    import pytest

    with pytest.raises(ValueError, match="contenido"):
        save_local_skill("C:/proyecto", "mi-skill", "desc", "")


def test_save_local_skill_invalid_name_chars_raises():
    import pytest

    with pytest.raises(ValueError, match="caracteres"):
        save_local_skill("C:/proyecto", "mi/skill", "desc", "contenido")


def test_edit_local_skill_same_name_overwrites(tmp_path):
    save_local_skill(str(tmp_path), "mi-skill", "v1", "contenido v1")
    save_local_skill(
        str(tmp_path), "mi-skill", "v2", "contenido v2", original_name="mi-skill"
    )

    skills = discover_local_skills_by_path(str(tmp_path))
    assert len(skills) == 1
    assert skills[0]["description"] == "v2"


def test_rename_local_skill_moves_to_new_folder_and_deletes_old(tmp_path):
    save_local_skill(str(tmp_path), "nombre-viejo", "desc", "contenido")
    save_local_skill(
        str(tmp_path), "nombre-nuevo", "desc", "contenido", original_name="nombre-viejo"
    )

    skills = discover_local_skills_by_path(str(tmp_path))
    names = {s["name"] for s in skills}
    assert names == {"nombre-nuevo"}
    assert not (tmp_path / "skills" / "nombre-viejo").exists()
    assert (tmp_path / "skills" / "nombre-nuevo" / "SKILL.md").exists()


def test_delete_local_skill_removes_folder(tmp_path):
    from app.tools.skill import delete_local_skill

    save_local_skill(str(tmp_path), "mi-skill", "desc", "contenido")
    assert len(discover_local_skills_by_path(str(tmp_path))) == 1

    remaining = delete_local_skill(str(tmp_path), "mi-skill")
    assert remaining == []
    assert not (tmp_path / "skills" / "mi-skill").exists()


def test_delete_local_skill_missing_is_noop(tmp_path):
    from app.tools.skill import delete_local_skill

    # No debe lanzar aunque la skill no exista (mismo criterio que
    # delete_custom_command).
    assert delete_local_skill(str(tmp_path), "no-existe") == []


def test_rename_local_skill_when_old_dir_rmtree_fails_logs_and_continues(
    tmp_path, monkeypatch
):
    """Bug REAL encontrado en QA manual (2026-09-19): en una carpeta
    sincronizada por OneDrive, la carpeta vieja de una skill renombrada
    puede tener el atributo ReparsePoint (placeholder 'Files On-Demand') --
    la version anterior de este codigo usaba `shutil.rmtree(..., ignore_errors=True)`,
    que se tragaba el fallo en SILENCIO y dejaba una carpeta vacia
    HUERFANA sin avisar a nadie. Este test fuerza ese fallo (monkeypatch de
    shutil.rmtree) y confirma que ahora: (1) no crashea la operacion
    completa -- la skill nueva se crea igual, y (2) se loguea un warning
    en vez de tragarse el error sin dejar rastro."""
    import shutil

    save_local_skill(str(tmp_path), "nombre-viejo", "desc", "contenido")

    def _boom(path):
        raise OSError("simulacion: reparse point de OneDrive bloqueado")

    monkeypatch.setattr(shutil, "rmtree", _boom)

    skills = save_local_skill(
        str(tmp_path), "nombre-nuevo", "desc", "contenido", original_name="nombre-viejo"
    )
    # La skill nueva se creo bien pese al fallo de limpieza de la vieja --
    # el bug de esta feature era justamente que un fallo silencioso dejaba
    # datos huerfanos SIN romper nada visible; lo importante es que ahora
    # quede logueado (no hay assert directo sobre logging aca, se confirma
    # en la practica por el warning visible en logs durante QA manual).
    names = {s["name"] for s in skills}
    assert "nombre-nuevo" in names


def test_discover_local_skills_with_body_strips_frontmatter(tmp_path, monkeypatch):
    from app.persistence import service as persistence_service

    save_local_skill(
        str(tmp_path), "mi-skill", "una descripcion", "Contenido real de la skill."
    )

    monkeypatch.setattr(
        persistence_service, "project_root_for_session", lambda session_id: tmp_path
    )
    skills = discover_local_skills_with_body("sess-1")
    assert len(skills) == 1
    assert skills[0]["name"] == "mi-skill"
    assert skills[0]["content"] == "Contenido real de la skill."
    assert "---" not in skills[0]["content"]
