from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .errors import PublishError
from .models import Artifact, PublicationRequest

SCHEMA = "MIEXIANG_RELEASE_MANIFEST_V1"
_HASH = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_manifest(
    request: PublicationRequest,
    artifact: Artifact,
    providers: list[dict[str, Any]],
    mirror_policy: str,
    generated_at: str | None = None,
    published_at: str | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    provider = providers[0] if providers else {}
    verification_times = [
        str(item.get("remote_verified_at") or item.get("published_at") or "")
        for item in providers
    ]
    verification_times = [value for value in verification_times if value]
    verified_at = verified_at or (max(verification_times) if verification_times else None) or generated_at or published_at or utc_now()
    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "product_id": request.product_id,
        "version": request.version,
        "channel": request.channel,
        "release_channel": request.channel,
        "generated_at": generated_at or utc_now(),
        "published_at": published_at or utc_now(),
        "verified_at": verified_at,
        "display_name": artifact.display_name,
        "artifact_name": artifact.filename,
        "size": artifact.size_bytes,
        "sha256": artifact.sha256,
        "provider": provider.get("provider"),
        "remote_url": provider.get("url"),
        "remote_sha256": provider.get("verified_sha256"),
        "verification_status": provider.get("status"),
        "release_notes": request.release_notes,
        "mirror_policy": mirror_policy,
        "artifacts": [artifact.to_public_dict()],
        "providers": providers,
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    try:
        if manifest.get("schema") != SCHEMA or manifest.get("schema_version") != SCHEMA:
            raise ValueError("unsupported schema")
        if not isinstance(manifest.get("artifacts"), list) or len(manifest["artifacts"]) != 1:
            raise ValueError("exactly one artifact is required")
        artifact = manifest["artifacts"][0]
        if not _HASH.fullmatch(str(artifact.get("sha256", ""))):
            raise ValueError("invalid artifact digest")
        if int(artifact.get("size_bytes", -1)) < 0:
            raise ValueError("invalid artifact size")
        if manifest.get("channel") != manifest.get("release_channel"):
            raise ValueError("channel aliases differ")
        if manifest.get("display_name") != artifact.get("display_name"):
            raise ValueError("display name alias differs")
        if manifest.get("artifact_name") != artifact.get("filename"):
            raise ValueError("artifact name alias differs")
        if manifest.get("size") != artifact.get("size_bytes"):
            raise ValueError("artifact size alias differs")
        if manifest.get("sha256") != artifact.get("sha256"):
            raise ValueError("artifact digest alias differs")
        if not isinstance(manifest.get("verified_at"), str) or not manifest["verified_at"]:
            raise ValueError("verification timestamp is required")
        for forbidden in ("source_path", "local_path", "machine", "hostname"):
            if forbidden in manifest or forbidden in artifact:
                raise ValueError("private local data is not allowed")
        if not isinstance(manifest.get("providers"), list) or not manifest["providers"]:
            raise ValueError("provider records are required")
        first_provider = manifest["providers"][0]
        if manifest.get("provider") != first_provider.get("provider"):
            raise ValueError("provider alias differs")
        if manifest.get("remote_url") != first_provider.get("url"):
            raise ValueError("remote URL alias differs")
        if manifest.get("remote_sha256") != first_provider.get("verified_sha256"):
            raise ValueError("remote digest alias differs")
        if manifest.get("verification_status") != first_provider.get("status"):
            raise ValueError("verification status alias differs")
        for provider in manifest["providers"]:
            status = provider.get("status")
            digest = provider.get("verified_sha256")
            if status != "VERIFIED" or not digest or digest != artifact["sha256"]:
                raise ValueError("verified provider digest differs from artifact digest")
            url = provider.get("url", "")
            if not str(url).startswith("https://"):
                raise ValueError("verified provider URL must use HTTPS")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PublishError("MANIFEST_INVALID", "release manifest failed schema validation") from None


def encode_manifest(manifest: dict[str, Any]) -> bytes:
    validate_manifest(manifest)
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def manifest_sha256(manifest_bytes: bytes) -> str:
    return hashlib.sha256(manifest_bytes).hexdigest()


_OBSERVATION_FIELDS = frozenset({"generated_at", "published_at", "verified_at", "remote_verified_at"})


def _without_observation_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_observation_fields(item)
            for key, item in value.items()
            if key not in _OBSERVATION_FIELDS
        }
    if isinstance(value, list):
        return [_without_observation_fields(item) for item in value]
    return value


def same_release_identity(first_bytes: bytes, second_bytes: bytes) -> bool:
    """Compare valid Manifests while ignoring only observation timestamps."""
    try:
        first = json.loads(first_bytes.decode("utf-8"))
        second = json.loads(second_bytes.decode("utf-8"))
        validate_manifest(first)
        validate_manifest(second)
    except (UnicodeDecodeError, json.JSONDecodeError, PublishError, AttributeError, TypeError):
        return False
    return _without_observation_fields(first) == _without_observation_fields(second)


def encode_sha256_sums(artifact: Artifact) -> bytes:
    return f"{artifact.sha256}  {artifact.filename}\n".encode("ascii")
