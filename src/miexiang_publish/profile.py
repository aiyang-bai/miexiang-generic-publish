from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PublishError
from . import paths

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}-[0-9]{6,14}$")
_REGION = re.compile(r"^[a-z]{2}-[a-z]+(?:-[a-z0-9]+)*$")
_PREFIX = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")


@dataclass(frozen=True)
class GitHubProfile:
    repository: str
    public_write_authorized: bool
    manifest_branch: str | None = None


@dataclass(frozen=True)
class CosProfile:
    bucket: str
    region: str
    prefix: str
    public_write_authorized: bool
    versioning_never_enabled: bool
    forbid_overwrite_policy_enforced: bool
    private_staging_prefix_enforced: bool
    credential_targets: dict[str, str]
    cost_budget_bytes: int


@dataclass(frozen=True)
class PublishProfile:
    mirror_policy: str
    allowed_channels: frozenset[str]
    github: GitHubProfile | None
    cos: CosProfile | None


def load_profile(path: Path | None = None) -> PublishProfile:
    try:
        data: dict[str, Any] = json.loads((path if path is not None else paths.PROFILE_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError("AUTH_NOT_READY", "approved provider profile is not configured") from None
    except (OSError, json.JSONDecodeError):
        raise PublishError("POLICY_REJECTED", "provider profile cannot be read") from None
    if data.get("profile_version") != 1 or data.get("provider_policy") != "cn-software-v1":
        raise PublishError("POLICY_REJECTED", "unsupported provider profile")
    mirror_policy = data.get("mirror_policy", "required")
    if mirror_policy not in {"required", "optional", "none"}:
        raise PublishError("POLICY_REJECTED", "mirror_policy is invalid")
    allowed_channels = data.get("allowed_channels", ["test"])
    if not isinstance(allowed_channels, list) or not allowed_channels or any(channel not in {"test", "candidate", "stable"} for channel in allowed_channels):
        raise PublishError("POLICY_REJECTED", "allowed_channels must be a non-empty subset of test, candidate, and stable")

    github_data = data.get("github")
    github = None
    if github_data:
        repository = str(github_data.get("repository", ""))
        if not _REPOSITORY.fullmatch(repository):
            raise PublishError("POLICY_REJECTED", "GitHub repository must be an explicit owner/repo slug")
        github = GitHubProfile(
            repository=repository,
            public_write_authorized=github_data.get("public_write_authorized") is True,
            manifest_branch=github_data.get("manifest_branch"),
        )

    cos_data = data.get("cos")
    cos = None
    if cos_data:
        bucket = str(cos_data.get("bucket", ""))
        region = str(cos_data.get("region", ""))
        prefix = str(cos_data.get("prefix", ""))
        if not _BUCKET.fullmatch(bucket) or not _REGION.fullmatch(region):
            raise PublishError("POLICY_REJECTED", "COS bucket or region is invalid")
        if prefix and (not _PREFIX.fullmatch(prefix) or prefix.startswith("/") or any(part in {"", ".", ".."} for part in prefix.split("/"))):
            raise PublishError("POLICY_REJECTED", "COS prefix must stay within the configured bucket namespace")
        targets = cos_data.get("credential_targets", {})
        if not isinstance(targets, dict) or not {"secret_id", "secret_key"}.issubset(targets):
            raise PublishError("POLICY_REJECTED", "COS Credential Manager target references are incomplete")
        if any(
            not isinstance(value, str)
            or not value.startswith("MiexiangGenericPublish/COS/")
            or len(value) <= len("MiexiangGenericPublish/COS/")
            for value in targets.values()
        ):
            raise PublishError("POLICY_REJECTED", "COS credentials must use the dedicated Credential Manager target namespace")
        cos = CosProfile(
            bucket=bucket,
            region=region,
            prefix=prefix.strip("/"),
            public_write_authorized=cos_data.get("public_write_authorized") is True,
            versioning_never_enabled=cos_data.get("versioning_never_enabled") is True,
            forbid_overwrite_policy_enforced=cos_data.get("forbid_overwrite_policy_enforced") is True,
            private_staging_prefix_enforced=cos_data.get("private_staging_prefix_enforced") is True,
            credential_targets={str(k): str(v) for k, v in targets.items()},
            cost_budget_bytes=int(cos_data.get("cost_budget_bytes", 0)),
        )
        if cos.cost_budget_bytes <= 0:
            raise PublishError("POLICY_REJECTED", "COS egress budget must be explicitly configured")
    return PublishProfile(mirror_policy=mirror_policy, allowed_channels=frozenset(allowed_channels), github=github, cos=cos)
