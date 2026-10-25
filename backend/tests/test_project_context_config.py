"""Configuracion del proyecto y explorador de carpetas (`app/project_context.py`, BL-006).

`test_project_tree.py` ya cubre el arbol de archivos; esto cubre lo que no tenia ninguna prueba: guardar y releer `project_config.json`, los setters
(SharePoint, skills, parametros de generacion, salto de modelos, umbrales) y el explorador de carpetas del que se sirven Configuracion > Proyecto local y Skills.
El archivo de configuracion es siempre el de `tmp_path` (fixture autouse de conftest): nunca se toca el real.
"""

from __future__ import annotations

import json

import pytest

from app import project_context as pc


# ---------------------------------------------------------------------------
# Leer y guardar
# ---------------------------------------------------------------------------
class TestLoadAndSave:
    def test_without_a_file_the_defaults_are_used(self):
        cfg = pc.get_project_config()

        assert cfg.attachment_warn_mb == 100 and cfg.attachment_inplace_mb == 500
        assert cfg.model_fallback_enabled is False and cfg.model_fallback_pause_minutes == 10

    def test_a_corrupt_file_falls_back_to_the_defaults_instead_of_failing(self):
        pc.CONFIG_FILE.write_text("{esto no es json", encoding="utf-8")

        assert pc.get_project_config() == pc.ProjectConfig()

    def test_a_json_that_is_not_an_object_falls_back_to_the_defaults(self):
        pc.CONFIG_FILE.write_text("[1, 2, 3]", encoding="utf-8")

        assert pc.get_project_config() == pc.ProjectConfig()

    def test_unknown_keys_in_the_file_are_ignored(self):
        """Un archivo escrito por una version mas nueva (o a mano) con claves que esta version no conoce no la rompe."""
        pc.CONFIG_FILE.write_text(json.dumps({"attachment_warn_mb": 7, "clave_del_futuro": 1}), encoding="utf-8")

        assert pc.get_project_config().attachment_warn_mb == 7

    def test_what_is_saved_is_read_back(self):
        pc.set_skills_repo_path("C:/skills")

        assert pc.get_project_config().skills_repo_path == "C:/skills"

    def test_saving_creates_the_folder_and_keeps_accents_readable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pc, "CONFIG_FILE", tmp_path / "nueva" / "carpeta" / "cfg.json")

        pc.set_skills_repo_path("C:/Mañana")

        text = pc.CONFIG_FILE.read_text(encoding="utf-8")
        assert (
            "Mañana" in text and "\\u00f1" not in text
        )  # ensure_ascii=False: el archivo se puede leer y editar a mano


# ---------------------------------------------------------------------------
# Setters
# ---------------------------------------------------------------------------
class TestSetters:
    def test_sharepoint_project_is_saved_and_cleared(self):
        pc.set_sharepoint_project("drv", "Digital", "fld", "Material Flow/Slotting")
        cfg = pc.get_project_config()
        assert (
            cfg.sharepoint_drive_id,
            cfg.sharepoint_drive_name,
            cfg.sharepoint_folder_id,
            cfg.sharepoint_folder_path,
        ) == (
            "drv",
            "Digital",
            "fld",
            "Material Flow/Slotting",
        )

        pc.clear_sharepoint_project()
        cleared = pc.get_project_config()
        assert (
            cleared.sharepoint_drive_id,
            cleared.sharepoint_drive_name,
            cleared.sharepoint_folder_id,
            cleared.sharepoint_folder_path,
        ) == ("", "", "", "")

    def test_clearing_sharepoint_keeps_the_other_settings(self):
        pc.set_skills_repo_path("C:/skills")
        pc.set_sharepoint_project("drv", "Digital", "fld", "x")

        pc.clear_sharepoint_project()

        assert pc.get_project_config().skills_repo_path == "C:/skills"

    def test_generation_params_update_only_known_fields_and_none_clears_one(self):
        pc.set_generation_params(temperature=0.2, max_tokens=500, campo_inventado=1)
        cfg = pc.get_project_config()
        assert cfg.temperature == 0.2 and cfg.max_tokens == 500 and not hasattr(cfg, "campo_inventado")

        pc.set_generation_params(temperature=None)  # None explicito = volver al default del modelo
        cfg = pc.get_project_config()
        assert cfg.temperature is None and cfg.max_tokens == 500  # y no toca los demas

    def test_connection_thresholds_update_only_known_fields_and_none_returns_to_the_env_default(self):
        pc.set_connection_thresholds(bq_cost_guardrail_bytes=123, export_row_threshold=50, basura=9)
        cfg = pc.get_project_config()
        assert cfg.bq_cost_guardrail_bytes == 123 and cfg.export_row_threshold == 50 and not hasattr(cfg, "basura")

        pc.set_connection_thresholds(bq_cost_guardrail_bytes=None)
        assert pc.get_project_config().bq_cost_guardrail_bytes is None

    def test_model_fallback_none_leaves_each_field_alone(self):
        pc.set_model_fallback(enabled=True, order=["a", "b"], pause_minutes=30)
        pc.set_model_fallback()  # todo None: no cambia nada

        cfg = pc.get_project_config()
        assert (
            cfg.model_fallback_enabled is True
            and cfg.model_fallback_order == ["a", "b"]
            and cfg.model_fallback_pause_minutes == 30
        )

    def test_model_fallback_can_be_turned_off_and_the_order_is_copied(self):
        order = ["x"]
        returned = pc.set_model_fallback(enabled=True, order=order)
        order.append("mutada despues")  # modificar la lista de afuera no cambia lo guardado ni lo devuelto

        assert pc.get_project_config().model_fallback_order == ["x"]
        assert returned.model_fallback_order == ["x"] and returned.model_fallback_order is not order
        pc.set_model_fallback(enabled=False)
        assert pc.get_project_config().model_fallback_enabled is False  # False NO es 'no tocar'


    def test_a_zero_pause_is_a_value_not_a_request_to_leave_it_alone(self):
        pc.set_model_fallback(pause_minutes=30)

        pc.set_model_fallback(pause_minutes=0)

        assert pc.get_project_config().model_fallback_pause_minutes == 0  # solo None significa 'no tocar'

    def test_an_empty_order_is_saved_not_ignored(self):
        pc.set_model_fallback(order=["a"])

        pc.set_model_fallback(order=[])

        assert pc.get_project_config().model_fallback_order == []


class TestAttachmentThresholds:
    def test_they_are_saved(self):
        cfg = pc.set_attachment_thresholds(warn_mb=50, inplace_mb=200)

        assert (cfg.attachment_warn_mb, cfg.attachment_inplace_mb) == (50, 200)
        assert pc.get_project_config().attachment_inplace_mb == 200

    def test_a_missing_field_keeps_the_current_value(self):
        pc.set_attachment_thresholds(warn_mb=50, inplace_mb=200)

        cfg = pc.set_attachment_thresholds(warn_mb=60)

        assert (cfg.attachment_warn_mb, cfg.attachment_inplace_mb) == (60, 200)

    @pytest.mark.parametrize(("warn", "inplace"), [(0, 500), (100, 0), (-1, 500)])
    def test_less_than_one_mb_is_refused(self, warn, inplace):
        with pytest.raises(ValueError, match="al menos 1 MB"):
            pc.set_attachment_thresholds(warn_mb=warn, inplace_mb=inplace)

    def test_inplace_below_warn_is_refused_with_a_clear_reason(self):
        with pytest.raises(ValueError, match="no puede ser menor que el de aviso"):
            pc.set_attachment_thresholds(warn_mb=100, inplace_mb=99)

    def test_equal_values_are_allowed(self):
        assert pc.set_attachment_thresholds(warn_mb=10, inplace_mb=10).attachment_inplace_mb == 10

    def test_a_refused_change_saves_nothing(self):
        before = pc.get_project_config().attachment_warn_mb
        with pytest.raises(ValueError):
            pc.set_attachment_thresholds(warn_mb=999, inplace_mb=1)

        assert pc.get_project_config().attachment_warn_mb == before


# ---------------------------------------------------------------------------
# Explorador de carpetas
# ---------------------------------------------------------------------------
class TestLocalRoots:
    def test_roots_are_existing_drives_with_a_windows_style_name(self):
        roots = pc.list_local_roots()

        assert roots and all(r["name"].endswith(":\\") and len(r["name"]) == 3 for r in roots)
        assert any(r["name"] == "C:\\" for r in roots)

    def test_only_drives_that_exist_are_listed(self):
        """Sin el filtro `exists()` saldrian las 26 letras, incluidas las unidades que esta maquina no tiene."""
        from pathlib import Path

        roots = pc.list_local_roots()

        assert all(Path(r["path"]).exists() for r in roots)
        assert len(roots) < 26


class TestLocalChildren:
    def test_only_folders_are_listed_sorted_without_regard_to_case(self, tmp_path):
        for name in ("beta", "Zeta", "alfa", "Beto"):
            (tmp_path / name).mkdir()
        (tmp_path / "archivo.txt").write_text("x", encoding="utf-8")

        # Sin `.lower()` toda mayuscula iria antes que cualquier minuscula (Beto, Zeta, alfa, beta).
        assert [e["name"] for e in pc.list_local_children(str(tmp_path))] == ["alfa", "beta", "Beto", "Zeta"]

    def test_hidden_and_ignored_folders_are_skipped(self, tmp_path):
        for name in (".git", ".oculta", "node_modules", "__pycache__", "visible"):
            (tmp_path / name).mkdir()

        assert [e["name"] for e in pc.list_local_children(str(tmp_path))] == ["visible"]

    def test_each_entry_carries_the_full_path(self, tmp_path):
        (tmp_path / "sub").mkdir()

        assert pc.list_local_children(str(tmp_path)) == [{"path": str(tmp_path / "sub"), "name": "sub"}]

    @pytest.mark.parametrize("kind", ["missing", "file"])
    def test_a_path_that_is_not_a_folder_says_so(self, tmp_path, kind):
        target = tmp_path / "nada"
        if kind == "file":
            target.write_text("x", encoding="utf-8")

        with pytest.raises(FileNotFoundError, match="No existe o no es un directorio"):
            pc.list_local_children(str(target))

    def test_a_folder_without_permission_gives_what_it_could_read_instead_of_failing(self, tmp_path, monkeypatch):
        def deny(self):
            raise PermissionError("denegado")

        monkeypatch.setattr(pc.Path, "iterdir", deny)

        assert pc.list_local_children(str(tmp_path)) == []


class TestCreateLocalFolder:
    def test_it_creates_the_folder_inside_the_parent(self, tmp_path):
        result = pc.create_local_folder(str(tmp_path), "Nueva")

        assert (tmp_path / "Nueva").is_dir()
        assert result == {"path": str(tmp_path / "Nueva"), "name": "Nueva"}

    def test_surrounding_spaces_are_trimmed(self, tmp_path):
        assert pc.create_local_folder(str(tmp_path), "  Con espacios  ")["name"] == "Con espacios"

    @pytest.mark.parametrize("name", ["", "   "])
    def test_an_empty_name_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError, match="no puede estar vacio"):
            pc.create_local_folder(str(tmp_path), name)

    @pytest.mark.parametrize("name", ["a<b", "a>b", "a:b", 'a"b', "a/b", "a\\b", "a|b", "a?b", "a*b"])
    def test_each_character_invalid_in_windows_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError, match="no puede contener"):
            pc.create_local_folder(str(tmp_path), name)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("name", [".", ".."])
    def test_dot_names_are_refused(self, tmp_path, name):
        with pytest.raises(ValueError, match="Nombre de carpeta invalido"):
            pc.create_local_folder(str(tmp_path), name)

    def test_a_path_traversal_cannot_escape_the_parent(self, tmp_path):
        """'../x' lleva una barra: se rechaza antes de construir la ruta, asi que nunca se crea nada fuera de la carpeta que el usuario esta viendo."""
        parent = tmp_path / "padre"
        parent.mkdir()

        with pytest.raises(ValueError):
            pc.create_local_folder(str(parent), "../afuera")
        assert not (tmp_path / "afuera").exists()

    def test_a_missing_parent_is_reported(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="La carpeta padre no existe"):
            pc.create_local_folder(str(tmp_path / "no-existe"), "x")

    def test_the_parent_must_be_a_folder(self, tmp_path):
        f = tmp_path / "archivo"
        f.write_text("x", encoding="utf-8")

        with pytest.raises(FileNotFoundError, match="La carpeta padre no existe"):
            pc.create_local_folder(str(f), "x")

    def test_an_existing_name_is_never_overwritten(self, tmp_path):
        (tmp_path / "Ya").mkdir()
        (tmp_path / "Ya" / "dato.txt").write_text("importante", encoding="utf-8")

        with pytest.raises(FileExistsError, match="Ya existe una carpeta o archivo llamado 'Ya'"):
            pc.create_local_folder(str(tmp_path), "Ya")
        assert (tmp_path / "Ya" / "dato.txt").read_text(encoding="utf-8") == "importante"

    def test_an_existing_file_with_that_name_also_blocks(self, tmp_path):
        (tmp_path / "doc").write_text("x", encoding="utf-8")

        with pytest.raises(FileExistsError):
            pc.create_local_folder(str(tmp_path), "doc")

    def test_it_is_not_recursive(self, tmp_path):
        """Crea UNA carpeta; no inventa niveles intermedios."""
        with pytest.raises(ValueError):
            pc.create_local_folder(str(tmp_path), "a/b")
        assert list(tmp_path.iterdir()) == []
