"""Deterministic local fixture only; no provider access or public release receipt."""
import hashlib
import io
from pathlib import Path
from .errors import PublishError
from .manifest import SCHEMA, build_manifest, encode_manifest, same_release_identity
from .models import Artifact, PublicationRequest, verify_stream
from .policy import check_version_binding


def run_demo() -> dict:
    payload = b"miexiang offline synthetic fixture\n"
    digest = hashlib.sha256(payload).hexdigest()
    verify_stream(io.BytesIO(payload), len(payload), digest)
    rejected = []
    for name, data in (("truncated", payload[:-1]), ("oversized", payload + b"x"), ("wrong_hash", b"x" * len(payload))):
        try:
            verify_stream(io.BytesIO(data), len(payload), digest)
        except PublishError as error:
            if error.code != "HASH_MISMATCH":
                raise RuntimeError("unexpected fixture failure code")
            rejected.append(name)
        else:
            raise RuntimeError("fixture mismatch was accepted")
    check_version_binding(existing_sha256=digest, requested_sha256=digest)
    try:
        check_version_binding(existing_sha256=digest, requested_sha256="0" * 64)
    except PublishError as error:
        if error.code != "VERSION_HASH_CONFLICT":
            raise RuntimeError("unexpected conflict failure code")
    else:
        raise RuntimeError("version conflict was accepted")
    request = PublicationRequest("offline-fixture", "1.0.0", "test")
    artifact = Artifact("offline-fixture-1.0.0.bin", "fixture.bin", len(payload), digest, "application/octet-stream", Path("synthetic.bin"))
    # Synthetic provider evidence exists only in memory to exercise the schema.
    provider = {"provider": "github-releases", "role": "mirror", "status": "VERIFIED",
                "url": "https://github.com/example/release-fixture/releases/download/fixture/fixture.bin",
                "published_at": "2026-01-01T00:00:00Z", "remote_verified_at": "2026-01-01T00:00:00Z", "verified_sha256": digest}
    manifest = build_manifest(request, artifact, [provider], "required", generated_at="2026-01-01T00:00:00Z", published_at="2026-01-01T00:00:00Z")
    original = encode_manifest(manifest)
    manifest["generated_at"] = "2026-01-02T00:00:00Z"
    if not same_release_identity(original, encode_manifest(manifest)):
        raise RuntimeError("observation time changed stable fixture identity")
    return {"PUBLISH_STATUS": "OFFLINE_FIXTURE_PASS", "MODE": "OFFLINE_SYNTHETIC",
            "REMOTE_WRITE_PERFORMED": False, "REMOTE_VERIFICATION": "NOT_RUN",
            "SHA256": digest, "SIZE_BYTES": len(payload), "MANIFEST_SCHEMA": SCHEMA,
            "CHECKS": ["local_hash", "same_bytes_retry", "different_bytes_conflict", "manifest_observation_time_drift", *rejected]}
