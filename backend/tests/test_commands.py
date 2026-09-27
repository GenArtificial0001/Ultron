"""Unit de `app/commands.py`: CRUD de comandos propios (un .md por comando)
y reglas de negocio de nombres reservados (spec comandos-slash REQ-006/008/009)."""

from __future__ import annotations

import pytest

from app import commands


def test_system_commands_have_unique_names_and_kind():
    names = [c["name"] for c in commands.SYSTEM_COMMANDS]
    assert len(names) == len(set(names))
    assert all("kind" in c and c["description"] for c in commands.SYSTEM_COMMANDS)
    assert commands.RESERVED_NAMES == set(names)
    assert "qa-audit" not in names  # tooling dev, fuera del menu de usuario


def test_list_without_project_or_folder_returns_empty(tmp_path):
    assert commands.list_custom_commands(None) == []
    assert commands.list_custom_commands("") == []
    assert commands.list_custom_commands(str(tmp_path)) == []  # sin subcarpeta commands/


def test_save_creates_file_and_lists_sorted(tmp_path):
    commands.save_custom_command(str(tmp_path), "  Zeta  ", "  prompt z  ")
    result = commands.save_custom_command(str(tmp_path), "alfa", "prompt a")
    assert [c["name"] for c in result] == ["alfa", "zeta"]  # normaliza a minuscula y ordena
    assert (tmp_path / "commands" / "zeta.md").read_text(encoding="utf-8") == "prompt z"


def test_save_same_name_overwrites(tmp_path):
    commands.save_custom_command(str(tmp_path), "x", "uno")
    result = commands.save_custom_command(str(tmp_path), "x", "dos")
    assert result == [{"name": "x", "prompt": "dos"}]


@pytest.mark.parametrize(
    ("name", "prompt", "fragment"),
    [
        ("", "p", "nombre"),
        ("   ", "p", "nombre"),
        ("init", "p", "comando del sistema"),
        ("EXPORT", "p", "comando del sistema"),
        ("ok", "   ", "prompt"),
    ],
)
def test_save_rejects_invalid_input(tmp_path, name, prompt, fragment):
    with pytest.raises(ValueError, match=fragment):
        commands.save_custom_command(str(tmp_path), name, prompt)
    assert not (tmp_path / "commands").exists() or not list((tmp_path / "commands").glob("*.md"))


def test_delete_is_idempotent(tmp_path):
    commands.save_custom_command(str(tmp_path), "borrame", "p")
    assert commands.delete_custom_command(str(tmp_path), " Borrame ") == []
    assert commands.delete_custom_command(str(tmp_path), "borrame") == []  # 2da vez: no-op
    assert commands.delete_custom_command("", "x") == []


def test_list_skips_unreadable_files(tmp_path, monkeypatch):
    commands.save_custom_command(str(tmp_path), "ok", "p")
    (tmp_path / "commands" / "roto.md").write_bytes(b"x")
    real_read = type(tmp_path).read_text

    def flaky(self, *a, **k):
        if self.name == "roto.md":
            raise OSError("bloqueado")
        return real_read(self, *a, **k)

    monkeypatch.setattr(type(tmp_path), "read_text", flaky)
    assert [c["name"] for c in commands.list_custom_commands(str(tmp_path))] == ["ok"]


def test_list_returns_empty_if_glob_fails(tmp_path, monkeypatch):
    (tmp_path / "commands").mkdir()

    def boom(self, pattern):
        raise OSError("disco caido")

    monkeypatch.setattr(type(tmp_path), "glob", boom)
    assert commands.list_custom_commands(str(tmp_path)) == []


def test_init_prompt_is_nonempty():
    assert "AGENTS.md" in commands.INIT_PROMPT
