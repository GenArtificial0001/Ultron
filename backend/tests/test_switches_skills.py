"""Interruptor POR skill, manteniendo el general (spec interruptores-conexiones-skills REQ-009/010).

Se usan skills reales en una carpeta temporal y una base maestra temporal. Lo que se verifica: una skill apagada no aparece en lo que ve el modelo (`<available_skills>`) ni se
puede cargar con la tool `skill`; el interruptor GENERAL sigue mandando por encima; las skills LOCALES de la sesion tambien se pueden apagar; y nada de esto cambia si no se apago nada."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import switches as sw
from app.persistence import master_index
from app.tools import skill as skill_mod


def make_skill(root: Path, name: str, description: str = "descripcion de prueba") -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\ncuerpo de {name}\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    store: dict[str, str] = {}
    monkeypatch.setattr(master_index, "get_app_setting", lambda k: store.get(k))
    monkeypatch.setattr(master_index, "set_app_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(master_index, "unset_app_setting", lambda k: store.pop(k, None))
    globales = tmp_path / "globales"
    globales.mkdir()
    for name in ("auditoria", "slotting", "cobertura"):
        make_skill(globales, name, f"hace {name}")
    monkeypatch.setattr(skill_mod, "discover_skills", lambda: skill_mod._scan_skills_dir(globales) if store.get(skill_mod.SKILLS_GLOBAL_ENABLED_KEY) == "true" else [])
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: [])
    store[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "true"
    yield store


def names_in_prompt(session_id=None) -> set[str]:
    xml = skill_mod.list_available_skills_xml(session_id)
    return {line.split("<name>")[1].split("</name>")[0] for line in xml.splitlines() if "<name>" in line}


class Session:
    def __init__(self, session_id="sid-1"):
        self.session_id = session_id


# ---------------------------------------------------------------------------------------------------------------------
# REQ-009 lo que ve el modelo
# ---------------------------------------------------------------------------------------------------------------------
def test_with_nothing_off_the_prompt_lists_every_skill_as_before():
    assert names_in_prompt() == {"auditoria", "slotting", "cobertura"}


def test_a_skill_turned_off_is_not_shown_to_the_model_neither_name_nor_description():
    sw.set_skill_enabled("slotting", False, {"auditoria", "slotting", "cobertura"})

    xml = skill_mod.list_available_skills_xml()

    assert names_in_prompt() == {"auditoria", "cobertura"}
    assert "slotting" not in xml and "hace slotting" not in xml


def test_turning_it_back_on_shows_it_again():
    known = {"auditoria", "slotting", "cobertura"}
    sw.set_skill_enabled("slotting", False, known)
    sw.set_skill_enabled("slotting", True, known)

    assert names_in_prompt() == known


def test_the_general_switch_off_hides_every_global_skill_whatever_the_individual_ones(_isolated):
    _isolated[skill_mod.SKILLS_GLOBAL_ENABLED_KEY] = "false"

    assert names_in_prompt() == set() and skill_mod.list_available_skills_xml() == ""


def test_the_general_switch_on_shows_only_the_ones_not_turned_off(_isolated):
    sw.set_skill_enabled("auditoria", False, {"auditoria", "slotting", "cobertura"})
    sw.set_skill_enabled("cobertura", False, {"auditoria", "slotting", "cobertura"})

    assert names_in_prompt() == {"slotting"}


def test_turning_every_skill_off_leaves_an_empty_block():
    known = {"auditoria", "slotting", "cobertura"}
    for name in known:
        sw.set_skill_enabled(name, False, known)

    assert skill_mod.list_available_skills_xml() == ""


def test_local_session_skills_can_be_turned_off_too(monkeypatch, tmp_path):
    locales = tmp_path / "locales"
    locales.mkdir()
    make_skill(locales, "mi-skill-local", "solo de esta sesion")
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: skill_mod._scan_skills_dir(locales))

    assert "mi-skill-local" in names_in_prompt("sid-1")
    sw.set_skill_enabled("mi-skill-local", False, {"mi-skill-local"})

    assert "mi-skill-local" not in names_in_prompt("sid-1")


def test_a_local_skill_with_the_same_name_as_a_global_one_is_turned_off_together(monkeypatch, tmp_path):
    """Local pisa a global en una colision de nombre; el interruptor es POR NOMBRE, asi que apagar `slotting` apaga a las dos (no queda una colada)."""
    locales = tmp_path / "locales"
    locales.mkdir()
    make_skill(locales, "slotting", "version local")
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: skill_mod._scan_skills_dir(locales))
    sw.set_skill_enabled("slotting", False, {"slotting"})

    assert "slotting" not in names_in_prompt("sid-1")


# ---------------------------------------------------------------------------------------------------------------------
# REQ-010 la tool `skill`
# ---------------------------------------------------------------------------------------------------------------------
def test_an_enabled_skill_loads_as_before():
    out = skill_mod._skill_execute({"name": "slotting"}, Session())

    assert out.startswith('<skill_content name="slotting">') and "cuerpo de slotting" in out


def test_a_disabled_skill_cannot_be_loaded_and_its_content_never_leaves_the_disk():
    sw.set_skill_enabled("slotting", False, {"auditoria", "slotting", "cobertura"})

    out = skill_mod._skill_execute({"name": "slotting"}, Session())

    assert out.startswith("Error:") and "desactivada" in out and "cuerpo de slotting" not in out


def test_the_other_skills_still_load_when_one_is_off():
    sw.set_skill_enabled("slotting", False, {"auditoria", "slotting", "cobertura"})

    assert "cuerpo de auditoria" in skill_mod._skill_execute({"name": "auditoria"}, Session())


def test_the_list_of_available_skills_in_the_error_hides_the_disabled_ones():
    sw.set_skill_enabled("slotting", False, {"auditoria", "slotting", "cobertura"})

    out = skill_mod._skill_execute({"name": "no-existe"}, Session())

    assert out.startswith("Error: no existe la skill") and "auditoria" in out and "cobertura" in out and "slotting" not in out


def test_loading_a_skill_that_does_not_exist_still_says_which_exist():
    out = skill_mod._skill_execute({"name": "inventada"}, Session())

    assert "Disponibles:" in out and "slotting" in out


def test_the_disabled_skill_message_does_not_expose_its_path_or_content():
    sw.set_skill_enabled("slotting", False, {"auditoria", "slotting", "cobertura"})

    out = skill_mod._skill_execute({"name": "slotting"}, Session())

    assert "SKILL.md" not in out and str(Path.home()) not in out


def test_a_missing_name_keeps_its_original_error():
    assert skill_mod._skill_execute({}, Session()) == "Error: falta el argumento 'name'."


def test_a_skill_with_capital_letters_stays_off_under_its_exact_name(_isolated):
    """Los nombres de skills distinguen mayusculas. Se guarda el nombre EXACTO: si se guardara en minuscula, «Mi Skill 2» se mostraria apagada en la pantalla pero el filtro
    (que compara por el nombre real) la seguiria ofreciendo al modelo."""
    known = {"Mi Skill 2", "auditoria"}
    sw.set_skill_enabled("Mi Skill 2", False, known)

    assert sw.is_skill_enabled("Mi Skill 2") is False
    assert sw.disabled_skill_names() == {"Mi Skill 2"}
    assert sw.is_skill_enabled("auditoria") is True


def test_a_capitalized_skill_turned_off_is_really_hidden_from_the_model(monkeypatch, tmp_path):
    locales = tmp_path / "locales_mayus"
    locales.mkdir()
    make_skill(locales, "Mi Skill 2", "con mayusculas")
    monkeypatch.setattr(skill_mod, "discover_local_skills", lambda session_id: skill_mod._scan_skills_dir(locales))
    assert "Mi Skill 2" in names_in_prompt("sid-1")

    sw.set_skill_enabled("Mi Skill 2", False, {"Mi Skill 2"})

    assert "Mi Skill 2" not in names_in_prompt("sid-1")
    assert skill_mod._skill_execute({"name": "Mi Skill 2"}, Session("sid-1")).startswith("Error:")
