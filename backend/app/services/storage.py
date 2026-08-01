"""Local filesystem storage for client document uploads.

Files land under ``settings.storage_dir/<firm_id>/<client_id>/<uuid>__<name>``.
The UUID prefix means two uploads of "bank.pdf" never collide, and the original
filename is preserved for display. Swap this module for an S3-compatible
backend on deploy — the rest of the app only uses ``save_upload`` /
``open_stored`` / ``delete_stored``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# Anything outside this set is collapsed to "_", which also removes "/" and
# "..", so a hostile filename cannot escape the storage root.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_STORED_NAME = 120

# Uploads a CA firm actually receives. Rejecting everything else keeps
# executables and archives out of the storage volume.
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/tiff",
        "text/plain",
        "text/csv",
        "application/json",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",  # some browsers send this for .xlsx/.docx
        "application/octet-stream",
    }
)

# Formats we can pull text out of without an OCR/PDF dependency. PDFs and
# images fall back to filename-based categorisation until OCR is wired up.
TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/csv", "application/json"})


class UploadTooLarge(ValueError):
    pass


class UnsupportedFileType(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    storage_path: str
    checksum_sha256: str
    size_bytes: int
    safe_filename: str


def storage_root() -> Path:
    return Path(settings.storage_dir).expanduser().resolve()


def safe_filename(filename: str) -> str:
    """Reduce a client-supplied filename to something safe to put on disk."""
    stem = Path(filename or "").name or "upload"
    cleaned = _UNSAFE.sub("_", stem).strip("._-") or "upload"
    return cleaned[-MAX_STORED_NAME:]


def validate_upload(content_type: str | None, size_bytes: int) -> None:
    if size_bytes > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / (1024 * 1024)
        raise UploadTooLarge(f"File exceeds the {limit_mb:.0f} MB upload limit")
    # A missing content type is treated as octet-stream rather than rejected;
    # browsers omit it for unusual extensions.
    if content_type and content_type.split(";")[0].strip() not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedFileType(f"Files of type {content_type} are not accepted")


def save_upload(
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    filename: str,
    data: bytes,
    content_type: str | None = None,
) -> StoredFile:
    """Write ``data`` to the storage volume and describe where it went."""
    validate_upload(content_type, len(data))

    name = safe_filename(filename)
    relative = Path(str(firm_id)) / str(client_id) / f"{uuid.uuid4().hex}__{name}"
    destination = storage_root() / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)

    return StoredFile(
        storage_path=str(relative),
        checksum_sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        safe_filename=name,
    )


def resolve_stored(storage_path: str) -> Path:
    """Map a stored relative path back to an absolute one, refusing escapes."""
    root = storage_root()
    candidate = (root / storage_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Storage path escapes the storage root")
    return candidate


def open_stored(storage_path: str) -> bytes:
    return resolve_stored(storage_path).read_bytes()


def delete_stored(storage_path: str) -> bool:
    """Remove a stored file. Returns False if it was already gone."""
    try:
        path = resolve_stored(storage_path)
    except ValueError:
        return False
    if not path.exists():
        return False
    path.unlink()
    return True


def text_excerpt(data: bytes, content_type: str | None, filename: str = "") -> str:
    """Best-effort plain text from an upload, for AI categorisation.

    Returns "" for PDFs and images — the categoriser then works from the
    filename alone, which is what it already does when no text is available.
    """
    base_type = (content_type or "").split(";")[0].strip()
    is_texty = base_type in TEXT_CONTENT_TYPES or filename.lower().endswith(
        (".txt", ".csv", ".json")
    )
    if not is_texty:
        return ""
    return data[: settings.document_excerpt_chars * 4].decode("utf-8", errors="replace")[
        : settings.document_excerpt_chars
    ]
