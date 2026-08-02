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
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        # Tally and every Excel before 2007 emit these, and a CA's clients send
        # them constantly. Accepted only under a .xls/.doc name — see
        # LEGACY_OFFICE_TYPES.
        "application/msword",
        "application/vnd.ms-excel",
        "application/zip",  # some browsers send this for .xlsx/.docx
        "application/octet-stream",
    }
)

# The leading bytes of an OLE compound file: .doc and .xls, but also .ppt and
# — the reason this is not simply allowed — .msi, a Windows installer. Nothing
# in the header distinguishes them, so the filename is what decides, and only
# these two extensions get in.
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
LEGACY_OFFICE_TYPES: dict[str, str] = {
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
}

# Formats we can pull text out of without an OCR/PDF dependency. PDFs and
# images fall back to filename-based categorisation until OCR is wired up.
TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/csv", "application/json"})

# The declared Content-Type is whatever the client chose to send, so it is not
# evidence of anything. These leading bytes are: an executable is an
# executable regardless of what the upload claims to be.
#
# Each entry is (signature, what it is, what the sender can do instead). The
# advice matters for the formats a CA's client plausibly sends by accident —
# being told a bank statement "looks like an OLE container" leaves them with
# nowhere to go.
REFUSED_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"MZ", "a Windows executable", ""),
    (b"\x7fELF", "a Linux executable", ""),
    (b"\xca\xfe\xba\xbe", "a Mach-O binary", ""),
    (b"\xcf\xfa\xed\xfe", "a Mach-O binary", ""),
    (b"\xce\xfa\xed\xfe", "a Mach-O binary", ""),
    (b"#!", "a shell script", ""),
    (b"Rar!", "a RAR archive", "Upload the documents themselves rather than an archive."),
    (
        b"\x37\x7a\xbc\xaf\x27\x1c",
        "a 7-Zip archive",
        "Upload the documents themselves rather than an archive.",
    ),
    (b"\x1f\x8b", "a gzip archive", "Upload the documents themselves rather than an archive."),
)

# Signature -> the content type we record, whatever the upload declared.
CONTENT_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),  # narrowed below by the WEBP tag
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),  # .docx / .xlsx are zip containers
)


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


def legacy_office_type(filename: str) -> str | None:
    """The content type a ``.doc``/``.xls`` name claims, or None for anything else."""
    return LEGACY_OFFICE_TYPES.get(Path(filename or "").suffix.lower())


def sniff_content_type(data: bytes, filename: str = "") -> str | None:
    """The content type implied by the leading bytes, or None if unrecognised.

    ``filename`` only matters for OLE compound files, where the bytes say
    "legacy Office container" and the extension is the only thing that says
    which one.
    """
    if data.startswith(OLE_SIGNATURE):
        return legacy_office_type(filename)
    for signature, content_type in CONTENT_SIGNATURES:
        if not data.startswith(signature):
            continue
        if signature == b"RIFF":
            # RIFF is a container; only the WEBP flavour is on the allow-list.
            return "image/webp" if data[8:12] == b"WEBP" else None
        return content_type
    return None


def validate_upload(
    content_type: str | None,
    size_bytes: int,
    data: bytes = b"",
    filename: str = "",
) -> None:
    """Reject uploads that are too large, the wrong type, or not what they claim.

    ``data`` is optional so the size/type checks can run before a body is
    buffered, but callers that have the bytes should pass them: the declared
    Content-Type is client-supplied and the signature check is the only part
    of this an attacker cannot simply set.
    """
    if size_bytes > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / (1024 * 1024)
        raise UploadTooLarge(f"File exceeds the {limit_mb:.0f} MB upload limit")
    # Size rather than the bytes, so the emptiest upload is not the one that
    # gets through: guarding on `data` alone skipped a zero-byte file entirely
    # while turning whitespace away. A stored document is what marks a
    # checklist requirement satisfied, so the firm would have seen paperwork
    # as received that nobody sent.
    if size_bytes <= 0 or (data and not data.strip()):
        raise UnsupportedFileType("The file is empty")

    # The signature is read before the declared type, because it is evidence
    # and the declaration is only a claim. It also gives the better message: a
    # renamed executable is turned away as an executable, rather than as a MIME
    # type the sender never chose and cannot change.
    head = data[:32]
    for signature, description, advice in REFUSED_SIGNATURES:
        if head.startswith(signature):
            refusal = f"This file looks like {description}, which cannot be accepted"
            raise UnsupportedFileType(f"{refusal}. {advice}" if advice else refusal)

    # .doc and .xls are welcome; the container they share with .ppt and with
    # .msi installers is not. The extension is all there is to tell them apart,
    # so an OLE file that is not named like legacy Word or Excel stops here.
    if head.startswith(OLE_SIGNATURE) and legacy_office_type(filename) is None:
        raise UnsupportedFileType(
            "This file is a legacy Office container, and only .doc and .xls files "
            "are accepted in that format. Save it as .docx, .xlsx or PDF and "
            "upload that."
        )

    # A missing content type is treated as octet-stream rather than rejected;
    # browsers omit it for unusual extensions.
    if content_type and content_type.split(";")[0].strip() not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedFileType(f"Files of type {content_type} are not accepted")


def effective_content_type(declared: str | None, data: bytes, filename: str = "") -> str | None:
    """What to record as the document's type.

    The signature wins when there is one: a browser that mislabels a PDF as
    ``application/octet-stream`` should not stop the categoriser from reading
    it, and a caller that labels a JPEG as ``text/csv`` should not get it
    handed to the text extractor.
    """
    return sniff_content_type(data, filename) or declared


def save_upload(
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    filename: str,
    data: bytes,
    content_type: str | None = None,
) -> StoredFile:
    """Write ``data`` to the storage volume and describe where it went."""
    validate_upload(content_type, len(data), data, filename)

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
