from __future__ import annotations

import codecs
import tempfile
from collections.abc import Iterable
from pathlib import Path

from aiogram import Bot
from aiogram.types import Document, Message


EXTENSIONS = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "text/plain": {".txt"},
    "audio/ogg": {".ogg"},
    "audio/mpeg": {".mp3"},
}
MIME_ALIASES = {"application/x-pdf": "application/pdf", "application/octet-stream": None}


def detect_document_type(header: bytes, declared_mime: str | None, filename: str | None) -> str | None:
    if header.startswith(b"%PDF-"):
        actual = "application/pdf"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        actual = "image/webp"
    elif header.startswith(b"OggS"):
        actual = "audio/ogg"
    elif header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        actual = "audio/mpeg"
    else:
        try:
            sample = header.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not sample or "\x00" in sample or any(ord(char) < 32 and char not in "\n\r\t" for char in sample):
            return None
        actual = "text/plain"

    declared = MIME_ALIASES.get(declared_mime, declared_mime)
    if declared and declared != actual:
        return None
    if filename and Path(filename).suffix.lower() not in EXTENSIONS[actual]:
        return None
    return actual


async def validate_uploaded_file(
    bot: Bot,
    file_id: str,
    file_size: int | None,
    max_bytes: int,
    allowed_types: Iterable[str],
    declared_mime: str | None = None,
    filename: str | None = None,
) -> tuple[str | None, str | None]:
    if file_size is None or file_size <= 0 or file_size > max_bytes:
        return None, "حجم فایل نامعتبر است یا از سقف مجاز بیشتر است."
    if filename and (len(filename) > 180 or any(ord(char) < 32 for char in filename)):
        return None, "نام فایل معتبر نیست."
    try:
        remote = await bot.get_file(file_id)
        if not remote.file_path:
            return None, "دریافت امن فایل ممکن نشد."
        with tempfile.TemporaryDirectory(prefix="request-bot-") as temporary_directory:
            path = Path(temporary_directory) / "upload.bin"
            await bot.download_file(remote.file_path, destination=path)
            if path.stat().st_size <= 0 or path.stat().st_size > max_bytes:
                return None, "حجم فایل نامعتبر است یا از سقف مجاز بیشتر است."
            with path.open("rb") as handle:
                header = handle.read(4096)
            actual_type = detect_document_type(header, declared_mime, filename)
            if actual_type == "text/plain" and not _is_valid_text_file(path):
                actual_type = None
        if actual_type not in allowed_types:
            return None, "نوع واقعی فایل با فهرست فایل‌های مجاز سازگار نیست."
        return actual_type, None
    except Exception:
        return None, "بررسی فایل ناموفق بود؛ فایل ذخیره یا ارسال نشد."


def _is_valid_text_file(path: Path) -> bool:
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                text = decoder.decode(chunk)
                if "\x00" in text or any(
                    ord(char) < 32 and char not in "\n\r\t" for char in text
                ):
                    return False
            final_text = decoder.decode(b"", final=True)
            if "\x00" in final_text or any(
                ord(char) < 32 and char not in "\n\r\t" for char in final_text
            ):
                return False
        return True
    except UnicodeDecodeError:
        return False


async def validate_document(bot: Bot, document: Document, max_bytes: int) -> tuple[bool, str | None]:
    actual_type, error = await validate_uploaded_file(
        bot,
        document.file_id,
        document.file_size,
        max_bytes,
        EXTENSIONS,
        document.mime_type,
        document.file_name,
    )
    return actual_type is not None, error


async def message_media(message: Message, bot: Bot, max_bytes: int) -> tuple[str, str | None, str | None, str | None]:
    if message.text is not None:
        if len(message.text) > 4000:
            return "invalid", None, None, "متن پیام بیش از حد مجاز است."
        return "text", None, message.text, None

    caption = message.caption or None
    if caption and len(caption) > 1000:
        return "invalid", None, None, "متن همراه فایل بیش از حد مجاز است."
    if message.photo:
        photo = message.photo[-1]
        actual_type, error = await validate_uploaded_file(
            bot,
            photo.file_id,
            photo.file_size,
            max_bytes,
            {"image/png", "image/jpeg", "image/webp"},
        )
        if not actual_type:
            return "invalid", None, None, error
        return "photo", photo.file_id, caption, None
    if message.voice:
        actual_type, error = await validate_uploaded_file(
            bot, message.voice.file_id, message.voice.file_size, max_bytes, {"audio/ogg"}
        )
        if not actual_type:
            return "invalid", None, None, error
        return "voice", message.voice.file_id, caption, None
    if message.document:
        valid, error = await validate_document(bot, message.document, max_bytes)
        if not valid:
            return "invalid", None, None, error
        return "document", message.document.file_id, caption, None
    if message.audio:
        actual_type, error = await validate_uploaded_file(
            bot,
            message.audio.file_id,
            message.audio.file_size,
            max_bytes,
            {"audio/ogg", "audio/mpeg"},
            message.audio.mime_type,
            message.audio.file_name,
        )
        if not actual_type:
            return "invalid", None, None, error
        return "audio", message.audio.file_id, caption, None
    return "invalid", None, None, "این نوع پیام پشتیبانی نمی‌شود. متن، عکس، ویس یا فایل مجاز بفرست."