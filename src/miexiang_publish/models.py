from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import PublishError
from .redaction import redact_text

_PRODUCT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,30})(?:[.+][A-Za-z0-9](?:[A-Za-z0-9-]{0,30}))*$")
_CHANNELS = {"test", "candidate", "stable"}
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,12}$")
_LOCAL_PATH = re.compile(
    r"(?i)(?:^|[\s=(\"'])((?:[a-z]:[\\/]|\\\\)[^\s,;\"']+|/(?!/)[^\s,;\"']+)"
)
_LOCAL_FILE_URL = re.compile(r"(?i)\bfile://")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class PublicationRequest:
    product_id: str
    version: str
    channel: str
    display_name: str | None = None
    release_notes: str | None = None

    def __post_init__(self) -> None:
        if not _PRODUCT_ID.fullmatch(self.product_id):
            raise PublishError("POLICY_REJECTED", "product_id must be a lowercase slug")
        if not _VERSION.fullmatch(self.version) or self.version in {".", ".."}:
            raise PublishError("POLICY_REJECTED", "version contains unsupported characters")
        if self.channel not in _CHANNELS:
            raise PublishError("POLICY_REJECTED", "channel must be test, candidate, or stable")
        if self.display_name is not None and ("\x00" in self.display_name or len(self.display_name) > 240):
            raise PublishError("POLICY_REJECTED", "display_name is invalid or too long")
        if self.release_notes is not None and ("\x00" in self.release_notes or len(self.release_notes) > 12000):
            raise PublishError("POLICY_REJECTED", "release_notes exceed the supported limit")
        metadata = "\n".join(value for value in (self.display_name, self.release_notes) if value)
        if _LOCAL_PATH.search(metadata) or _LOCAL_FILE_URL.search(metadata) or redact_text(metadata) != metadata:
            raise PublishError("POLICY_REJECTED", "release metadata may not contain credentials or local filesystem paths")


@dataclass(frozen=True)
class Artifact:
    filename: str
    display_name: str
    size_bytes: int
    sha256: str
    content_type: str
    source_path: Path

    def to_public_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "display_name": self.display_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class StreamVerification:
    size_bytes: int
    sha256: str


def canonical_artifact_name(product_id: str, version: str, original_name: str) -> str:
    """Generate an ASCII provider name without changing the source filename."""
    suffix = Path(original_name).suffix.lower().lstrip(".")
    extension = suffix if _SAFE_EXTENSION.fullmatch(suffix) else "bin"
    stem = f"{product_id}-{version}"
    return f"{stem}.{extension}"


def _reject_reparse_or_link(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        try:
            item_stat = os.lstat(cursor)
        except OSError:
            raise PublishError("ARTIFACT_NOT_FOUND", "artifact path cannot be inspected") from None
        if stat.S_ISLNK(item_stat.st_mode) or getattr(item_stat, "st_file_attributes", 0) & _REPARSE_POINT:
            raise PublishError("ARTIFACT_INVALID", "artifact paths may not traverse links or reparse points")


def hash_file(path: Path) -> StreamVerification:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
    except OSError:
        raise PublishError("ARTIFACT_INVALID", "artifact cannot be read") from None
    return StreamVerification(size_bytes=size, sha256=digest.hexdigest())


def identify_artifact(
    artifact_path: str | os.PathLike[str],
    product_id: str,
    version: str,
    channel: str,
    display_name: str | None = None,
) -> Artifact:
    PublicationRequest(product_id=product_id, version=version, channel=channel)
    path = Path(artifact_path)
    if not path.is_absolute():
        raise PublishError("ARTIFACT_INVALID", "artifact_path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise PublishError("ARTIFACT_NOT_FOUND", "artifact does not exist") from None
    _reject_reparse_or_link(path)
    try:
        info = resolved.stat()
    except OSError:
        raise PublishError("ARTIFACT_NOT_FOUND", "artifact does not exist") from None
    if not stat.S_ISREG(info.st_mode):
        raise PublishError("ARTIFACT_INVALID", "artifact_path must name one regular file")
    before = (info.st_size, info.st_mtime_ns)
    hashed = hash_file(resolved)
    try:
        after_stat = resolved.stat()
    except OSError:
        raise PublishError("ARTIFACT_INVALID", "artifact changed while it was being identified") from None
    if before != (after_stat.st_size, after_stat.st_mtime_ns) or hashed.size_bytes != before[0]:
        raise PublishError("ARTIFACT_INVALID", "artifact changed while it was being identified")
    name = resolved.name
    mime = mimetypes.guess_type(name, strict=False)[0] or "application/octet-stream"
    return Artifact(
        filename=canonical_artifact_name(product_id, version, name),
        display_name=display_name or name,
        size_bytes=hashed.size_bytes,
        sha256=hashed.sha256,
        content_type=mime,
        source_path=resolved,
    )


def verify_stream(stream: BinaryIO, expected_size: int, expected_sha256: str) -> StreamVerification:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        digest.update(block)
    actual = digest.hexdigest()
    if size != expected_size or actual != expected_sha256:
        raise PublishError("HASH_MISMATCH", "downloaded bytes do not match the local artifact")
    return StreamVerification(size_bytes=size, sha256=actual)
