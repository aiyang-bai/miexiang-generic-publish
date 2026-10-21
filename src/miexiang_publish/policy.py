from __future__ import annotations

from .errors import PublishError

GITHUB_RELEASE_MAX_BYTES = 2 * 1024 * 1024 * 1024


def check_version_binding(existing_sha256: str | None, requested_sha256: str) -> str:
    if existing_sha256 is None:
        return "NEW"
    if existing_sha256.lower() == requested_sha256.lower():
        return "IDEMPOTENT"
    raise PublishError("VERSION_HASH_CONFLICT", "this product/channel/version is already bound to different bytes")


def check_github_size(size_bytes: int) -> None:
    if size_bytes >= GITHUB_RELEASE_MAX_BYTES:
        raise PublishError("FILE_TOO_LARGE", "GitHub release assets must be smaller than 2 GiB")
