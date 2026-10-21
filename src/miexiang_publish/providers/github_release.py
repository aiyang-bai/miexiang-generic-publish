from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..errors import PublishError
from ..http_verify import download_and_verify
from ..manifest import encode_sha256_sums, same_release_identity, utc_now
from ..models import Artifact, PublicationRequest, hash_file
from ..policy import check_github_size, check_version_binding
from ..profile import GitHubProfile
from ..redaction import redact_text

_RELEASE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _default_runner(args: list[str], **kwargs):
    return subprocess.run(args, shell=False, **kwargs)


class GitHubReleaseAdapter:
    provider = "github-releases"
    role = "mirror"

    def __init__(
        self,
        profile: GitHubProfile,
        staging_root: Path,
        runner: Callable[..., Any] = _default_runner,
    ):
        if not _RELEASE_REPO.fullmatch(profile.repository):
            raise PublishError("POLICY_REJECTED", "GitHub repository must be an explicit owner/repo slug")
        self.profile = profile
        self.repository = profile.repository
        self.staging_root = staging_root
        self.runner = runner
        self._default_branch: str | None = profile.manifest_branch
        self._repo_public = False

    def _run(self, args: list[str], expected: tuple[int, ...] = (0,)) -> str:
        try:
            result = self.runner(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError:
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub CLI could not be started") from None
        if result.returncode not in expected:
            details = redact_text((result.stderr or "")[:800]).lower()
            if "http 404" in details or "not found" in details:
                raise PublishError("REMOTE_CONFLICT", "GitHub release or manifest path was not found")
            if "authentication" in details or "not logged" in details or "http 401" in details or "http 403" in details:
                raise PublishError("AUTH_NOT_READY", "GitHub authentication or repository permission is not ready")
            if "already exists" in details or "http 422" in details:
                raise PublishError("REMOTE_CONFLICT", "GitHub rejected a duplicate immutable identity")
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub operation failed; inspect the bounded provider status")
        return result.stdout or ""

    def _api_json(self, endpoint: str, args: list[str] | None = None) -> Any:
        output = self._run(["api", endpoint, *(args or [])])
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub returned an unreadable API response") from None

    def _tag(self, request: PublicationRequest) -> str:
        return f"{request.channel}-miexiang-generic-publish-{request.product_id}-{request.version}"

    def _release(self, tag: str) -> dict[str, Any] | None:
        try:
            data = self._api_json(f"repos/{self.repository}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        except PublishError as error:
            if error.code == "REMOTE_CONFLICT":
                # GitHub's get-by-tag endpoint does not return drafts. Authenticated
                # release listings do include drafts for users with push access.
                pages = self._api_json(f"repos/{self.repository}/releases", ["--paginate", "--slurp"])
                if not isinstance(pages, list):
                    raise PublishError("PROVIDER_UNAVAILABLE", "GitHub release listing had an invalid shape") from None
                releases: list[dict[str, Any]] = []
                for page in pages:
                    if isinstance(page, list):
                        releases.extend(item for item in page if isinstance(item, dict))
                    elif isinstance(page, dict):
                        releases.append(page)
                matches = [release for release in releases if release.get("tag_name") == tag]
                if len(matches) > 1:
                    raise PublishError("REMOTE_CONFLICT", "multiple GitHub releases reserve the same tag")
                return matches[0] if matches else None
            raise
        if not isinstance(data, dict):
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub release response had an invalid shape")
        return data

    def preflight(self) -> dict[str, Any]:
        if not self.profile.public_write_authorized:
            raise PublishError("AUTH_NOT_READY", "GitHub public test repository is not approved in the profile")
        if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
            raise PublishError("AUTH_NOT_READY", "a GitHub token environment override is present; use the OS credential store")
        try:
            status = self._run(["auth", "status", "--hostname", "github.com"])
        except PublishError:
            raise
        # gh auth status reports its selected credential backend. Do not return or log its raw output.
        if "keyring" not in status.lower():
            raise PublishError("AUTH_NOT_READY", "GitHub authentication is not confirmed in the OS credential store")
        repo = self._api_json(f"repos/{self.repository}")
        if not isinstance(repo, dict):
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub repository response had an invalid shape")
        if repo.get("private") is True:
            raise PublishError("POLICY_REJECTED", "the selected GitHub release repository must be public for consumer verification")
        permissions = repo.get("permissions") or {}
        if permissions.get("push") is not True:
            raise PublishError("AUTH_NOT_READY", "the selected GitHub account cannot publish to the configured repository")
        self._repo_public = True
        if not self._default_branch:
            self._default_branch = str(repo.get("default_branch") or "")
        return {
            "provider": self.provider,
            "auth": "READY",
            "credential_backend": "OS_KEYRING",
            "repository": self.repository,
            "repository_public": True,
            "default_branch": self._default_branch,
        }

    def preview_artifact(self, request: PublicationRequest, artifact: Artifact) -> dict[str, Any]:
        check_github_size(artifact.size_bytes)
        readiness = self.preflight()
        release = self._release(self._tag(request))
        if release is None:
            identity = "NEW"
        elif release.get("draft") is True:
            sums_bytes = encode_sha256_sums(artifact)
            sums_sha256 = hashlib.sha256(sums_bytes).hexdigest()
            try:
                self._verify_draft_assets(release, artifact, sums_bytes, sums_sha256)
            except PublishError as error:
                identity = "CONFLICT_VERSION_HASH" if error.code == "VERSION_HASH_CONFLICT" else "DRAFT_RESERVED"
            else:
                identity = "DRAFT_RESUMABLE"
        else:
            asset = self._asset_by_name(release, artifact.filename)
            if asset is None:
                identity = "REMOTE_CONFLICT"
            else:
                digest = str(asset.get("digest") or "")
                remote_digest = digest.partition(":")[2] if digest.startswith("sha256:") else None
                if remote_digest:
                    identity = "SAME_HASH" if remote_digest.lower() == artifact.sha256 else "CONFLICT_VERSION_HASH"
                else:
                    identity = "VERIFY_ON_PUBLISH" if int(asset.get("size", -1)) == artifact.size_bytes else "CONFLICT_VERSION_HASH"
        return {**readiness, "identity": identity}

    def _asset_by_name(self, release: dict[str, Any], name: str) -> dict[str, Any] | None:
        return next((asset for asset in release.get("assets", []) if asset.get("name") == name), None)

    def _verify_draft_assets(self, draft: dict[str, Any], artifact: Artifact, sums_bytes: bytes, sums_sha256: str) -> None:
        expected_names = {artifact.filename, "SHA256SUMS.txt"}
        assets = draft.get("assets", [])
        names = [str(asset.get("name") or "") for asset in assets]
        if draft.get("draft") is not True or len(names) != len(expected_names) or set(names) != expected_names:
            raise PublishError("REMOTE_CONFLICT", "draft contains missing or unexpected GitHub release assets")

        asset = self._asset_by_name(draft, artifact.filename)
        if asset is None:
            raise PublishError("REMOTE_CONFLICT", "draft does not contain the expected GitHub release asset")
        digest = str(asset.get("digest") or "")
        remote_digest = digest.partition(":")[2] if digest.startswith("sha256:") else None
        if remote_digest:
            check_version_binding(remote_digest, artifact.sha256)
        elif int(asset.get("size", -1)) != artifact.size_bytes:
            raise PublishError("VERSION_HASH_CONFLICT", "draft asset size differs from the requested artifact")

        sums_asset = self._asset_by_name(draft, "SHA256SUMS.txt")
        sums_digest = str((sums_asset or {}).get("digest") or "")
        if sums_asset is None:
            raise PublishError("REMOTE_CONFLICT", "draft does not contain SHA256SUMS.txt")
        if sums_digest.startswith("sha256:"):
            check_version_binding(sums_digest.partition(":")[2], sums_sha256)
        elif int(sums_asset.get("size", -1)) != len(sums_bytes):
            raise PublishError("VERSION_HASH_CONFLICT", "draft checksum asset size differs from its contents")

    def _verify_release_asset(self, release: dict[str, Any], artifact: Artifact, idempotent: bool) -> dict[str, Any]:
        published_at = str(release.get("published_at") or "")
        if not published_at:
            raise PublishError("PROVIDER_UNAVAILABLE", "GitHub release publication time could not be read")
        asset = self._asset_by_name(release, artifact.filename)
        if asset is None:
            raise PublishError("REMOTE_CONFLICT", "release tag already exists without the expected asset")
        digest = str(asset.get("digest") or "")
        remote_digest = digest.partition(":")[2] if digest.startswith("sha256:") else None
        if remote_digest:
            check_version_binding(remote_digest, artifact.sha256)
        elif int(asset.get("size", -1)) != artifact.size_bytes:
            raise PublishError("VERSION_HASH_CONFLICT", "existing GitHub asset size differs from the requested artifact")
        url = str(asset.get("browser_download_url") or "")
        if not url:
            raise PublishError("DOWNLOAD_FAILED", "GitHub did not return a consumer download URL")
        destination = self.staging_root / f"github-readback-{artifact.sha256}.part"
        receipt = download_and_verify(
            url,
            destination,
            artifact.size_bytes,
            artifact.sha256,
            allowed_hosts=("github.com", "githubusercontent.com"),
        )
        destination.unlink(missing_ok=True)
        sums_bytes = encode_sha256_sums(artifact)
        sums_sha256 = hashlib.sha256(sums_bytes).hexdigest()
        sums_asset = self._asset_by_name(release, "SHA256SUMS.txt")
        if sums_asset is None:
            raise PublishError("REMOTE_CONFLICT", "release does not contain the required SHA256SUMS.txt asset")
        sums_digest = str(sums_asset.get("digest") or "")
        if sums_digest.startswith("sha256:"):
            check_version_binding(sums_digest.partition(":")[2], sums_sha256)
        elif int(sums_asset.get("size", -1)) != len(sums_bytes):
            raise PublishError("VERSION_HASH_CONFLICT", "existing GitHub checksum asset size differs from the requested artifact")
        sums_url = str(sums_asset.get("browser_download_url") or "")
        if not sums_url:
            raise PublishError("DOWNLOAD_FAILED", "GitHub did not return a checksum file URL")
        sums_destination = self.staging_root / f"github-sums-readback-{sums_sha256}.part"
        sums_receipt = download_and_verify(
            sums_url,
            sums_destination,
            len(sums_bytes),
            sums_sha256,
            allowed_hosts=("github.com", "githubusercontent.com"),
        )
        sums_destination.unlink(missing_ok=True)
        return {
            "provider": self.provider,
            "role": self.role,
            "status": "VERIFIED",
            "url": url,
            "published_at": published_at,
            "remote_verified_at": utc_now(),
            "verified_sha256": receipt.verification.sha256,
            "remote_size_bytes": receipt.verification.size_bytes,
            "final_host": receipt.final_host,
            "immutable_enforced": release.get("immutable") is True,
            "idempotent": idempotent,
            "checksums_url": sums_url,
            "checksums_sha256": sums_receipt.verification.sha256,
            "uploaded_bytes": 0 if idempotent else artifact.size_bytes + len(sums_bytes),
            "downloaded_bytes": receipt.verification.size_bytes + sums_receipt.verification.size_bytes,
        }

    def publish_artifact(self, request: PublicationRequest, artifact: Artifact) -> dict[str, Any]:
        check_github_size(artifact.size_bytes)
        self.preflight()
        tag = self._tag(request)
        sums_bytes = encode_sha256_sums(artifact)
        sums_sha256 = hashlib.sha256(sums_bytes).hexdigest()
        existing = self._release(tag)
        if existing is not None and existing.get("draft") is not True:
            return self._verify_release_asset(existing, artifact, idempotent=True)

        if existing is not None:
            # Resume only an exact draft already bound to these bytes; never add or replace assets.
            draft = existing
            self._verify_draft_assets(draft, artifact, sums_bytes, sums_sha256)
        else:
            notes = redact_text(request.release_notes or f"{request.product_id} {request.version} ({request.channel} release).")
            title = f"{request.product_id} {request.version} ({request.channel} release artifact)"
            create_args = ["release", "create", tag, "--draft", "--title", title, "--notes", notes, "--latest=false", "--repo", self.repository]
            if request.channel in {"test", "candidate"}:
                create_args.append("--prerelease")
            self._run(create_args)
            # gh release upload uses the local basename, so name and verify a disposable copy first.
            # The upload still runs without --clobber; an existing release asset is never replaced.
            self.staging_root.mkdir(parents=True, exist_ok=True)
            upload_dir = Path(tempfile.mkdtemp(prefix="github-upload-", dir=self.staging_root))
            upload_path = upload_dir / artifact.filename
            sums_path = upload_dir / "SHA256SUMS.txt"
            try:
                try:
                    shutil.copyfile(artifact.source_path, upload_path)
                except OSError:
                    raise PublishError("ARTIFACT_INVALID", "staged artifact could not be prepared for GitHub upload") from None
                upload_identity = hash_file(upload_path)
                if upload_identity.size_bytes != artifact.size_bytes or upload_identity.sha256 != artifact.sha256:
                    raise PublishError("HASH_MISMATCH", "named GitHub upload copy differs from the verified artifact")
                sums_path.write_bytes(sums_bytes)
                self._run(["release", "upload", tag, str(upload_path), str(sums_path), "--repo", self.repository])
            finally:
                upload_path.unlink(missing_ok=True)
                sums_path.unlink(missing_ok=True)
                upload_dir.rmdir()
            draft = self._release(tag)
            if draft is None:
                raise PublishError("UPLOAD_FAILED", "GitHub draft state could not be read back")
            self._verify_draft_assets(draft, artifact, sums_bytes, sums_sha256)

        # Publishing is a separate, explicit commit after the draft digest matches.
        self._run(["release", "edit", tag, "--draft=false", "--latest=false", "--repo", self.repository])
        published = self._release(tag)
        if published is None or published.get("draft") is True:
            raise PublishError("UPLOAD_FAILED", "GitHub release did not become public")
        return self._verify_release_asset(published, artifact, idempotent=False)

    def read_manifest(self, request: PublicationRequest) -> bytes | None:
        if not self._default_branch:
            self.preflight()
        path = f"manifests/{request.product_id}/{request.channel}/{request.version}/release-manifest.json"
        query = urllib.parse.urlencode({"ref": self._default_branch})
        try:
            current = self._api_json(f"repos/{self.repository}/contents/{path}?{query}")
        except PublishError as error:
            if error.code == "REMOTE_CONFLICT":
                return None
            raise
        if not isinstance(current, dict):
            raise PublishError("REMOTE_CONFLICT", "existing GitHub manifest has an invalid response shape")
        try:
            encoded = current.get("content", "")
            if not isinstance(encoded, str) or not encoded.strip():
                raise ValueError("content is absent")
            return base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, TypeError):
            raise PublishError("REMOTE_CONFLICT", "existing GitHub manifest cannot be read safely") from None

    def publish_manifest(self, request: PublicationRequest, manifest_bytes: bytes, manifest_sha256: str) -> dict[str, Any]:
        if not self.profile.public_write_authorized:
            raise PublishError("AUTH_NOT_READY", "GitHub manifest repository is not approved in the profile")
        if not self._default_branch:
            self.preflight()
        path = f"manifests/{request.product_id}/{request.channel}/{request.version}/release-manifest.json"
        endpoint = f"repos/{self.repository}/contents/{path}"
        current_bytes = self.read_manifest(request)
        if current_bytes is not None:
            # A versioned manifest path is create-only. Never call the update form of the API.
            if not same_release_identity(current_bytes, manifest_bytes):
                raise PublishError("REMOTE_CONFLICT", "a different manifest already occupies this version path")
            remote_manifest_sha256 = hashlib.sha256(current_bytes).hexdigest()
            query = urllib.parse.urlencode({"path": path, "per_page": "1", "sha": self._default_branch})
            commits = self._api_json(f"repos/{self.repository}/commits?{query}")
            if not isinstance(commits, list) or not commits or not commits[0].get("sha"):
                raise PublishError("PROVIDER_UNAVAILABLE", "GitHub manifest commit identity could not be read")
            commit_sha = str(commits[0]["sha"])
        else:
            remote_manifest_sha256 = manifest_sha256
            body = base64.b64encode(manifest_bytes).decode("ascii")
            identity = self._api_json("user", ["--jq", "{login,id}"])
            login = identity.get("login") if isinstance(identity, dict) else None
            user_id = identity.get("id") if isinstance(identity, dict) else None
            if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) or not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
                raise PublishError("AUTH_NOT_READY", "public GitHub commit identity could not be established")
            noreply = f"{user_id}+{login}@users.noreply.github.com"
            response = self._api_json(
                endpoint,
                [
                    "--method", "PUT",
                    "--field", f"message=Add {request.product_id} {request.version} release manifest",
                    "--field", f"content={body}",
                    "--field", f"branch={self._default_branch}",
                    "--field", f"author[name]={login}",
                    "--field", f"author[email]={noreply}",
                    "--field", f"committer[name]={login}",
                    "--field", f"committer[email]={noreply}",
                ],
            )
            commit_sha = str((response.get("commit") or {}).get("sha") or "")
            if not commit_sha:
                raise PublishError("UPLOAD_FAILED", "GitHub did not return the manifest commit identity")
        raw_url = f"https://raw.githubusercontent.com/{self.repository}/{commit_sha}/{urllib.parse.quote(path, safe='/')}"
        expected_bytes = current_bytes if current_bytes is not None else manifest_bytes
        destination = self.staging_root / f"github-manifest-{remote_manifest_sha256}.part"
        receipt = download_and_verify(raw_url, destination, len(expected_bytes), remote_manifest_sha256, ("githubusercontent.com",))
        destination.unlink(missing_ok=True)
        return {
            "provider": self.provider,
            "status": "VERIFIED",
            "url": raw_url,
            "remote_verified_at": utc_now(),
            "verified_sha256": receipt.verification.sha256,
            "remote_size_bytes": receipt.verification.size_bytes,
            "commit_sha": commit_sha,
            "idempotent": current_bytes is not None,
            "uploaded_bytes": len(manifest_bytes) if current_bytes is None else 0,
            "downloaded_bytes": receipt.verification.size_bytes,
        }
