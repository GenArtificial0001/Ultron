"""spec controles-seguridad-optimizacion-lightspeed-bigquery REQ-004:
endpoint `GET /api/sessions/{id}/exports/{filename}` que sirve los CSV
exportados. Cubre el patron BOLA/IDOR + path traversal (ver skill
secure-code-review): solo debe servir archivos DENTRO de exports/ de
la sesion pedida, nunca una ruta arbitraria del disco."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.persistence import master_index as mi
from app.persistence import service as persistence


def _client_with_project(tmp_path):
    project = tmp_path / "proyecto"
    exports = project / persistence.EXPORTS_SUBDIR
    exports.mkdir(parents=True)
    (exports / "ok.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (project / "secret.txt").write_text("no deberia ser accesible", encoding="utf-8")

    patch_root = patch.object(
        persistence, "project_root_for_session", lambda sid: project
    )
    patch_owner = patch.object(mi, "get_owner", lambda sid: None)
    return TestClient(app), patch_root, patch_owner, project


def test_download_real_export_returns_200(tmp_path):
    client, patch_root, patch_owner, _ = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/ok.csv")
    assert r.status_code == 200
    assert r.text.replace("\r\n", "\n").strip() == "a,b\n1,2"


def test_download_uses_inline_content_disposition(tmp_path):
    """Bug real reportado por el usuario (2026-09-21): 'el enlace no
    deberia descargarmelo de nuevo, deberia abrirme el archivo sin
    descargar'. `FileResponse` de Starlette usa 'attachment' por defecto
    (fuerza descarga) -- el endpoint debe pedir explicitamente 'inline'
    para que el navegador intente abrirlo en vez de descargarlo."""
    client, patch_root, patch_owner, _ = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/ok.csv")
    assert r.status_code == 200
    content_disposition = r.headers.get("content-disposition", "")
    assert "inline" in content_disposition
    assert "attachment" not in content_disposition


def test_download_missing_file_returns_404(tmp_path):
    client, patch_root, patch_owner, _ = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/no-existe.csv")
    assert r.status_code == 404


def test_download_session_without_project_folder_returns_404():
    client = TestClient(app)
    with (
        patch.object(persistence, "project_root_for_session", lambda sid: None),
        patch.object(mi, "get_owner", lambda sid: None),
    ):
        r = client.get("/api/sessions/s-sin-carpeta/exports/algo.csv")
    assert r.status_code == 404


def test_download_rejects_path_traversal_encoded(tmp_path):
    client, patch_root, patch_owner, _ = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/..%2Fsecret.txt")
    assert r.status_code == 404


def test_download_rejects_path_traversal_variant(tmp_path):
    client, patch_root, patch_owner, _ = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/....//secret.txt")
    assert r.status_code == 404


def test_download_never_serves_sibling_file_outside_exports(tmp_path):
    """El nombre de archivo se resuelve SIEMPRE dentro de exports/ (via
    Path(filename).name) -- ni siquiera un nombre que matchee un archivo
    real fuera de esa carpeta deberia poder servirse."""
    client, patch_root, patch_owner, project = _client_with_project(tmp_path)
    with patch_root, patch_owner:
        r = client.get("/api/sessions/s1/exports/secret.txt")
    assert r.status_code == 404
