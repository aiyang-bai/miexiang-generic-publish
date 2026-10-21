from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import PublishError
from .models import PublicationRequest
from . import paths
from .profile import PublishProfile, load_profile
from .publisher import GenericPublisher
from .providers.github_release import GitHubReleaseAdapter
from .providers.tencent_cos import TencentCosAdapter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miexiang-publish", description="Publish one fixed artifact and verify consumer bytes.")
    parser.add_argument("--profile", type=Path, help="approved provider profile; defaults to the configured data directory profile")
    parser.add_argument("--data-dir", type=Path, help="runtime data root (overrides MIEXIANG_PUBLISH_DATA_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "publish"):
        command = sub.add_parser(name)
        command.add_argument("--product-id", required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--channel", choices=("test", "candidate", "stable"), required=True)
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--display-name")
        command.add_argument("--release-notes")
    sub.add_parser("offline-demo", help="synthetic local verification; never contacts providers")
    sub.add_parser("auth-status", help="read provider readiness without showing credential values")
    return parser


def _load_optional_profile(path: Path | None) -> PublishProfile | None:
    try:
        return load_profile(path) if path else load_profile()
    except PublishError as error:
        if error.code == "AUTH_NOT_READY":
            return None
        raise


def _publisher(profile: PublishProfile | None) -> GenericPublisher:
    if profile is None:
        return GenericPublisher(None, [])
    providers = []
    if profile.cos is not None:
        providers.append(TencentCosAdapter(profile.cos, paths.STAGING_ROOT))
    if profile.github is not None and profile.mirror_policy != "none":
        providers.append(GitHubReleaseAdapter(profile.github, paths.STAGING_ROOT))
    return GenericPublisher(profile, providers)


def _print(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _public_status(error_code: str) -> str:
    if error_code == "AUTH_NOT_READY":
        return "AUTH_REQUIRED"
    if error_code == "VERSION_HASH_CONFLICT":
        return "CONFLICT_VERSION_HASH"
    if error_code == "HASH_MISMATCH":
        return "HASH_MISMATCH"
    if error_code in {"POLICY_REJECTED", "FILE_TOO_LARGE", "ARTIFACT_INVALID", "MANIFEST_INVALID"}:
        return "POLICY_REJECTED"
    return "FAILED"


def _auth_status(profile: PublishProfile | None) -> dict[str, Any]:
    if profile is None:
        return {"GITHUB_AUTH": "NOT_READY", "COS_AUTH": "NOT_READY", "PROFILE": "NOT_CONFIGURED"}
    result: dict[str, Any] = {"PROFILE": "CONFIGURED", "GITHUB_AUTH": "NOT_CONFIGURED", "COS_AUTH": "NOT_CONFIGURED"}
    publisher = _publisher(profile)
    for adapter in publisher.providers:
        key = "GITHUB_AUTH" if adapter.provider == "github-releases" else "COS_AUTH"
        try:
            state = adapter.preflight()
            result[key] = "READY"
            result.setdefault("PROVIDERS", []).append(state)
        except PublishError as error:
            result[key] = "NOT_READY"
            result.setdefault("PROVIDER_ERRORS", []).append({"provider": adapter.provider, "code": error.code})
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths.configure_runtime(args.data_dir)
        if args.command == "offline-demo":
            from .offline import run_demo
            _print(run_demo())
            return 0
        profile = _load_optional_profile(args.profile)
        if args.command == "auth-status":
            result = _auth_status(profile)
            _print(result)
            return 0 if all(value in {"READY", "NOT_CONFIGURED", "CONFIGURED"} for key, value in result.items() if key.endswith("_AUTH") or key == "PROFILE") else 2
        if args.command == "preview":
            request = PublicationRequest(args.product_id, args.version, args.channel, args.display_name, args.release_notes)
            result = _publisher(profile).preview(request, args.artifact)
            _print(result)
            return 0
        if args.command == "publish":
            if profile is None:
                raise PublishError("AUTH_NOT_READY", "approved provider profile is not configured")
            request = PublicationRequest(args.product_id, args.version, args.channel, args.display_name, args.release_notes)
            result = _publisher(profile).publish(request, args.artifact)
            _print(result)
            return 0 if result.get("PUBLISH_STATUS") in {"PUBLISH_SUCCESS", "PUBLISH_SUCCESS_WITH_OPTIONAL_DEGRADED", "IDEMPOTENT_ALREADY_PUBLISHED"} else 2
        raise PublishError("POLICY_REJECTED", "unsupported command")
    except PublishError as error:
        _print({"PUBLISH_STATUS": _public_status(error.code), "ERROR_CODE": error.code, "MESSAGE": error.message})
        return 2
    except Exception:
        # Provider SDK and operating-system exceptions may contain paths or credentials.
        _print({"PUBLISH_STATUS": "FAILED", "ERROR_CODE": "PROVIDER_UNAVAILABLE", "MESSAGE": "operation failed; inspect bounded provider status"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
