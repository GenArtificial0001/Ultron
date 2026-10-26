"""
PDF escaneado -> transcripcion por vision (spec adjuntar-archivos-chat REQ-006).

Cuando `read` encuentra paginas sin capa de texto (menos de 20 caracteres
no-espacio), las renderiza a imagen con `pypdfium2` y le pide al MODELO ACTIVO
DEL TURNO que las transcriba. Sin OCR (decision explicita del usuario): la
vision del propio modelo hace el trabajo.

Reglas (spec):
- El render deja el lado mayor en 1568 px y JPEG de calidad 0,82: mismos
  parametros que las imagenes del chat (contexto-multimodal).
- Hasta 10 paginas por llamada; pedir mas devuelve la estimacion de tokens y
  pide acotar `pages` (REQ-006 e).
- Si el modelo activo NO soporta imagenes: mensaje de bloqueo (mismo patron que
  contexto-multimodal, nombrando modelos que si soportan). CERO texto inventado.
- La transcripcion lleva la nota "(transcripcion por vision: puede tener errores)".

Todas las dependencias externas (render, cliente del modelo, catalogo) entran
por parametro para que los tests no necesiten pypdfium2 real ni red.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_SIDE_PX = 1568
JPEG_QUALITY = 82  # 0,82 en la escala de PIL
MAX_SCANNED_PAGES = 10
# Tokens aproximados por pagina de imagen (estimacion conservadora, REQ-006 e).
TOKENS_PER_IMAGE_PAGE = 1600

TRANSCRIBE_PROMPT = (
    "Transcribí tal cual el texto de esta página escaneada. Las tablas van en markdown. "
    "No inventes ni completes nada que no se lea: si algo es ilegible, escribí [ilegible]. "
    "Devolvé solo la transcripción."
)
NOTICE = "(transcripción por visión: puede tener errores)"

RenderFn = Callable[[Path, int], bytes]  # (pdf, pagina desde 1) -> JPEG


def blocked_message(vision_models: list[str]) -> str:
    """REQ-006 d: mismo patron de bloqueo que contexto-multimodal."""
    names = ", ".join(vision_models[:4]) if vision_models else "gpt-5-mini, gemini o claude-sonnet"
    return (
        "El modelo activo no soporta imágenes, así que no puedo leer las páginas escaneadas de este PDF. "
        f"Cambiá a un modelo con visión ({names}) y pedime de nuevo esas páginas."
    )


def too_many_message(pages: list[int]) -> str:
    """REQ-006 e: pedir mas de 10 paginas escaneadas devuelve la estimacion y pide acotar."""
    tokens = len(pages) * TOKENS_PER_IMAGE_PAGE
    return (
        f"Hay {len(pages)} páginas escaneadas (≈ {tokens // 1000} k tokens si las leo todas). "
        f"Leo hasta {MAX_SCANNED_PAGES} por vez: pedime un rango con `pages` (ej. \"1-{MAX_SCANNED_PAGES}\")."
    )


def image_to_jpeg(image: Any) -> bytes:
    """Una imagen PIL -> JPEG de calidad 0,82 con el lado mayor en 1568 px (mismos parametros que las paginas y que las
    imagenes del chat: contexto-multimodal). Compartido con la lectura de imagenes de PDF/Word (document_images)."""
    image = image.convert("RGB")
    longest = max(image.size)
    if longest > MAX_SIDE_PX:  # por si el redondeo se pasa un pixel
        ratio = MAX_SIDE_PX / longest
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def render_page_jpeg(pdf_path: Path, page_number: int) -> bytes:
    """Renderiza UNA pagina (desde 1) con pypdfium2: lado mayor 1568 px, JPEG 0,82."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_number - 1]
        width, height = page.get_size()  # en puntos (1/72")
        scale = min(MAX_SIDE_PX / max(width, height), 4.0)
        image = page.render(scale=scale).to_pil()
    finally:
        pdf.close()
    return image_to_jpeg(image)


def vision_models_from_catalog(catalog: dict[str, dict[str, Any]]) -> list[str]:
    """Ids de los modelos del roster que soportan imagenes (para el mensaje de bloqueo)."""
    return sorted(mid for mid, m in catalog.items() if m.get("supports_image_input"))


def supports_images(model: str | None, catalog: dict[str, dict[str, Any]]) -> bool:
    """El modelo activo soporta imagenes segun el catalogo (por id o alias)."""
    if not model:
        return False
    entry = catalog.get(model)
    if entry is None:
        for m in catalog.values():
            if model in (m.get("aliases") or []):
                entry = m
                break
    return bool(entry and entry.get("supports_image_input"))


def make_transcriber(
    *,
    model: str | None,
    catalog: dict[str, dict[str, Any]],
    complete: Callable[[str, list[dict[str, Any]]], str],
    render: RenderFn = render_page_jpeg,
    on_progress: Callable[[int], None] | None = None,
) -> Callable[[Path, list[int]], tuple[dict[int, str], str | None]]:
    """Arma la funcion que `read_pdf` llama con las paginas escaneadas.

    `complete(model, messages)` hace la llamada real al modelo (inyectada). Devuelve
    ({pagina: texto}, aviso): el aviso es el bloqueo/limite o la nota de "puede tener
    errores". Nunca lanza: una pagina que falla queda sin texto y read la informa."""

    def transcribe(pdf_path: Path, pages: list[int]) -> tuple[dict[int, str], str | None]:
        if not supports_images(model, catalog):
            return {}, blocked_message(vision_models_from_catalog(catalog))
        if len(pages) > MAX_SCANNED_PAGES:
            return {}, too_many_message(pages)
        out: dict[int, str] = {}
        failed: list[int] = []
        for i, n in enumerate(pages, start=1):
            try:
                jpeg = render(pdf_path, n)
                data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
                text = complete(
                    model or "",
                    [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": TRANSCRIBE_PROMPT},
                                {"type": "image_url", "image_url": {"url": data_uri}},
                            ],
                        }
                    ],
                )
                if text and text.strip():
                    out[n] = text.strip()
                else:
                    failed.append(n)
            except Exception:  # noqa: BLE001 -- una pagina rota no debe tirar el PDF entero
                logger.warning("No se pudo transcribir la página %s de %s", n, pdf_path.name, exc_info=True)
                failed.append(n)
            if on_progress:
                on_progress(int(i * 100 / len(pages)))
        notes: list[str] = []
        if out:
            notes.append(NOTICE)
        if failed:
            notes.append(
                "No pude transcribir las páginas " + ", ".join(str(n) for n in failed) + "."
            )
        return out, " ".join(notes) or None

    return transcribe


def complete_with_proveedor(model: str, messages: list[dict[str, Any]]) -> str:
    """Llamada real (sincrona, sin streaming) al modelo activo via el proveedor de IA. Corre
    en el hilo de la tool `read`. Devuelve el texto de la respuesta."""
    from app.llm.client import call_model_with_token_retry  # import diferido: evita ciclo

    # transcripcion-reintenta-token REQ-006: un 401 pide token nuevo y reintenta una vez.
    # Con el cliente del proveedor de `model` (bug real 2026-10-26, ver `call_model_with_token_retry`).
    resp = call_model_with_token_retry(
        model, lambda client, name: client.chat.completions.create(model=name, messages=messages, stream=False)
    )
    return (resp.choices[0].message.content or "") if resp.choices else ""
