import pytest
from pathlib import Path
from types import SimpleNamespace

from app.media import detect_document_type, validate_uploaded_file


@pytest.mark.parametrize(
    ("header", "mime", "filename", "expected"),
    [
        (b"%PDF-1.7", "application/pdf", "receipt.pdf", "application/pdf"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png", "image.png", "image/png"),
        (b"\xff\xd8\xffrest", "image/jpeg", "proof.jpg", "image/jpeg"),
        (b"OggSrest", "audio/ogg", "voice.ogg", "audio/ogg"),
        (b"safe text\n", "text/plain", "notes.txt", "text/plain"),
        (b"%PDF-1.7", "image/png", "receipt.pdf", None),
        (b"MZ\x00\x00", "application/octet-stream", "program.exe", None),
    ],
)
def test_document_type_is_checked_against_content_and_metadata(header, mime, filename, expected):
    assert detect_document_type(header, mime, filename) == expected


class FakeBot:
    def __init__(self, payload):
        self.payload = payload

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id)

    async def download_file(self, file_path, destination):
        Path(destination).write_bytes(self.payload)


@pytest.mark.asyncio
async def test_upload_validation_checks_actual_bytes_and_size():
    valid_type, valid_error = await validate_uploaded_file(
        FakeBot(b"\xff\xd8\xffvalid image bytes"), "photo", 20, 100, {"image/jpeg"}
    )
    invalid_type, invalid_error = await validate_uploaded_file(
        FakeBot(b"MZ\x00\x00executable"), "photo", 20, 100, {"image/jpeg"}
    )
    oversized_type, oversized_error = await validate_uploaded_file(
        FakeBot(b"\xff\xd8\xff"), "photo", 101, 100, {"image/jpeg"}
    )

    assert valid_type == "image/jpeg" and valid_error is None
    assert invalid_type is None and invalid_error
    assert oversized_type is None and oversized_error


@pytest.mark.asyncio
async def test_text_upload_rejects_invalid_utf8_beyond_header():
    payload = b"a" * 5000 + b"\xff"
    detected, error = await validate_uploaded_file(
        FakeBot(payload), "text", len(payload), 6000, {"text/plain"}, "text/plain", "notes.txt"
    )

    assert detected is None
    assert error


@pytest.mark.asyncio
async def test_text_upload_accepts_utf8_character_split_across_chunks():
    payload = b"a" * 65535 + "é\n".encode("utf-8")
    detected, error = await validate_uploaded_file(
        FakeBot(payload), "text", len(payload), 70000, {"text/plain"}, "text/plain", "notes.txt"
    )

    assert detected == "text/plain"
    assert error is None