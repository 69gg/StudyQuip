"""原件不可变保存；PDFium 仅在隔离子进程中使用。"""

import base64
import io
import multiprocessing
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from .config import Settings

register_heif_opener()


def safe_path(settings: Settings, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = settings.files_dir / path
    path = path.resolve()
    if not path.is_relative_to(settings.files_dir.resolve()):
        raise ValueError("文件路径不在资料目录中")
    return path


def _asset(
    settings: Settings,
    path: Path,
    name: str,
    mime: str,
    *,
    asset_id: str | None = None,
    preview: Path | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    identifier = asset_id or str(uuid.uuid4())
    return {
        "id": identifier,
        "name": name,
        "mime": mime,
        "path": str(path.relative_to(settings.files_dir)),
        "preview_path": str(preview.relative_to(settings.files_dir)) if preview else None,
        "url": f"/api/assets/{identifier}/file",
        "preview_url": f"/api/assets/{identifier}/preview",
        "width": width,
        "height": height,
    }


def store_upload(settings: Settings, filename: str, content: bytes) -> dict[str, Any]:
    if not content:
        raise ValueError("上传文件为空")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"文件超过 {settings.max_upload_mb} MB 上限")
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    identifier = str(uuid.uuid4())
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    audio_type = audio_mime(content, suffix)
    if audio_type:
        path = settings.files_dir / f"{identifier}{suffix}"
        path.write_bytes(content)
        return _asset(settings, path, name, audio_type, asset_id=identifier)
    if content.startswith(b"%PDF-"):
        path = settings.files_dir / f"{identifier}.pdf"
        path.write_bytes(content)
        return _asset(settings, path, name, "application/pdf", asset_id=identifier)
    if suffix in {".txt", ".md", ".text"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件请使用 UTF-8 编码") from exc
        path = settings.files_dir / f"{identifier}.txt"
        path.write_bytes(content)
        return _asset(settings, path, name, "text/plain", asset_id=identifier)
    try:
        with Image.open(io.BytesIO(content)) as raw:
            if raw.width * raw.height > settings.max_image_pixels:
                raise ValueError("图像像素超过上限，请裁剪或降低分辨率")
            fmt = (raw.format or "").upper()
            if fmt not in {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}:
                raise ValueError("仅支持 JPEG、PNG、WebP、HEIC/HEIF、PDF 或 UTF-8 文本")
            original = settings.files_dir / f"{identifier}.original"
            original.write_bytes(content)
            image = ImageOps.exif_transpose(raw).convert("RGB")
            preview = settings.files_dir / f"{identifier}.jpg"
            image.save(preview, quality=94)
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt, "image/heif")
            return _asset(
                settings,
                original,
                name,
                mime,
                asset_id=identifier,
                preview=preview,
                width=image.width,
                height=image.height,
            )
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法读取图像，文件可能损坏或格式不受支持") from exc


def image_data_url(settings: Settings, asset: dict[str, Any]) -> str:
    path = safe_path(settings, asset.get("preview_path") or asset["path"])
    mime = "image/jpeg" if asset.get("preview_path") else asset.get("mime", "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def audio_mime(content: bytes, suffix: str) -> str | None:
    """Recognize supported audio containers, never trust the uploaded MIME header."""
    if suffix == ".wav" and content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        return "audio/wav"
    if suffix in {".mp3", ".mpeg", ".mpga"} and (
        content.startswith(b"ID3") or (len(content) > 1 and content[0] == 255 and content[1] & 224 == 224)
    ):
        return "audio/mpeg"
    if suffix in {".ogg", ".oga", ".opus"} and content.startswith(b"OggS"):
        return "audio/ogg"
    if suffix == ".flac" and content.startswith(b"fLaC"):
        return "audio/flac"
    if suffix in {".m4a", ".mp4"} and content[4:8] == b"ftyp":
        return "audio/mp4"
    if suffix == ".webm" and content.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    return None


def crop_asset(settings: Settings, asset: dict[str, Any], box: list[float]) -> dict[str, Any]:
    if not asset["mime"].startswith("image/") or len(box) != 4:
        raise ValueError("仅支持裁剪图片，坐标需要四个值")
    source = safe_path(settings, asset.get("preview_path") or asset["path"])
    with Image.open(source) as image:
        x1, y1, x2, y2 = (round(value) for value in box)
        if not (0 <= x1 < x2 <= image.width and 0 <= y1 < y2 <= image.height):
            raise ValueError("裁剪坐标超出已归正图像范围")
        buffer = io.BytesIO()
        image.crop((x1, y1, x2, y2)).save(buffer, format="PNG")
    result = store_upload(settings, f"裁剪-{asset['name']}.png", buffer.getvalue())
    return {**result, "source_asset_id": asset["id"], "crop_box": box}


def _pdf_count(path: str, max_pages: int) -> int:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(path) as pdf:
        count = len(pdf)
        if count > max_pages:
            raise ValueError(f"PDF 超过 {max_pages} 页上限")
        return count


def _pdf_page(path: str, index: int, scale: float, destination: str, max_pixels: int) -> dict[str, Any]:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(path) as pdf:
        with closing(pdf[index]) as page:
            width, height = page.get_size()
            if width * height * scale * scale > max_pixels:
                raise ValueError("PDF 页面渲染像素超过上限")
            with closing(page.get_textpage()) as textpage:
                text = textpage.get_text_bounded()
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB")
                image.save(destination, quality=94)
                return {"text": text, "width": image.width, "height": image.height}
            finally:
                bitmap.close()


def render_pdf_page(
    settings: Settings, asset: dict[str, Any], index: int, scale: float = 2
) -> dict[str, Any]:
    identifier = str(uuid.uuid4())
    destination = settings.files_dir / f"{identifier}.jpg"
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        result = pool.submit(
            _pdf_page,
            str(safe_path(settings, asset["path"])),
            index,
            scale,
            str(destination),
            settings.max_image_pixels,
        ).result(timeout=settings.pdf_timeout_seconds)
    image_asset = _asset(
        settings,
        destination,
        f"{asset['name']} · {index + 1}",
        "image/jpeg",
        asset_id=identifier,
        width=result["width"],
        height=result["height"],
    )
    return {
        "source_asset_id": asset["id"],
        "page_index": index,
        "text": result["text"],
        "image_asset": image_asset,
        "image_path": image_asset["path"],
        "source_type": "pdf",
    }


def prepare_book_pages(settings: Settings, asset: dict[str, Any]) -> list[dict[str, Any]]:
    if asset["mime"] == "application/pdf":
        source = str(safe_path(settings, asset["path"]))
        context = multiprocessing.get_context("spawn")
        pages: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
            count = pool.submit(_pdf_count, source, settings.max_pdf_pages).result(
                timeout=settings.pdf_timeout_seconds
            )
            for index in range(count):
                identifier = str(uuid.uuid4())
                destination = settings.files_dir / f"{identifier}.jpg"
                result = pool.submit(
                    _pdf_page, source, index, settings.pdf_scale, str(destination), settings.max_image_pixels
                ).result(timeout=settings.pdf_timeout_seconds)
                image_asset = _asset(
                    settings,
                    destination,
                    f"{asset['name']} · {index + 1}",
                    "image/jpeg",
                    asset_id=identifier,
                    width=result["width"],
                    height=result["height"],
                )
                pages.append(
                    {
                        "source_asset_id": asset["id"],
                        "page_index": index,
                        "text": result["text"],
                        "image_asset": image_asset,
                        "image_path": image_asset["path"],
                        "source_type": "pdf",
                    }
                )
        return pages
    if asset["mime"].startswith("image/"):
        return [
            {
                "source_asset_id": asset["id"],
                "page_index": 0,
                "text": "",
                "image_asset": asset,
                "image_path": asset.get("preview_path") or asset["path"],
                "source_type": "image",
            }
        ]
    if asset["mime"] == "text/plain":
        text = safe_path(settings, asset["path"]).read_text(encoding="utf-8-sig")
        return [
            {
                "source_asset_id": asset["id"],
                "page_index": 0,
                "text": text,
                "image_asset": None,
                "image_path": None,
                "source_type": "text",
            }
        ]
    raise ValueError("不支持该教材格式")
