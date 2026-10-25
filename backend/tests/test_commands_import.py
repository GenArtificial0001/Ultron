"""spec quick-panel-labels-comandos REQ-005: importar comandos propios desde una
carpeta local (`commands.import_commands_from_folder` + `POST
/api/commands/import-path`).

Complementa a `test_commands.py` (camino feliz, no recursivo, conflicto basico):
aca van los bordes que la spec promete y el contrato observable del endpoint
-- nombres del sistema en cualquier mayuscula, sobrescritura solo de lo
confirmado, archivos ilegibles, dueño de la sesion y que no se escriba nada
fuera de `commands/`. Todo contra `tmp_path` (nunca una carpeta real)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import commands, main
from app.persistence import master_index


def _write_md(folder: Path, files: dict[str, str | bytes]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = folder / name
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return folder


def _snapshot(root: Path) -> dict[str, bytes]:
    """Todos los archivos bajo `root` con su contenido: sirve para probar que
    el import no toca nada fuera de `commands/` de la sesion."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# Reglas de negocio (funcion pura)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reserved", sorted(commands.RESERVED_NAMES))
def test_every_system_command_name_is_rejected_in_any_case(tmp_path, reserved):
    # REQ-005: "nombres reservados se rechazan con aviso claro". El nombre del
    # archivo puede venir en mayuscula (EXPORT.md): igual pisaria /export.
    project = tmp_path / "proyecto"
    project.mkdir()
    src = _write_md(
        tmp_path / "origen", {f"{reserved.upper()}.md": "pisaria el sistema"}
    )

    result = commands.import_commands_from_folder(str(project), str(src))

    assert result["reserved"] == [reserved]
    assert result["imported"] == []
    assert not (project / "commands").exists() or not any(
        (project / "commands").iterdir()
    )


def test_overwrite_only_touches_the_names_the_user_confirmed(tmp_path):
    # REQ-005: el 2do paso sobrescribe SOLO lo confirmado; lo demas sigue en conflicto.
    project = tmp_path / "proyecto"
    project.mkdir()
    commands.save_custom_command(str(project), "alfa", "alfa original")
    commands.save_custom_command(str(project), "beta", "beta original")
    src = _write_md(
        tmp_path / "origen", {"alfa.md": "alfa nuevo", "beta.md": "beta nuevo"}
    )

    result = commands.import_commands_from_folder(
        str(project), str(src), overwrite=["alfa"]
    )

    assert result["imported"] == ["alfa"]
    assert result["conflicts"] == ["beta"]
    assert (project / "commands" / "alfa.md").read_text(
        encoding="utf-8"
    ) == "alfa nuevo"
    assert (project / "commands" / "beta.md").read_text(
        encoding="utf-8"
    ) == "beta original"


def test_unreadable_file_goes_to_invalid_and_the_rest_is_imported(tmp_path):
    # Un .md que no es UTF-8 (ej. guardado en ANSI) no rompe el import entero.
    project = tmp_path / "proyecto"
    project.mkdir()
    src = _write_md(
        tmp_path / "origen",
        {"ansi.md": b"\xff\xfe\xfa texto en otra codificacion \xe9", "ok.md": "hola"},
    )

    result = commands.import_commands_from_folder(str(project), str(src))

    assert result["imported"] == ["ok"]
    assert result["invalid"] == ["ansi.md"]


def test_empty_source_folder_imports_nothing_without_error(tmp_path):
    project = tmp_path / "proyecto"
    project.mkdir()
    src = _write_md(tmp_path / "origen", {"leeme.txt": "no es un comando"})

    result = commands.import_commands_from_folder(str(project), str(src))

    assert result == {
        "imported": [],
        "conflicts": [],
        "reserved": [],
        "invalid": [],
        "custom": [],
    }


def test_conflict_is_detected_even_if_the_existing_file_has_uppercase(tmp_path):
    # QA-029 CERRADO: list_custom_commands normaliza a minuscula al listar
    # y el import compara contra esa lista normalizada.
    # REQ-005: "si un nombre pisa un comando existente se pide confirmacion antes
    # de sobrescribir". Un comando creado a mano fuera de Vision (el docstring de
    # commands.py lo invita: "mas facil de editar a mano") puede tener mayusculas.
    project = tmp_path / "proyecto"
    (project / "commands").mkdir(parents=True)
    (project / "commands" / "Resumen.md").write_text("original", encoding="utf-8")
    src = _write_md(tmp_path / "origen", {"resumen.md": "nuevo"})

    result = commands.import_commands_from_folder(str(project), str(src))

    assert result["conflicts"] == ["resumen"]
    assert result["imported"] == []
    stored = {
        p.name: p.read_text(encoding="utf-8")
        for p in (project / "commands").glob("*.md")
    }
    assert "original" in stored.values() and "nuevo" not in stored.values()


def test_utf8_bom_is_not_part_of_the_prompt(tmp_path):
    # QA-030 CERRADO: import_commands_from_folder lee con "utf-8-sig".
    project = tmp_path / "proyecto"
    project.mkdir()
    src = _write_md(tmp_path / "origen", {"saludo.md": "﻿decí hola".encode()})

    commands.import_commands_from_folder(str(project), str(src))

    assert (project / "commands" / "saludo.md").read_text(
        encoding="utf-8"
    ) == "decí hola"


@pytest.mark.parametrize("name", ["..\\..\\fuera", "../../fuera", "sub/dentro"])
def test_custom_command_names_cannot_escape_the_commands_folder(tmp_path, name):
    # Seguridad (spec quick-panel-labels-comandos §7 y comandos-slash): los
    # comandos propios se escriben SOLO dentro de commands/ de la sesion. El
    # import ya lo cumple (el nombre sale del stem de un archivo); el alta/baja
    # manual (POST/DELETE /api/commands/custom) recibe el nombre del cliente.
    project = tmp_path / "a" / "b" / "proyecto"
    project.mkdir(parents=True)
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError):
        commands.save_custom_command(str(project), name, "prompt")

    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["..\\..\\victima", "../../victima", "sub/dentro"])
def test_delete_custom_command_cannot_escape_the_commands_folder(tmp_path, name):
    # QA-032 CERRADO, mitad del hallazgo que save_custom_command no cubria:
    # delete_custom_command tambien pasaba el nombre sin validar a
    # _command_file, asi que un nombre con '..\\' borraba un archivo dos
    # niveles arriba del proyecto en vez de ser un no-op sobre `commands/`.
    project = tmp_path / "a" / "b" / "proyecto"
    project.mkdir(parents=True)
    victim = tmp_path / "a" / "victima.md"
    victim.write_text("no me borres", encoding="utf-8")
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError):
        commands.delete_custom_command(str(project), name)

    assert _snapshot(tmp_path) == before


def test_validate_command_name_blocks_device_reserved_and_long_names():
    # REQ-006: casos que no cubren los parametrize de arriba -- nombre de
    # dispositivo reservado de Windows, termina en punto, y largo. El
    # espacio final NO se prueba: `validate_command_name` hace `.strip()`
    # antes de normalizar (mismo criterio que save_custom_command ya tenia
    # con "  Zeta  " -> "zeta").
    with pytest.raises(ValueError):
        commands.validate_command_name("con")
    with pytest.raises(ValueError):
        commands.validate_command_name("COM1")
    with pytest.raises(ValueError):
        commands.validate_command_name("nombre.")
    with pytest.raises(ValueError):
        commands.validate_command_name("a" * 65)
    # Nombres validos existentes (con tildes/espacios/guiones) no se rompen.
    assert (
        commands.validate_command_name("  Resumen Ejecutivo-2026  ")
        == "resumen ejecutivo-2026"
    )


def test_import_rejects_files_larger_than_the_size_cap(tmp_path):
    # REQ-005 f: un .md de mas de 256 KB va a `invalid` ("demasiado grande").
    project = tmp_path / "proyecto"
    project.mkdir()
    src = tmp_path / "origen"
    src.mkdir()
    (src / "gigante.md").write_text("x" * (256 * 1024 + 1), encoding="utf-8")

    result = commands.import_commands_from_folder(str(project), str(src))

    assert result["invalid"] == ["gigante.md"]
    assert result["imported"] == []


def test_import_never_lets_two_files_in_the_same_batch_overwrite_each_other(tmp_path):
    # REQ-005 e: dos archivos REALMENTE distintos en disco que normalizan al
    # mismo nombre de comando (un espacio final es parte del nombre de
    # archivo para NTFS, pero `validate_command_name` lo recorta) -- el
    # segundo va a conflicts, nunca pisa al primero silenciosamente.
    # ("Resumen.md" vs "resumen.md" NO sirve para este caso: en NTFS son el
    # MISMO archivo, case-insensitive -- ese escenario ya lo cubre QA-029
    # con un archivo preexistente en `commands/`, no dos en el mismo lote).
    project = tmp_path / "proyecto"
    project.mkdir()
    src = _write_md(
        tmp_path / "origen", {"resumen.md": "primero", "resumen .md": "segundo"}
    )

    result = commands.import_commands_from_folder(str(project), str(src))

    # El orden de proceso es alfabetico por nombre de ARCHIVO ("resumen .md"
    # antes que "resumen.md", el espacio ordena antes que el punto), asi
    # que el que se importa es el primero en ese orden -- lo que importa
    # para REQ-005 e es que solo UNO se importa y el otro queda en
    # conflicts, nunca los dos pisandose en silencio.
    assert result["imported"] == ["resumen"]
    assert result["conflicts"] == ["resumen"]
    stored = (project / "commands" / "resumen.md").read_text(encoding="utf-8")
    assert stored in ("primero", "segundo")


# ---------------------------------------------------------------------------
# Endpoint POST /api/commands/import-path
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(main.app)


def _own_session(session_id: str, project: Path) -> None:
    """Registra la sesion en el indice maestro (temp) a nombre del usuario actual."""
    from app.persistence import service as persistence

    master_index.set_project_path(
        session_id,
        str(project),
        windows_user_created=f"{persistence.current_windows_user()}_20260928120000",
    )


def test_endpoint_rejects_a_session_of_another_windows_user(
    tmp_path, tmp_master_db, client
):
    # Seguridad §7: "El endpoint valida session_id/ownership". Carpeta compartida
    # (OneDrive): conocer el id de una sesion ajena no alcanza para escribirle.
    project = tmp_path / "proyecto-ajeno"
    project.mkdir()
    master_index.set_project_path(
        "sesion-ajena", str(project), windows_user_created="otra.persona_20260101000000"
    )
    src = _write_md(tmp_path / "origen", {"hola.md": "decí hola"})

    r = client.post(
        "/api/commands/import-path",
        json={"session_id": "sesion-ajena", "source_path": str(src)},
    )

    assert r.status_code == 403
    assert not (project / "commands").exists()


def test_endpoint_rejects_a_file_as_source_folder(tmp_path, tmp_master_db, client):
    project = tmp_path / "proyecto"
    project.mkdir()
    _own_session("s-archivo", project)
    a_file = tmp_path / "suelto.md"
    a_file.write_text("no es carpeta", encoding="utf-8")

    r = client.post(
        "/api/commands/import-path",
        json={"session_id": "s-archivo", "source_path": str(a_file)},
    )

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "carpeta" in detail.lower()
    assert "Errno" not in detail and "Traceback" not in detail  # mensaje para personas


def test_endpoint_writes_only_inside_the_session_commands_folder(
    tmp_path, tmp_master_db, client
):
    # Seguridad §7: "escribe solo dentro de commands/ de esa sesion". El origen
    # queda intacto (se COPIA, no se mueve) y no aparece nada fuera de commands/.
    project = tmp_path / "proyecto"
    project.mkdir()
    _own_session("s-limpio", project)
    src = _write_md(
        tmp_path / "origen", {"uno.md": "prompt uno", "dos.md": "prompt dos"}
    )
    before = _snapshot(tmp_path)

    r = client.post(
        "/api/commands/import-path",
        json={"session_id": "s-limpio", "source_path": str(src)},
    )

    assert r.status_code == 200, r.text
    after = _snapshot(tmp_path)
    new_files = sorted(set(after) - set(before))
    assert new_files == [
        str(Path("proyecto") / "commands" / "dos.md"),
        str(Path("proyecto") / "commands" / "uno.md"),
    ]
    assert {k: v for k, v in after.items() if k in before} == before


def test_imported_commands_show_up_in_the_commands_list(
    tmp_path, tmp_master_db, client
):
    # REQ-003 + REQ-005: despues de importar, la misma fuente del menu "/" y del
    # panel (GET /api/commands) los lista como "propios".
    project = tmp_path / "proyecto"
    project.mkdir()
    _own_session("s-lista", project)
    src = _write_md(tmp_path / "origen", {"Resumen.md": "resumí esto"})

    r = client.post(
        "/api/commands/import-path",
        json={"session_id": "s-lista", "source_path": str(src)},
    )
    assert r.status_code == 200, r.text

    listed = client.get("/api/commands", params={"session_id": "s-lista"}).json()
    assert {"name": "resumen", "prompt": "resumí esto"} in listed["custom"]
    # spec comando-update-sesiones-publicas REQ-010: `/update` solo se OFRECE si la carpeta tiene publicas ajenas (aca no), pero sigue RESERVADO:
    # nadie puede crear un comando propio llamado `update` que lo tape.
    assert {c["name"] for c in listed["system"]} == commands.RESERVED_NAMES - {"update"}
    assert "update" in commands.RESERVED_NAMES


def test_endpoint_second_step_overwrites_after_confirmation(
    tmp_path, tmp_master_db, client
):
    # REQ-005: flujo en 2 pasos por HTTP -- 1ro devuelve el conflicto sin tocar
    # nada; 2do, con `overwrite`, reemplaza.
    project = tmp_path / "proyecto"
    project.mkdir()
    _own_session("s-2pasos", project)
    commands.save_custom_command(str(project), "resumen", "original")
    src = _write_md(tmp_path / "origen", {"resumen.md": "nuevo"})
    body = {"session_id": "s-2pasos", "source_path": str(src)}

    first = client.post("/api/commands/import-path", json=body).json()
    assert first["conflicts"] == ["resumen"]
    assert (project / "commands" / "resumen.md").read_text(
        encoding="utf-8"
    ) == "original"

    second = client.post(
        "/api/commands/import-path", json={**body, "overwrite": ["resumen"]}
    ).json()
    assert second["imported"] == ["resumen"] and second["conflicts"] == []
    assert (project / "commands" / "resumen.md").read_text(encoding="utf-8") == "nuevo"


# ---------------------------------------------------------------------------
# REQ-006 (QA-032): POST/DELETE /api/commands/custom -- dueño de la sesion +
# nombre invalido por HTTP (el {name} del DELETE llega ya decodificado, ej.
# "%5C" -> "\\").
# ---------------------------------------------------------------------------
def test_post_custom_command_rejects_a_session_of_another_windows_user(
    tmp_path, tmp_master_db, client
):
    project = tmp_path / "proyecto-ajeno"
    project.mkdir()
    master_index.set_project_path(
        "sesion-ajena", str(project), windows_user_created="otra.persona_20260101000000"
    )

    r = client.post(
        "/api/commands/custom",
        json={"session_id": "sesion-ajena", "name": "hola", "prompt": "texto"},
    )

    assert r.status_code == 403
    assert not (project / "commands").exists()


def test_delete_custom_command_rejects_a_session_of_another_windows_user(
    tmp_path, tmp_master_db, client
):
    project = tmp_path / "proyecto-ajeno"
    project.mkdir()
    master_index.set_project_path(
        "sesion-ajena", str(project), windows_user_created="otra.persona_20260101000000"
    )

    r = client.delete(
        "/api/commands/custom/hola", params={"session_id": "sesion-ajena"}
    )

    assert r.status_code == 403


def test_delete_custom_command_endpoint_rejects_path_traversal_name(
    tmp_path, tmp_master_db, client
):
    # QA-032: por HTTP el segmento de ruta llega decodificado ("%2E%2E%5C..."
    # -> "..\\"); el endpoint tiene que rechazarlo con 400, nunca borrar
    # fuera de commands/ de la sesion.
    project = tmp_path / "a" / "b" / "proyecto"
    project.mkdir(parents=True)
    _own_session("s-traversal", project)
    victim = tmp_path / "a" / "victima.md"
    victim.write_text("no me borres", encoding="utf-8")

    r = client.delete(
        "/api/commands/custom/..%5C..%5Cvictima", params={"session_id": "s-traversal"}
    )

    assert r.status_code == 400
    assert victim.exists() and victim.read_text(encoding="utf-8") == "no me borres"
