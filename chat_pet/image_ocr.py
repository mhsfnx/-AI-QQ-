from __future__ import annotations

import re
import threading
from pathlib import Path


SUPPORTED_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageTextError(RuntimeError):
    pass


def _wait_operation(operation, timeout: int = 30):  # type: ignore[no-untyped-def]
    from winsdk.windows.foundation import AsyncStatus

    completed = threading.Event()

    def done(_operation, _status) -> None:  # type: ignore[no-untyped-def]
        completed.set()

    operation.completed = done
    if operation.status != AsyncStatus.STARTED:
        completed.set()
    if not completed.wait(timeout):
        operation.cancel()
        raise ImageTextError("Windows OCR 操作超时。")
    if operation.status == AsyncStatus.ERROR:
        raise ImageTextError(
            f"Windows OCR 操作失败（HRESULT {operation.error_code}）。"
        )
    if operation.status == AsyncStatus.CANCELED:
        raise ImageTextError("Windows OCR 操作已取消。")
    return operation.get_results()


def _recognize_blocking(path: Path) -> str:
    try:
        import winsdk._winrt as winrt
        from winsdk.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapDecoder,
            BitmapPixelFormat,
            BitmapTransform,
            ColorManagementMode,
            ExifOrientationMode,
        )
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import FileAccessMode, StorageFile
    except ImportError as exc:
        raise ImageTextError("当前运行环境缺少 Windows 本地 OCR 组件。") from exc

    preferred_tags = []
    for language in OcrEngine.available_recognizer_languages or []:
        tag = str(language.language_tag)
        normalized = tag.casefold()
        if normalized.startswith("zh"):
            preferred_tags.insert(0, tag)
        elif normalized.startswith("en"):
            preferred_tags.append(tag)
    if not preferred_tags:
        available = [
            str(language.language_tag)
            for language in (OcrEngine.available_recognizer_languages or [])
        ]
        preferred_tags = available[:2]
    engines = [
        engine
        for engine in (
            OcrEngine.try_create_from_language(Language(tag))
            for tag in preferred_tags
        )
        if engine is not None
    ]
    if not engines:
        raise ImageTextError(
            "Windows 本地 OCR 没有可用语言。请在 Windows 语言设置中安装中文 OCR。"
        )
    initialized = False
    try:
        winrt.init_apartment(0)
        initialized = True
    except OSError:
        pass
    try:
        storage_file = _wait_operation(
            StorageFile.get_file_from_path_async(str(path.resolve()))
        )
        stream = _wait_operation(storage_file.open_async(FileAccessMode.READ))
        decoder = _wait_operation(BitmapDecoder.create_async(stream))
        width = int(decoder.pixel_width or 0)
        height = int(decoder.pixel_height or 0)
        maximum = int(OcrEngine.max_image_dimension or 0)
        if maximum and max(width, height) > maximum:
            scale = maximum / max(width, height)
            transform = BitmapTransform()
            transform.scaled_width = max(1, int(width * scale))
            transform.scaled_height = max(1, int(height * scale))
            bitmap = _wait_operation(
                decoder.get_software_bitmap_async(
                    BitmapPixelFormat.BGRA8,
                    BitmapAlphaMode.PREMULTIPLIED,
                    transform,
                    ExifOrientationMode.RESPECT_EXIF_ORIENTATION,
                    ColorManagementMode.DO_NOT_COLOR_MANAGE,
                )
            )
        else:
            bitmap = _wait_operation(decoder.get_software_bitmap_async())
        results = [
            _wait_operation(engine.recognize_async(bitmap))
            for engine in engines[:2]
        ]
    finally:
        if initialized:
            winrt.uninit_apartment()
    texts = []
    for result in results:
        lines = [
            _clean_ocr_line(str(line.text))
            for line in (result.lines or [])
            if str(line.text).strip()
        ]
        text = "\n".join(lines) or _clean_ocr_line(str(result.text or ""))
        if text:
            texts.append(text)
    if not texts:
        return ""
    return max(
        texts,
        key=lambda value: (
            sum("\u4e00" <= char <= "\u9fff" for char in value) * 3
            + sum(char.isalnum() for char in value)
        ),
    )


def validate_image_file(path: Path) -> Path:
    path = Path(path)
    if not path.is_file():
        raise ImageTextError("选择的图片不存在。")
    if path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ImageTextError(
            "只支持 PNG、JPG、BMP、WEBP 图片；GIF 请选择静态帧图片。"
        )
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ImageTextError("图片不能超过 20 MB。")
    return path


def _clean_ocr_line(value: str) -> str:
    return re.sub(
        r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
        "",
        value.strip(),
    )


def extract_image_text(path: Path) -> str:
    path = validate_image_file(path)
    try:
        text = _recognize_blocking(path)
    except ImageTextError:
        raise
    except Exception as exc:
        raise ImageTextError(f"本地 OCR 识别失败：{exc}") from exc
    text = text.strip()
    if not text:
        raise ImageTextError(
            "这张图片没有识别到可读文字。当前版本不会分析纯画面内容。"
        )
    if len(text) > 12000:
        text = text[:12000] + "\n（图片文字过长，已截断）"
    return text


__all__ = [
    "ImageTextError",
    "SUPPORTED_IMAGE_SUFFIXES",
    "extract_image_text",
    "validate_image_file",
]
