from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .errors import PublishError
from .locking import IdentityLock
from .manifest import SCHEMA, build_manifest, encode_manifest, manifest_sha256, same_release_identity, utc_now, validate_manifest
from .models import Artifact, PublicationRequest, identify_artifact
from . import paths
from .profile import PublishProfile
from .providers.base import ProviderAdapter
from .staging import stage_snapshot


def _reuse_matching_manifest(
    path: Path,
    expected_manifest_bytes: bytes,
) -> bytes | None:
    if not path.exists():
        return None
    try:
        manifest_bytes = path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        validate_manifest(manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PublishError):
        raise PublishError("REMOTE_CONFLICT", "an existing local release Manifest cannot be verified safely") from None

    if not same_release_identity(manifest_bytes, expected_manifest_bytes):
        raise PublishError("REMOTE_CONFLICT", "an existing local Manifest belongs to a different immutable release identity")
    return manifest_bytes


class GenericPublisher:
    def __init__(self, profile: PublishProfile | None, providers: list[ProviderAdapter]):
        self.profile = profile
        self.providers = providers

    def preview(self, request: PublicationRequest, artifact_path: str | Path) -> dict[str, Any]:
        artifact = identify_artifact(
            artifact_path,
            product_id=request.product_id,
            version=request.version,
            channel=request.channel,
            display_name=request.display_name,
        )
        configured = [adapter.provider for adapter in self.providers]
        provider_previews: list[dict[str, Any]] = []
        for adapter in self.providers:
            inspect_artifact = getattr(adapter, "preview_artifact", None)
            if not callable(inspect_artifact):
                provider_previews.append({"provider": adapter.provider, "readiness": "NOT_INSPECTED", "identity": "UNKNOWN"})
                continue
            try:
                provider_previews.append({"provider": adapter.provider, "readiness": "READY", **inspect_artifact(request, artifact)})
            except PublishError as error:
                provider_previews.append({"provider": adapter.provider, "readiness": "NOT_READY", "error_code": error.code})
            except Exception:
                provider_previews.append({"provider": adapter.provider, "readiness": "NOT_READY", "error_code": "PROVIDER_UNAVAILABLE"})
        return {
            "PUBLISH_STATUS": "PREVIEW_READY",
            "PRODUCT_ID": request.product_id,
            "VERSION": request.version,
            "RELEASE_CHANNEL": request.channel,
            "ARTIFACT": artifact.to_public_dict(),
            "ARTIFACT_NAME": artifact.filename,
            "DISPLAY_NAME": artifact.display_name,
            "ARTIFACT_SIZE_BYTES": artifact.size_bytes,
            "SHA256": artifact.sha256,
            "PROVIDERS": configured,
            "PROVIDER_PREVIEW": provider_previews,
            "MIRROR_POLICY": self.profile.mirror_policy if self.profile else "not-configured",
            "REMOTE_WRITE_PERFORMED": False,
        }

    @staticmethod
    def _status_for_failures(failed: list[dict[str, Any]]) -> str:
        codes = {entry.get("error_code") for entry in failed}
        if "AUTH_NOT_READY" in codes:
            return "AUTH_REQUIRED"
        if "VERSION_HASH_CONFLICT" in codes:
            return "CONFLICT_VERSION_HASH"
        if "HASH_MISMATCH" in codes:
            return "HASH_MISMATCH"
        if "POLICY_REJECTED" in codes or "FILE_TOO_LARGE" in codes:
            return "POLICY_REJECTED"
        return "FAILED"

    @staticmethod
    def _provider_summary(statuses: list[dict[str, Any]]) -> dict[str, Any]:
        primary = next((item for item in statuses if item.get("role") == "primary"), None)
        mirror = next((item for item in statuses if item.get("role") == "mirror"), None)
        return {
            "PRIMARY_PROVIDER": primary.get("provider") if primary else None,
            "PRIMARY_URL": primary.get("url") if primary else None,
            "PRIMARY_REMOTE_VERIFIED": primary.get("status") == "VERIFIED" if primary else False,
            "MIRROR_PROVIDER": mirror.get("provider") if mirror else None,
            "MIRROR_URL": mirror.get("url") if mirror else None,
            "MIRROR_REMOTE_VERIFIED": mirror.get("status") == "VERIFIED" if mirror else None,
        }

    def _public_provider_record(self, adapter: ProviderAdapter, error: PublishError) -> dict[str, Any]:
        return {
            "provider": adapter.provider,
            "role": adapter.role,
            "status": "FAILED",
            "error_code": error.code,
        }

    def publish(self, request: PublicationRequest, artifact_path: str | Path) -> dict[str, Any]:
        if self.profile is None or not self.providers:
            raise PublishError("AUTH_NOT_READY", "no approved provider profile is configured")
        if request.channel not in self.profile.allowed_channels:
            raise PublishError("POLICY_REJECTED", "release channel is not authorized by the active profile")
        preview = self.preview(request, artifact_path)
        artifact = identify_artifact(
            artifact_path,
            product_id=request.product_id,
            version=request.version,
            channel=request.channel,
            display_name=request.display_name,
        )
        statuses: list[dict[str, Any]] = []
        verified_adapters: list[ProviderAdapter] = []
        all_idempotent = True
        artifact_provider_failed = False
        remote_upload_bytes = 0
        remote_download_bytes = 0
        cos_upload_bytes = 0
        cos_download_bytes = 0
        manifest_path: Path | None = None
        manifest_digest: str | None = None
        manifest_urls: list[str] = []
        manifest_results: list[dict[str, Any]] = []

        paths.ensure_runtime_roots()
        with IdentityLock(request.product_id, request.channel, request.version):
            staged_artifact = stage_snapshot(artifact)
            try:
                for adapter in self.providers:
                    try:
                        provider_result = adapter.publish_artifact(request, staged_artifact)
                        statuses.append(provider_result)
                        verified_adapters.append(adapter)
                        all_idempotent = all_idempotent and provider_result.get("idempotent") is True
                        remote_upload_bytes += int(provider_result.get("uploaded_bytes", 0))
                        remote_download_bytes += int(provider_result.get("downloaded_bytes", 0))
                        if adapter.provider == "tencent-cos":
                            cos_upload_bytes += int(provider_result.get("uploaded_bytes", 0))
                            cos_download_bytes += int(provider_result.get("downloaded_bytes", 0))
                    except PublishError as error:
                        artifact_provider_failed = True
                        statuses.append(self._public_provider_record(adapter, error))
                    except Exception:
                        artifact_provider_failed = True
                        statuses.append(self._public_provider_record(adapter, PublishError("PROVIDER_UNAVAILABLE")))

                required_failure = self.profile.mirror_policy == "required" and len(verified_adapters) != len(self.providers)
                if not verified_adapters or required_failure:
                    failed = [entry for entry in statuses if entry.get("status") != "VERIFIED"]
                    status = "PARTIAL" if verified_adapters else self._status_for_failures(failed)
                    return {
                        **preview,
                        **self._provider_summary(statuses),
                        "PUBLISH_STATUS": status,
                        "PROVIDERS": statuses,
                        "MANIFEST_PATH": None,
                        "MANIFEST_SCHEMA": SCHEMA,
                        "MANIFEST_SHA256": None,
                        "REMOTE_UPLOAD_BYTES": remote_upload_bytes,
                        "REMOTE_DOWNLOAD_BYTES": remote_download_bytes,
                        "COS_BYTES_UPLOADED": cos_upload_bytes,
                        "COS_BYTES_DOWNLOADED": cos_download_bytes,
                        "MANIFEST_PROVIDERS": [],
                        "NOT_PROVEN": ["public Manifest publication withheld until all required artifact providers verify"],
                    }

                manifest_providers = [
                    {key: value for key, value in provider.items() if key in {"provider", "role", "status", "url", "published_at", "remote_verified_at", "verified_sha256"}}
                    for provider in statuses
                    if provider.get("status") == "VERIFIED"
                ]
                provider_publication_times = [str(provider.get("published_at") or "") for provider in manifest_providers]
                if not provider_publication_times or any(not value for value in provider_publication_times):
                    return {
                        **preview,
                        **self._provider_summary(statuses),
                        "PUBLISH_STATUS": "PARTIAL",
                        "PROVIDERS": statuses,
                        "MANIFEST_PATH": None,
                        "MANIFEST_SCHEMA": SCHEMA,
                        "MANIFEST_SHA256": None,
                        "REMOTE_UPLOAD_BYTES": remote_upload_bytes,
                        "REMOTE_DOWNLOAD_BYTES": remote_download_bytes,
                        "COS_BYTES_UPLOADED": cos_upload_bytes,
                        "COS_BYTES_DOWNLOADED": cos_download_bytes,
                        "MANIFEST_PROVIDERS": [],
                        "NOT_PROVEN": ["verified provider publication timestamps were unavailable; Manifest was withheld"],
                    }
                published_at = max(provider_publication_times)
                local_manifest_dir = paths.RUNTIME_ROOT / "reports" / request.product_id / request.channel / request.version
                paths.ensure_runtime_directory(local_manifest_dir)
                manifest_path = local_manifest_dir / "release-manifest.json"
                paths.assert_runtime_path(manifest_path)
                verification_times = [
                    str(provider.get("remote_verified_at") or provider.get("published_at") or "")
                    for provider in manifest_providers
                ]
                manifest = build_manifest(
                    request,
                    staged_artifact,
                    providers=manifest_providers,
                    mirror_policy=self.profile.mirror_policy,
                    generated_at=utc_now(),
                    published_at=published_at,
                    verified_at=max(verification_times),
                )
                proposed_manifest_bytes = encode_manifest(manifest)
                manifest_bytes = _reuse_matching_manifest(manifest_path, proposed_manifest_bytes)
                if manifest_bytes is None:
                    manifest_bytes = proposed_manifest_bytes

                remote_manifests: list[bytes] = []
                manifest_read_failed = False
                for adapter in verified_adapters:
                    read_manifest = getattr(adapter, "read_manifest", None)
                    if not callable(read_manifest):
                        continue
                    try:
                        remote_bytes = read_manifest(request)
                    except PublishError as error:
                        statuses.append({"provider": adapter.provider, "status": "MANIFEST_READ_FAILED", "error_code": error.code})
                        remote_bytes = None
                        manifest_read_failed = True
                    except Exception:
                        statuses.append({"provider": adapter.provider, "status": "MANIFEST_READ_FAILED", "error_code": "PROVIDER_UNAVAILABLE"})
                        remote_bytes = None
                        manifest_read_failed = True
                    if remote_bytes is not None:
                        if not same_release_identity(remote_bytes, proposed_manifest_bytes):
                            raise PublishError("REMOTE_CONFLICT", "remote Manifest belongs to a different immutable release identity")
                        remote_manifests.append(remote_bytes)

                if manifest_read_failed:
                    return {
                        **preview,
                        **self._provider_summary(statuses),
                        "PUBLISH_STATUS": "PARTIAL",
                        "PROVIDERS": statuses,
                        "MANIFEST_PATH": manifest_path.relative_to(paths.RUNTIME_ROOT).as_posix(),
                        "MANIFEST_SCHEMA": SCHEMA,
                        "MANIFEST_SHA256": None,
                        "REMOTE_UPLOAD_BYTES": remote_upload_bytes,
                        "REMOTE_DOWNLOAD_BYTES": remote_download_bytes,
                        "COS_BYTES_UPLOADED": cos_upload_bytes,
                        "COS_BYTES_DOWNLOADED": cos_download_bytes,
                        "MANIFEST_PROVIDERS": [],
                        "NOT_PROVEN": ["remote Manifest could not be safely read; no Manifest write was attempted"],
                    }

                if remote_manifests:
                    manifest_bytes = remote_manifests[0]
                    if any(not same_release_identity(manifest_bytes, other) for other in remote_manifests[1:]):
                        raise PublishError("REMOTE_CONFLICT", "provider Manifests disagree on immutable release identity")

                # Persist the exact remote bytes on replay; a clean workspace can rebuild its local cache.
                manifest_temp = local_manifest_dir / f".manifest-{uuid.uuid4().hex}.tmp"
                paths.assert_runtime_path(manifest_temp)
                created = False
                try:
                    # Exclusive creation prevents following a pre-existing temporary-file link.
                    with manifest_temp.open("xb") as stream:
                        created = True
                        stream.write(manifest_bytes)
                    paths.assert_runtime_path(manifest_path)
                    manifest_temp.replace(manifest_path)
                finally:
                    if created:
                        manifest_temp.unlink(missing_ok=True)
                manifest_digest = manifest_sha256(manifest_bytes)

                for adapter in verified_adapters:
                    try:
                        manifest_result = adapter.publish_manifest(request, manifest_bytes, manifest_digest)
                        manifest_urls.append(str(manifest_result["url"]))
                        manifest_results.append({"provider": adapter.provider, **manifest_result})
                        remote_upload_bytes += int(manifest_result.get("uploaded_bytes", 0))
                        remote_download_bytes += int(manifest_result.get("downloaded_bytes", 0))
                        if adapter.provider == "tencent-cos":
                            cos_upload_bytes += int(manifest_result.get("uploaded_bytes", 0))
                            cos_download_bytes += int(manifest_result.get("downloaded_bytes", 0))
                    except PublishError as error:
                        statuses.append({"provider": adapter.provider, "status": "MANIFEST_FAILED", "error_code": error.code})
                    except Exception:
                        statuses.append({"provider": adapter.provider, "status": "MANIFEST_FAILED", "error_code": "PROVIDER_UNAVAILABLE"})

                manifest_required = self.profile.mirror_policy == "required"
                expected_manifest_providers = len(verified_adapters) if manifest_required else 1
                manifest_ok = len(manifest_urls) >= expected_manifest_providers
                if not manifest_ok:
                    return {
                        **preview,
                        **self._provider_summary(statuses),
                        "PUBLISH_STATUS": "PARTIAL",
                        "PROVIDERS": statuses,
                        "MANIFEST_PATH": manifest_path.relative_to(paths.RUNTIME_ROOT).as_posix(),
                        "MANIFEST_SCHEMA": SCHEMA,
                        "MANIFEST_URLS": manifest_urls,
                        "MANIFEST_SHA256": manifest_digest,
                        "MANIFEST_PROVIDERS": manifest_results,
                        "REMOTE_UPLOAD_BYTES": remote_upload_bytes,
                        "REMOTE_DOWNLOAD_BYTES": remote_download_bytes,
                        "COS_BYTES_UPLOADED": cos_upload_bytes,
                        "COS_BYTES_DOWNLOADED": cos_download_bytes,
                        "NOT_PROVEN": ["artifact URLs verified; public Manifest readback did not complete on every required provider"],
                    }

                verified_roles = {entry.get("role") for entry in statuses if entry.get("status") == "VERIFIED"}
                failed_provider = any(entry.get("status") in {"FAILED", "MANIFEST_FAILED"} for entry in statuses)
                status = "PUBLISH_SUCCESS"
                if failed_provider:
                    status = "PUBLISH_SUCCESS_WITH_OPTIONAL_DEGRADED" if "primary" in verified_roles else "PARTIAL"
                manifests_idempotent = bool(manifest_results) and all(item.get("idempotent") is True for item in manifest_results)
                if all_idempotent and manifests_idempotent and not artifact_provider_failed and not failed_provider:
                    status = "IDEMPOTENT_ALREADY_PUBLISHED"
                return {
                    **preview,
                    **self._provider_summary(statuses),
                    "PUBLISH_STATUS": status,
                    "PROVIDERS": statuses,
                    "MANIFEST_PATH": manifest_path.relative_to(paths.RUNTIME_ROOT).as_posix(),
                    "MANIFEST_SCHEMA": SCHEMA,
                    "MANIFEST_URLS": manifest_urls,
                    "MANIFEST_SHA256": manifest_digest,
                    "MANIFEST_PROVIDERS": manifest_results,
                    "PUBLISHED_AT": published_at,
                    "REMOTE_VERIFIED_AT": max((str(item.get("remote_verified_at") or "") for item in statuses if item.get("status") == "VERIFIED"), default=None),
                    "REMOTE_UPLOAD_BYTES": remote_upload_bytes,
                    "REMOTE_DOWNLOAD_BYTES": remote_download_bytes,
                    "COS_BYTES_UPLOADED": cos_upload_bytes,
                    "COS_BYTES_DOWNLOADED": cos_download_bytes,
                    "NOT_PROVEN": ["China reachability beyond the network used for this run", "code signing and installer behavior"],
                }
            finally:
                staged_artifact.source_path.unlink(missing_ok=True)


def write_json_report(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(encoded)
    temp.replace(path)
