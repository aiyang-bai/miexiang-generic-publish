from __future__ import annotations


class PublishError(RuntimeError):
    """A bounded, user-safe publication error."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code.replace("_", " ").lower()
        super().__init__(self.message)


ERROR_CODES = {
    "AUTH_NOT_READY",
    "REMOTE_CONFLICT",
    "VERSION_HASH_CONFLICT",
    "UPLOAD_FAILED",
    "DOWNLOAD_FAILED",
    "HASH_MISMATCH",
    "PROVIDER_UNAVAILABLE",
    "FILE_TOO_LARGE",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_INVALID",
    "POLICY_REJECTED",
    "LOCK_HELD",
    "MANIFEST_INVALID",
}
