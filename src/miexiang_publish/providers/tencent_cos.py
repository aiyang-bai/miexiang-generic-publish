from __future__ import annotations

import json
import hashlib
import logging
import secrets
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from ..credentials import CosCredentials, read_cos_credentials
from ..errors import PublishError
from ..http_verify import download_and_verify
from ..manifest import encode_sha256_sums, utc_now
from ..models import Artifact, PublicationRequest, verify_stream
from ..policy import check_version_binding
from ..profile import CosProfile
from ..redaction import redact_text
from .base import ProviderAdapter

logging.getLogger("qcloud_cos").setLevel(logging.WARNING)


def _cos_error_details(error: Exception) -> tuple[int | None, str | None]:
    def read(name: str):
        method = getattr(error, name, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception:
            return None

    status = read("get_status_code")
    code = read("get_error_code")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status, str(code) if code else None


def _default_client_factory(profile: CosProfile, credentials: CosCredentials):
    try:
        from qcloud_cos import CosConfig, CosS3Client
        from qcloud_cos.cos_comm import maplist
    except ImportError:
        raise PublishError("PROVIDER_UNAVAILABLE", "Tencent COS requires the optional extra: pip install miexiang-generic-publish[cos]") from None
    ClientType = make_no_overwrite_client_type(CosS3Client, maplist)

    config = CosConfig(
        Region=profile.region,
        SecretId=credentials.secret_id,
        SecretKey=credentials.secret_key,
        Token=credentials.session_token,
        Scheme="https",
    )
    return ClientType(config)


def make_no_overwrite_client_type(base_client_type, header_map):
    """Bridge Tencent's forbid-overwrite header into the pinned SDK transfer helper."""
    header_map.setdefault("ForbidOverwrite", "x-cos-forbid-overwrite")

    class NoOverwriteCosClient(base_client_type):
        def __init__(self, config):
            super().__init__(config)
            self._publisher_forbid_overwrite = False

        def upload_file(self, Bucket, Key, LocalFilePath, PartSize=8, MAXThread=4, EnableMD5=False, progress_callback=None, **kwargs):
            if kwargs.get("ForbidOverwrite") not in {True, "true"}:
                raise PublishError("POLICY_REJECTED", "COS upload must carry x-cos-forbid-overwrite=true")
            self._publisher_forbid_overwrite = True
            try:
                return super().upload_file(
                    Bucket=Bucket,
                    Key=Key,
                    LocalFilePath=LocalFilePath,
                    PartSize=PartSize,
                    MAXThread=MAXThread,
                    EnableMD5=EnableMD5,
                    progress_callback=progress_callback,
                    **kwargs,
                )
            finally:
                self._publisher_forbid_overwrite = False

        def complete_multipart_upload(self, *args, **kwargs):
            # The pinned high-level helper forwards request headers to multipart-init but omits
            # them on complete. Re-add the guard at the official SDK call boundary.
            if self._publisher_forbid_overwrite:
                kwargs["ForbidOverwrite"] = "true"
            return super().complete_multipart_upload(*args, **kwargs)

    return NoOverwriteCosClient


class TencentCosAdapter:
    provider = "tencent-cos"
    role = "primary"

    def __init__(
        self,
        profile: CosProfile,
        staging_root: Path,
        credential_reader: Callable[[dict[str, str]], CosCredentials | None] = read_cos_credentials,
        client_factory: Callable[[CosProfile, CosCredentials], Any] = _default_client_factory,
    ):
        self.profile = profile
        self.staging_root = staging_root
        self.credential_reader = credential_reader
        self.client_factory = client_factory
        self._client = None
        self._downloaded_bytes = 0

    def _require_guard_contract(self) -> None:
        if not self.profile.public_write_authorized:
            raise PublishError("AUTH_NOT_READY", "COS public writes are not approved in the profile")
        if not self.profile.versioning_never_enabled or not self.profile.forbid_overwrite_policy_enforced:
            raise PublishError("POLICY_REJECTED", "COS must have versioning never enabled and a policy enforcing x-cos-forbid-overwrite")
        if not self.profile.private_staging_prefix_enforced:
            raise PublishError("POLICY_REJECTED", "COS must keep the private staging prefix outside the public read policy")
        if self.profile.cost_budget_bytes <= 0:
            raise PublishError("POLICY_REJECTED", "COS egress budget is not configured")

    def _get_client(self):
        self._require_guard_contract()
        if self._client is not None:
            return self._client
        credentials = self.credential_reader(self.profile.credential_targets)
        if credentials is None:
            raise PublishError("AUTH_NOT_READY", "COS Credential Manager references are not ready")
        self._client = self.client_factory(self.profile, credentials)
        return self._client

    def object_url(self, key: str) -> str:
        quoted = urllib.parse.quote(key, safe="/-._~")
        return f"https://{self.profile.bucket}.cos.{self.profile.region}.myqcloud.com/{quoted}"

    def _identity_key(self, request: PublicationRequest) -> str:
        base = f"{self.profile.prefix}/" if self.profile.prefix else ""
        return f"{base}releases/{request.product_id}/{request.channel}/{request.version}/identity.json"

    def _artifact_key(self, request: PublicationRequest, artifact: Artifact) -> str:
        base = f"{self.profile.prefix}/" if self.profile.prefix else ""
        return f"{base}releases/{request.product_id}/{request.channel}/{request.version}/{artifact.sha256}/{artifact.filename}"

    def _private_stage_key(self, request: PublicationRequest, artifact: Artifact) -> str:
        base = f"{self.profile.prefix}/" if self.profile.prefix else ""
        return f"{base}private-staging/{request.product_id}/{request.channel}/{request.version}/{artifact.sha256}/{artifact.filename}"

    def _checksums_key(self, request: PublicationRequest, artifact: Artifact) -> str:
        base = f"{self.profile.prefix}/" if self.profile.prefix else ""
        return f"{base}releases/{request.product_id}/{request.channel}/{request.version}/{artifact.sha256}/SHA256SUMS.txt"

    def _head_exists(self, client, key: str) -> bool:
        try:
            client.head_object(Bucket=self.profile.bucket, Key=key)
            return True
        except Exception as error:
            status_code, error_code = _cos_error_details(error)
            if status_code == 404 or error_code in {"NoSuchKey", "NoSuchResource"}:
                return False
            raise PublishError("PROVIDER_UNAVAILABLE", "COS object metadata read failed") from None

    def _published_at(self, client, key: str) -> str:
        try:
            response = client.head_object(Bucket=self.profile.bucket, Key=key)
            value = str(response.get("LastModified") or "")
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except Exception:
            raise PublishError("PROVIDER_UNAVAILABLE", "COS public object modification time could not be read") from None

    def _read_identity(self, client, key: str) -> dict[str, Any] | None:
        try:
            response = client.get_object(Bucket=self.profile.bucket, Key=key)
            body = response["Body"].read(64 * 1024)
            data = json.loads(body.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as error:
            status_code, error_code = _cos_error_details(error)
            if status_code == 404 or error_code in {"NoSuchKey", "NoSuchResource"}:
                return None
            raise PublishError("PROVIDER_UNAVAILABLE", "COS identity read failed") from None

    def _reserve_identity(self, client, request: PublicationRequest, artifact: Artifact) -> tuple[bool, int]:
        key = self._identity_key(request)
        current = self._read_identity(client, key)
        if current is not None:
            check_version_binding(str(current.get("sha256") or ""), artifact.sha256)
            if current.get("filename") != artifact.filename or int(current.get("size_bytes", -1)) != artifact.size_bytes:
                raise PublishError("REMOTE_CONFLICT", "COS version identity metadata differs from this artifact")
            return True, 0
        reservation = {
            "schema": "MIEXIANG_RELEASE_IDENTITY_V1",
            "product_id": request.product_id,
            "release_channel": request.channel,
            "version": request.version,
            "filename": artifact.filename,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "state": "RESERVED",
        }
        try:
            body = json.dumps(reservation, separators=(",", ":")).encode("utf-8")
            client.put_object(
                Bucket=self.profile.bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
                ForbidOverwrite="true",
            )
            return False, len(body)
        except Exception:
            # A concurrent same-hash request may have won. Read back before deciding.
            current = self._read_identity(client, key)
            if current is not None and current.get("sha256") == artifact.sha256:
                return True, 0
            if current is not None:
                raise PublishError("VERSION_HASH_CONFLICT", "COS version identity is already bound to different bytes") from None
            raise PublishError("UPLOAD_FAILED", "COS version reservation failed") from None

    def preflight(self) -> dict[str, Any]:
        client = self._get_client()
        try:
            client.head_bucket(Bucket=self.profile.bucket)
            versioning = client.get_bucket_versioning(Bucket=self.profile.bucket)
        except Exception as error:
            status_code, error_code = _cos_error_details(error)
            if status_code in {401, 403} or error_code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise PublishError("AUTH_NOT_READY", "COS scoped identity lacks required bucket read access") from None
            if status_code == 404 or error_code in {"NoSuchBucket", "NoSuchResource"}:
                raise PublishError("REMOTE_CONFLICT", "configured COS bucket was not found") from None
            raise PublishError("PROVIDER_UNAVAILABLE", "COS bucket preflight could not be completed") from None
        status = str((versioning or {}).get("Status") or "Disabled")
        if status.lower() in {"enabled", "suspended"}:
            raise PublishError("POLICY_REJECTED", "COS versioning disables the required no-overwrite guarantee")
        return {
            "provider": self.provider,
            "auth": "READY",
            "bucket": self.profile.bucket,
            "region": self.profile.region,
            "versioning_status": status,
            "forbid_overwrite_policy_enforced": self.profile.forbid_overwrite_policy_enforced,
            "private_staging_prefix_enforced": self.profile.private_staging_prefix_enforced,
            "cost_budget_bytes": self.profile.cost_budget_bytes,
        }

    def preview_artifact(self, request: PublicationRequest, artifact: Artifact) -> dict[str, Any]:
        readiness = self.preflight()
        identity = self._read_identity(self._get_client(), self._identity_key(request))
        if identity is None:
            state = "NEW"
        elif identity.get("sha256") != artifact.sha256:
            state = "CONFLICT_VERSION_HASH"
        elif identity.get("filename") != artifact.filename or int(identity.get("size_bytes", -1)) != artifact.size_bytes:
            state = "REMOTE_CONFLICT"
        else:
            state = "SAME_HASH"
        return {**readiness, "identity": state}

    def _verify_private_stage(self, client, key: str, artifact: Artifact) -> int:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        destination = self.staging_root / f"cos-private-readback-{artifact.sha256}-{secrets.token_hex(6)}.part"
        body = None
        copied = 0
        try:
            response = client.get_object(Bucket=self.profile.bucket, Key=key)
            body = response["Body"]
            with destination.open("xb") as output:
                while True:
                    block = body.read(min(1024 * 1024, artifact.size_bytes - copied + 1))
                    if not block:
                        break
                    copied += len(block)
                    if copied > artifact.size_bytes:
                        raise PublishError("HASH_MISMATCH", "COS private staging object exceeds the expected size")
                    output.write(block)
                output.flush()
            with destination.open("rb") as staged_file:
                verify_stream(staged_file, artifact.size_bytes, artifact.sha256)
            self._downloaded_bytes += copied
            return copied
        except PublishError:
            raise
        except Exception:
            raise PublishError("PROVIDER_UNAVAILABLE", "COS private staging bytes could not be read safely") from None
        finally:
            destination.unlink(missing_ok=True)
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def publish_artifact(self, request: PublicationRequest, artifact: Artifact) -> dict[str, Any]:
        sums_bytes = encode_sha256_sums(artifact)
        sums_sha256 = hashlib.sha256(sums_bytes).hexdigest()
        self.preflight()
        client = self._get_client()
        key = self._artifact_key(request, artifact)
        stage_key = self._private_stage_key(request, artifact)
        sums_key = self._checksums_key(request, artifact)
        artifact_exists = self._head_exists(client, key)
        artifact_preexisting = artifact_exists
        expected_download = artifact.size_bytes * (1 if artifact_exists else 2) + len(sums_bytes) + 64 * 1024
        if self._downloaded_bytes + expected_download > self.profile.cost_budget_bytes:
            raise PublishError("POLICY_REJECTED", "COS per-run egress budget is too small for stage and consumer verification")
        identity_exists, reservation_bytes = self._reserve_identity(client, request, artifact)
        uploaded_bytes = reservation_bytes
        private_stage_downloaded = 0
        try:
            if not artifact_exists:
                if not self._head_exists(client, stage_key):
                    try:
                        client.upload_file(
                            Bucket=self.profile.bucket,
                            Key=stage_key,
                            LocalFilePath=str(artifact.source_path),
                            PartSize=8,
                            MAXThread=4,
                            ForbidOverwrite="true",
                            Metadata={"x-cos-meta-sha256": artifact.sha256},
                            ContentType=artifact.content_type,
                            ContentDisposition=f'attachment; filename="{artifact.filename}"',
                        )
                        uploaded_bytes += artifact.size_bytes
                    except Exception as error:
                        _, error_code = _cos_error_details(error)
                        if error_code != "FileAlreadyExists":
                            raise
                private_stage_downloaded = self._verify_private_stage(client, stage_key, artifact)
                try:
                    client.copy_object(
                        Bucket=self.profile.bucket,
                        Key=key,
                        CopySource={"Bucket": self.profile.bucket, "Key": stage_key, "Region": self.profile.region},
                        CopyStatus="Replaced",
                        ForbidOverwrite="true",
                        Metadata={"x-cos-meta-sha256": artifact.sha256},
                        ContentType=artifact.content_type,
                        ContentDisposition=f'attachment; filename="{artifact.filename}"',
                    )
                    artifact_exists = True
                except Exception as error:
                    _, error_code = _cos_error_details(error)
                    if error_code == "FileAlreadyExists":
                        # Same-hash concurrent publication may win the immutable final copy.
                        artifact_exists = True
                    else:
                        raise PublishError("UPLOAD_FAILED", "COS public version copy did not complete") from None
            sums_exists = self._head_exists(client, sums_key)
            if not sums_exists:
                try:
                    client.put_object(
                        Bucket=self.profile.bucket,
                        Key=sums_key,
                        Body=sums_bytes,
                        ContentType="text/plain; charset=utf-8",
                        ForbidOverwrite="true",
                    )
                    uploaded_bytes += len(sums_bytes)
                except Exception as error:
                    _, error_code = _cos_error_details(error)
                    if error_code != "FileAlreadyExists":
                        raise
                    sums_exists = True
        except PublishError:
            raise
        except Exception as error:
            raise PublishError("UPLOAD_FAILED", "COS upload did not complete") from None
        url = self.object_url(key)
        destination = self.staging_root / f"cos-readback-{artifact.sha256}.part"
        receipt = download_and_verify(
            url,
            destination,
            artifact.size_bytes,
            artifact.sha256,
            allowed_hosts=("myqcloud.com",),
        )
        destination.unlink(missing_ok=True)
        self._downloaded_bytes += receipt.verification.size_bytes
        sums_url = self.object_url(sums_key)
        sums_destination = self.staging_root / f"cos-sums-readback-{sums_sha256}.part"
        sums_receipt = download_and_verify(
            sums_url,
            sums_destination,
            len(sums_bytes),
            sums_sha256,
            allowed_hosts=("myqcloud.com",),
        )
        sums_destination.unlink(missing_ok=True)
        self._downloaded_bytes += sums_receipt.verification.size_bytes
        return {
            "provider": self.provider,
            "role": self.role,
            "status": "VERIFIED",
            "url": url,
            "published_at": self._published_at(client, key),
            "remote_verified_at": utc_now(),
            "verified_sha256": receipt.verification.sha256,
            "remote_size_bytes": receipt.verification.size_bytes,
            "final_host": receipt.final_host,
            "idempotent": artifact_preexisting and sums_exists and identity_exists,
            "uploaded_bytes": uploaded_bytes,
            "checksums_url": sums_url,
            "checksums_sha256": sums_receipt.verification.sha256,
            "downloaded_bytes": private_stage_downloaded + receipt.verification.size_bytes + sums_receipt.verification.size_bytes,
        }

    def publish_manifest(self, request: PublicationRequest, manifest_bytes: bytes, manifest_sha256: str) -> dict[str, Any]:
        if self._downloaded_bytes + len(manifest_bytes) > self.profile.cost_budget_bytes:
            raise PublishError("POLICY_REJECTED", "COS per-run egress budget does not cover Manifest readback")
        client = self._get_client()
        key = f"{self.profile.prefix}/manifests/{request.product_id}/{request.channel}/{request.version}/{manifest_sha256}/release-manifest.json"
        key = key.lstrip("/")
        uploaded_bytes = 0
        idempotent = self._head_exists(client, key)
        if not idempotent:
            try:
                client.put_object(
                    Bucket=self.profile.bucket,
                    Key=key,
                    Body=manifest_bytes,
                    ContentType="application/json; charset=utf-8",
                    ForbidOverwrite="true",
                )
                uploaded_bytes = len(manifest_bytes)
            except Exception as error:
                _, error_code = _cos_error_details(error)
                if error_code == "FileAlreadyExists":
                    # A concurrent writer may have committed the same content-hash key.
                    idempotent = True
                else:
                    raise PublishError("UPLOAD_FAILED", "COS Manifest write did not complete") from None
        url = self.object_url(key)
        destination = self.staging_root / f"cos-manifest-{manifest_sha256}.part"
        receipt = download_and_verify(url, destination, len(manifest_bytes), manifest_sha256, ("myqcloud.com",))
        destination.unlink(missing_ok=True)
        self._downloaded_bytes += receipt.verification.size_bytes
        return {
            "provider": self.provider,
            "status": "VERIFIED",
            "url": url,
            "remote_verified_at": utc_now(),
            "verified_sha256": receipt.verification.sha256,
            "idempotent": idempotent,
            "remote_size_bytes": receipt.verification.size_bytes,
            "uploaded_bytes": uploaded_bytes,
            "downloaded_bytes": receipt.verification.size_bytes,
        }
