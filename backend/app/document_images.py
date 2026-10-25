"""
Leer las IMAGENES que hay dentro de un PDF o un Word (spec leer-imagenes-pdf-word).

`read` solo transcribia con vision las paginas SIN texto (pdf_vision.py) y `read_docx` ignoraba las imagenes. Este
modulo agrega, sin dependencias nuevas ni OCR, la lectura de imagenes con el modelo ACTIVO del turno, bajo estas reglas:

- Es opt-in: solo con `images=true`. Sin ese parametro, `read` devuelve lo de siempre MAS un aviso corto al final si el
  documento tiene imagenes utiles (cuantas y cuantos tokens costaria leerlas).
- Utiles = lado menor >= `image_min_px` (64) y no repetidas. Un PDF de pruebas real traia 213 imagenes y solo 3 eran utiles
  (205 eran casilleros de 2x2 px): contarlas todas habria disparado una pregunta sin sentido.
- Si leerlas es caro (mas de `image_ask_count` imagenes O mas de `image_ask_pct` % de la ventana de contexto), el aviso pasa a
  ser una INSTRUCCION para el modelo: preguntarle al usuario con la tool `question`. `read` no puede preguntar por si misma.
  Es una instruccion, no un bloqueo: lo que acota el gasto de un modelo que la ignore es el tope de 10 imagenes por llamada.
- Ninguna opcion de esa pregunta se marca como recomendada: un `/loop` autonomo auto-elige la "(Recomendada)" y gastar tokens
  lo tiene que decidir una persona. Sin recomendada, la tool `question` pausa y suena la alarma.
- Sin visión en el modelo activo: se avisa y no se inventa texto.

Todo lo externo (modelo, catalogo, umbrales) entra por `DocImagesContext`: los tests no necesitan red ni login (ADR-015).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import posixpath
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import pdf_vision

logger = logging.getLogger(__name__)

# Tokens aproximados por imagen: el mismo valor conservador que ya usa la lectura de paginas escaneadas.
TOKENS_PER_IMAGE = pdf_vision.TOKENS_PER_IMAGE_PAGE
MAX_IMAGES_PER_CALL = 10  # igual que MAX_SCANNED_PAGES
MAX_KEPT_IMAGES = 100  # tope de imagenes utiles que se retienen en memoria por documento
MAX_EXAMINED_IMAGES = 300  # tope de imagenes grandes que se decodifican para filtrar repetidas
MAX_MEDIA_BYTES = 25 * 1024 * 1024  # un medio de Word mas grande que esto se omite (proteccion contra zip bomb)
MAX_ZIP_ENTRIES = 200  # mismo tope que attachment_readers.MAX_ZIP_ENTRIES (no se importa: ciclo)
MAX_MODELS_LISTED = 8

IMAGE_PROMPT = (
    "Esta imagen está dentro de un documento. Si tiene texto, transcribilo tal cual; si es un gráfico, una tabla o un "
    "diagrama, explicá lo que muestra (las tablas en markdown). No inventes ni completes nada que no se vea: si algo es "
    "ilegible, escribí [ilegible]. Devolvé solo el contenido."
)


# ---------------------------------------------------------------------------
# Configuracion y politica de costo
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageThresholds:
    """REQ-002/004/007: filtro y umbral de costo. Los valores por defecto son los de `ProjectConfig`."""

    min_px: int = 64
    ask_count: int = 5
    ask_pct: int = 10


def tokens_for(count: int) -> int:
    return count * TOKENS_PER_IMAGE


def is_costly(count: int, window: int, thresholds: ImageThresholds) -> bool:
    """REQ-004: alcanza con que se cumpla UNO de los dos criterios. Es "mas de", no "al menos": con el umbral en 5, cinco
    imagenes no preguntan y seis si. Sin imagenes nunca es caro (0 no supera ningun umbral, no hace falta un caso aparte). Con una ventana desconocida (<= 0) decide solo la cantidad."""
    if count > thresholds.ask_count:
        return True
    return window > 0 and tokens_for(count) * 100 > thresholds.ask_pct * window


def fmt_int(n: int) -> str:
    """Entero con punto de miles (es-AR): 9600 -> '9.600'."""
    return f"{n:,}".replace(",", ".")


def fmt_pct(pct: float) -> str:
    """Porcentaje con coma decimal; mas decimales cuanto mas chico, para que 0,96 no se lea como 1."""
    decimals = 2 if pct < 1 else 1 if pct < 10 else 0
    return f"{pct:.{decimals}f}".replace(".", ",") + " %"


def n_images(n: int, truncated: bool = False) -> str:
    if truncated:
        return f"más de {n} imágenes útiles"
    return "1 imagen útil" if n == 1 else f"{n} imágenes útiles"


# ---------------------------------------------------------------------------
# Imagenes extraidas
# ---------------------------------------------------------------------------
@dataclass
class DocImage:
    number: int  # 1.. en orden de lectura del documento
    page: int | None  # solo PDF
    open_image: Callable[[], Any]  # devuelve una imagen PIL, recien cuando se la va a leer


@dataclass
class Extracted:
    images: list[DocImage] = field(default_factory=list)
    unreadable: int = 0  # imagenes grandes que no se pudieron decodificar (se omiten, y se avisa)
    truncated: bool = False  # se alcanzo un tope: el conteo es "mas de N"
    placement: list[list[int]] = field(default_factory=list)  # solo Word: numeros de imagen por bloque del cuerpo


def extract_pdf_images(pdf_path: Path, pages: list[int], min_px: int) -> Extracted:
    """Imagenes utiles de las `pages` pedidas (desde 1), con pypdfium2. Dentro de cada pagina van de arriba hacia abajo.
    La deduplicacion es sobre lo que se esta leyendo: una imagen que solo se repite en una pagina NO pedida igual se lee."""
    import pypdfium2 as pdfium

    out = Extracted()
    seen: set[bytes] = set()
    examined = 0
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for n in pages:
            if out.truncated:
                break
            try:
                page = pdf[n - 1]
            except IndexError:
                continue
            found: list[tuple[float, Any]] = []
            for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
                try:
                    width, height = obj.get_px_size()
                except Exception:  # noqa: BLE001 -- un objeto roto no tira el documento
                    out.unreadable += 1
                    continue
                if min(width, height) < min_px:
                    continue
                if examined >= MAX_EXAMINED_IMAGES or len(out.images) + len(found) >= MAX_KEPT_IMAGES:
                    out.truncated = True
                    break
                examined += 1
                try:
                    pil = obj.get_bitmap().to_pil().convert("RGB")
                    top = obj.get_bounds()[3]
                except Exception:  # noqa: BLE001 -- mascaras, formatos raros: se informa, no se inventa
                    logger.warning(
                        "No se pudo decodificar una imagen de la página %s de %s", n, pdf_path.name, exc_info=True
                    )
                    out.unreadable += 1
                    continue
                digest = hashlib.sha256(repr(pil.size).encode() + pil.tobytes()).digest()
                if digest in seen:
                    continue
                seen.add(digest)
                found.append((top, pil))
            found.sort(key=lambda item: -item[0])  # PDF: el eje Y crece hacia arriba
            for _top, pil in found:
                out.images.append(DocImage(len(out.images) + 1, n, lambda pil=pil: pil))
    finally:
        pdf.close()
    return out


def extract_docx_images(path: Path, document: Any, min_px: int) -> Extracted:
    """Imagenes utiles de un Word, en el orden del cuerpo y con el bloque donde aparece cada una (`placement`).

    El ORDEN y las referencias salen del XML que python-docx ya parseo (`a:blip/@r:embed` -> relacion); los BYTES, de
    `word/media/*` via zipfile, con tope de entradas y de tamano por medio. La deduplicacion es por los bytes del medio."""
    from PIL import Image

    children = list(document.element.body.iterchildren())
    out = Extracted(placement=[[] for _ in children])
    seen: set[bytes] = set()
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return out
    with zf:
        infos = {info.filename: info for info in zf.infolist()[:MAX_ZIP_ENTRIES]}
        rels = document.part.rels
        for index, child in enumerate(children):
            for rid in child.xpath(".//a:blip/@r:embed"):
                rel = rels.get(rid)
                if rel is None or rel.is_external or not rel.reltype.endswith("/image"):
                    continue
                name = posixpath.normpath(posixpath.join("word", rel.target_ref))
                info = infos.get(name)
                if info is None or info.file_size > MAX_MEDIA_BYTES:
                    out.unreadable += 1
                    continue
                try:
                    data = zf.read(info)
                    with Image.open(io.BytesIO(data)) as probe:
                        width, height = probe.size
                except Exception:  # noqa: BLE001 -- EMF/WMF y otros formatos sin soporte: se informa
                    out.unreadable += 1
                    continue
                if min(width, height) < min_px:
                    continue
                digest = hashlib.sha256(data).digest()
                if digest in seen:
                    continue
                if len(out.images) >= MAX_KEPT_IMAGES:
                    out.truncated = True
                    return out
                seen.add(digest)
                number = len(out.images) + 1
                out.images.append(DocImage(number, None, lambda data=data: Image.open(io.BytesIO(data))))
                out.placement[index].append(number)
    return out


# ---------------------------------------------------------------------------
# Contexto de la lectura (todo lo externo entra por aca)
# ---------------------------------------------------------------------------
@dataclass
class DocImagesContext:
    want: bool  # images=true
    model: str | None
    catalog: dict[str, dict[str, Any]]
    thresholds: ImageThresholds
    window: int  # ventana de contexto del modelo activo
    confirmed: bool  # el usuario ya confirmo leer las imagenes de ESTE archivo en esta sesion
    complete: Callable[[str, list[dict[str, Any]]], str]
    mark_confirmed: Callable[[], None]
    select: str | None = None  # Word: `pages` como numeros de imagen (decision D2)
    on_progress: Callable[[int], None] | None = None


def truthy(value: Any) -> bool:
    """El modelo a veces manda "true" como texto."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true") or value == 1


def build_context(
    session: Any, path: Path, args: dict, on_progress: Callable[[int], None] | None = None
) -> DocImagesContext:
    """Arma el contexto desde la sesion (modelo activo, imagenes ya confirmadas) y la configuracion. Nunca lanza."""
    from app.project_context import get_project_config

    model = getattr(session, "active_model", None)
    catalog: dict[str, dict[str, Any]] = {}
    window = 200_000
    try:
        from app import main  # import diferido: evita ciclo

        catalog = main._MODELS_CACHE.get("by_id") or {}
        if model:
            window = main.resolve_context_window(model)
    except Exception:  # noqa: BLE001 -- sin catalogo se sigue: bloqueara o avisara con un mensaje claro
        logger.debug("Sin catalogo de modelos para la lectura de imagenes", exc_info=True)

    cfg = get_project_config()
    thresholds = ImageThresholds(cfg.image_min_px, cfg.image_ask_count, cfg.image_ask_pct)

    store = getattr(session, "image_read_ok", None) if session is not None else None
    if session is not None and store is None:
        try:
            session.image_read_ok = store = set()
        except Exception:  # noqa: BLE001 -- una sesion que no admite el atributo: simplemente no se recuerda
            store = None
    key = str(Path(path).resolve())

    def mark() -> None:
        if store is not None:
            store.add(key)

    return DocImagesContext(
        want=truthy(args.get("images")),
        model=model,
        catalog=catalog,
        thresholds=thresholds,
        window=window,
        confirmed=store is not None and key in store,
        # Se busca al armar el contexto (no al importar): asi se puede reemplazar en los tests sin red.
        complete=pdf_vision.complete_with_proveedor,
        mark_confirmed=mark,
        select=args.get("pages"),
        on_progress=on_progress,
    )


# ---------------------------------------------------------------------------
# Decision y lectura
# ---------------------------------------------------------------------------
@dataclass
class Outcome:
    """Que hacer con las imagenes de un documento: los marcadores a insertar y las notas del final."""

    texts: dict[int, str] = field(default_factory=dict)
    failed: list[int] = field(default_factory=list)
    shown: set[int] = field(default_factory=set)
    tail: list[str] = field(default_factory=list)

    def marker(self, image: DocImage) -> str | None:
        """Bloque `[Imagen N, página P: texto]` (o sin pagina, en Word) o None si esa imagen no se leyo en esta llamada."""
        if image.number not in self.shown:
            return None
        where = f", página {image.page}" if image.page is not None else ""
        body = self.texts.get(image.number)
        return (
            f"[Imagen {image.number}{where}: {body}]"
            if body is not None
            else f"[Imagen {image.number}{where}: no se pudo transcribir]"
        )


def vision_options(catalog: dict[str, dict[str, Any]]) -> list[tuple[str, int | None]]:
    """(id, ventana) de los modelos con vision, de MAYOR a menor ventana. El catalogo guarda la misma entrada bajo el id y
    bajo cada alias: se deduplica por id. Solo se usa lo que el catalogo informa (vision y ventana): ni precios ni 'ilimitado'."""
    seen: set[str] = set()
    options: list[tuple[str, int | None]] = []
    for key, entry in catalog.items():
        if not entry.get("supports_image_input"):
            continue
        name = entry.get("id") or key
        if name in seen:
            continue
        seen.add(name)
        window = entry.get("context_window")
        options.append((name, window if isinstance(window, int) and window > 0 else None))
    options.sort(key=lambda o: (-(o[1] or 0), o[0]))
    return options


def _short_notice(count: int, truncated: bool) -> str:
    return (
        f"Este documento tiene {n_images(count, truncated)} (≈ {fmt_int(tokens_for(count))} tokens si se leen). "
        "Para leerlas, volvé a llamar a `read` con `images=true` (solo si el usuario lo pidió o lo confirmó)."
    )


def _no_vision_notice(count: int, truncated: bool, catalog: dict[str, dict[str, Any]]) -> str:
    names = ", ".join(name for name, _ in vision_options(catalog)[:4]) or "gpt-5-mini, gemini o claude-sonnet"
    return (
        f"Este documento tiene {n_images(count, truncated)}, pero el modelo activo no soporta imágenes, así que no puedo leerlas. "
        f"Cambiá a un modelo con visión ({names}) y pedime de nuevo las imágenes."
    )


def _ask_instruction(count: int, truncated: bool, ctx: DocImagesContext) -> str:
    tokens = tokens_for(count)
    pct = tokens / ctx.window * 100 if ctx.window > 0 else 0.0
    options = vision_options(ctx.catalog)
    listing = []
    for name, window in options[:MAX_MODELS_LISTED]:
        if window:
            listing.append(f"{name} (ventana de {fmt_int(window)} tokens; ocuparían {fmt_pct(tokens / window * 100)})")
        else:
            listing.append(f"{name} (ventana no informada)")
    if len(options) > MAX_MODELS_LISTED:
        listing.append(f"y {len(options) - MAX_MODELS_LISTED} más")
    models = "; ".join(listing) if listing else "ninguno informa soporte de imágenes en el catálogo actual"
    return (
        f"[Lectura de imágenes: pedí confirmación] Este documento tiene {n_images(count, truncated)}. Leerlas costaría "
        f"≈ {fmt_int(tokens)} tokens (aproximado), el {fmt_pct(pct)} de la ventana de contexto del modelo activo "
        f"({ctx.model or 'desconocido'}). TODAVÍA NO LAS LEÍ.\n"
        "Antes de seguir, preguntale al usuario con la tool `question`. No marques ninguna opción como recomendada: gastar "
        "tokens lo decide una persona. Opciones:\n"
        '- "Sí, leer las imágenes": volvé a llamar a `read` con la misma ruta e `images=true`.\n'
        '- "No, solo el texto": seguí con el texto que ya leíste.\n'
        '- "Cambiar de modelo": el usuario cambia el modelo desde el selector y después te pide leerlas. '
        f"Modelos con visión, de mayor a menor ventana de contexto: {models}."
    )


def _compact(numbers: list[int]) -> str:
    """[11, 12, 14] -> '11-12,14'."""
    ranges: list[str] = []
    start = prev = numbers[0]
    for n in [*numbers[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ",".join(ranges)


def _select_numbers(total: int, spec: str | None, is_word: bool) -> list[int]:
    """Que imagenes leer. PDF: todas las de las paginas pedidas. Word: `pages` elige NUMEROS DE IMAGEN (decision D2)."""
    if not is_word or spec is None or not str(spec).strip():
        return list(range(1, total + 1))
    from app.attachment_readers import parse_pages  # import diferido: evita ciclo

    return parse_pages(spec, total)


def plan(extracted: Extracted, ctx: DocImagesContext, *, is_word: bool) -> Outcome:
    """Decide que hacer (nada, aviso corto, pedir confirmacion, bloqueo o leer) y, si corresponde, transcribe."""
    out = Outcome()
    images = extracted.images
    count = len(images)
    if extracted.unreadable:
        out.tail.append(
            f"Se omitieron {extracted.unreadable} imágenes que no pude decodificar."
            if extracted.unreadable != 1
            else "Se omitió 1 imagen que no pude decodificar."
        )
    if count == 0:
        return out
    vision = pdf_vision.supports_images(ctx.model, ctx.catalog)

    if not ctx.want:
        if not vision:
            out.tail.append(_no_vision_notice(count, extracted.truncated, ctx.catalog))
        elif is_costly(count, ctx.window, ctx.thresholds) and not ctx.confirmed:
            out.tail.append(_ask_instruction(count, extracted.truncated, ctx))
        else:
            out.tail.append(_short_notice(count, extracted.truncated))
        return out

    if not vision:
        out.tail.append(_no_vision_notice(count, extracted.truncated, ctx.catalog))
        return out

    try:
        numbers = _select_numbers(count, ctx.select, is_word)
    except ValueError as exc:
        out.tail.append(f"Error: {exc}.")
        return out
    if not numbers:
        out.tail.append(f"Ninguna de las imágenes pedidas existe (el documento tiene {count}).")
        return out
    to_read, remaining = numbers[:MAX_IMAGES_PER_CALL], numbers[MAX_IMAGES_PER_CALL:]
    by_number = {image.number: image for image in images}
    for i, number in enumerate(to_read, start=1):
        text = _transcribe(ctx, by_number[number])
        out.shown.add(number)
        if text is None:
            out.failed.append(number)
        else:
            out.texts[number] = text
        if ctx.on_progress:
            ctx.on_progress(int(i * 100 / len(to_read)))
    ctx.mark_confirmed()
    if out.texts:
        out.tail.append(pdf_vision.NOTICE)
    if out.failed:
        names = ", ".join(str(n) for n in out.failed)
        out.tail.append(
            f"No pude transcribir la imagen {names}."
            if len(out.failed) == 1
            else f"No pude transcribir las imágenes {names}."
        )
    if remaining:
        how = (
            f'pedí esas con `pages` (números de imagen, ej. "{_compact(remaining[:MAX_IMAGES_PER_CALL])}") e `images=true`'
            if is_word
            else "pedí un rango más chico de páginas con `pages` e `images=true`"
        )
        out.tail.append(f"Leí {len(to_read)} imágenes por vez; hay {len(remaining)} imágenes más sin leer: {how}.")
    return out


def _transcribe(ctx: DocImagesContext, image: DocImage) -> str | None:
    """Una imagen -> texto, o None si fallo. Nunca lanza: una imagen rota no tira el documento ni se inventa su contenido."""
    try:
        jpeg = pdf_vision.image_to_jpeg(image.open_image())
        uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        text = ctx.complete(
            ctx.model or "",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_PROMPT},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ],
        )
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo transcribir la imagen %s", image.number, exc_info=True)
        return None
    return text.strip() if text and text.strip() else None
