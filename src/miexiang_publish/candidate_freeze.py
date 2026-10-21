from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


DIGEST_SCHEMA = "MIEXIANG_SOURCE_TREE_DIGEST"
DIGEST_VERSION = 1
IDENTITY_SCHEMA = "MIEXIANG_GENERIC_PUBLISH_CANDIDATE_IDENTITY_V1"
SKILL_PATH = "skill/miexiang-generic-publish/SKILL.md"
REQUIREMENTS_LOCK_PATH = "requirements.lock"
SELECTION = {
    "DIGEST_SCHEMA": DIGEST_SCHEMA,
    "DIGEST_VERSION": DIGEST_VERSION,
    "FILE_ROOT": "Git tree at IMPLEMENTATION_COMMIT",
    "INCLUDED_PATHS": "Every Git blob with mode 100644, 100755, or 120000; no extension/path filters",
    "EXCLUDED_PATHS": ".git metadata; untracked and ignored files (the tracked worktree must be clean); gitlinks and unknown modes are rejected",
    "SORT_ORDER": "Ascending raw UTF-8 path bytes",
    "PATH_NORMALIZATION": "Strict UTF-8 Git paths with forward slashes; no case folding or Unicode normalization",
    "HASH_ALGORITHM": "SHA-256",
    "RECORD_FORMAT": "path_utf8 NUL mode_ascii NUL size_ascii NUL sha256_ascii LF; no terminal NUL",
    "DIRECTORY_HASH_INPUT": "Concatenation of the sorted record bytes",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class CandidateFreezeError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, shell=False, capture_output=True,
            text=not binary, encoding=None if binary else "utf-8", errors=None if binary else "strict",
        )
    except (OSError, UnicodeError) as error:
        raise CandidateFreezeError(f"Git command could not be completed: {args[0]}") from error
    if result.returncode:
        raise CandidateFreezeError(f"Git command failed: {args[0]}")
    return result.stdout if binary else result.stdout.strip()


def _clean_status(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise CandidateFreezeError("formal candidate freeze requires a clean tracked and untracked worktree")


def _path_text(path_bytes: bytes) -> str:
    try:
        path = path_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CandidateFreezeError("Git tree contains a non-UTF-8 path") from error
    components = path.split("/")
    if not path or "\\" in path or any(part in {"", ".", ".."} for part in components):
        raise CandidateFreezeError("Git tree contains a non-canonical path")
    return path


def _tree_entries(repo: Path, commit: str) -> list[dict[str, Any]]:
    output = _git(repo, "ls-tree", "-rz", "--full-tree", commit, binary=True)
    assert isinstance(output, bytes)
    entries: list[tuple[bytes, str, str]] = []
    for raw_record in output.split(b"\0"):
        if not raw_record:
            continue
        try:
            header, path_bytes = raw_record.split(b"\t", 1)
            mode_bytes, kind, object_id = header.split(b" ", 2)
        except ValueError as error:
            raise CandidateFreezeError("Git returned a malformed tree record") from error
        mode = mode_bytes.decode("ascii")
        path = _path_text(path_bytes)
        if kind != b"blob" or mode not in {"100644", "100755", "120000"}:
            raise CandidateFreezeError(f"unsupported Git tree entry at {path}")
        entries.append((path_bytes, mode, object_id.decode("ascii")))

    entries.sort(key=lambda item: item[0])
    result: list[dict[str, Any]] = []
    for path_bytes, mode, object_id in entries:
        blob = _git(repo, "cat-file", "blob", object_id, binary=True)
        assert isinstance(blob, bytes)
        result.append({
            "path": _path_text(path_bytes),
            "mode": mode,
            "size_bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        })
    return result


def _source_manifest(commit: str, entries: list[dict[str, Any]]) -> bytes:
    value = {
        "schema": "MIEXIANG_GIT_SOURCE_MANIFEST_V1",
        "implementation_commit": commit,
        "digest_schema": DIGEST_SCHEMA,
        "digest_version": DIGEST_VERSION,
        "selection": SELECTION,
        "entries": entries,
    }
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _directory_digest(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        record = (
            entry["path"].encode("utf-8") + b"\0"
            + entry["mode"].encode("ascii") + b"\0"
            + str(entry["size_bytes"]).encode("ascii") + b"\0"
            + entry["sha256"].encode("ascii") + b"\n"
        )
        digest.update(record)
    return digest.hexdigest()


def _validate_e2e_summary(path: Path, commit: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        summary = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateFreezeError("e2e-summary.json is missing or invalid") from error
    required_passes = (
        "FRESH_GITHUB_RELEASE_E2E",
        "SAME_WORKSPACE_IDEMPOTENCY",
        "CLEAN_WORKSPACE_IDEMPOTENCY",
        "CLEAN_R2_REPLAY",
        "CLEAN_FRESH_RELEASE_REPLAY",
        "REMOTE_HASH_VERIFY",
        "VERSION_HASH_CONFLICT",
        "TEST_SUITE",
    )
    if not isinstance(summary, dict) or summary.get("E2E_COMMIT") != commit:
        raise CandidateFreezeError("E2E commit does not equal the candidate implementation commit")
    if any(summary.get(key) != "PASS" for key in required_passes):
        raise CandidateFreezeError("E2E summary does not contain every required PASS")
    try:
        real_provider_count = int(summary.get("REAL_PROVIDER_E2E_COUNT", 0))
    except (TypeError, ValueError):
        real_provider_count = 0
    if real_provider_count < 1:
        raise CandidateFreezeError("at least one real provider E2E is required")
    if summary.get("GITHUB_PUBLIC_REPO") != "example/release-fixture":
        raise CandidateFreezeError("E2E summary does not identify the approved public test repository")
    return summary, raw


def _evidence_inventory(evidence_dir: Path) -> tuple[list[dict[str, Any]], bytes]:
    if not evidence_dir.is_dir():
        raise CandidateFreezeError("evidence directory does not exist")
    files: list[Path] = []
    for path in evidence_dir.rglob("*"):
        if path.is_symlink():
            raise CandidateFreezeError("evidence bundle cannot contain symbolic links")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise CandidateFreezeError("evidence bundle contains a non-regular entry")
    entries: list[dict[str, Any]] = []
    for path in sorted(files, key=lambda item: item.relative_to(evidence_dir).as_posix().encode("utf-8")):
        relative = path.relative_to(evidence_dir).as_posix()
        content = path.read_bytes()
        entries.append({"path": relative, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    raw = (json.dumps({"schema": "MIEXIANG_CANDIDATE_EVIDENCE_INVENTORY_V1", "entries": entries}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return entries, raw


def _identity_fingerprint(identity: dict[str, Any]) -> str:
    body = {key: value for key, value in identity.items() if key not in {"IDENTITY_SHA256", "CANDIDATE_IDENTITY_SELF_CHECK"}}
    encoded = (json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_candidate(repo_root: str | Path, output_dir: str | Path, evidence_dir: str | Path) -> dict[str, Any]:
    repo = Path(_git(Path(repo_root).resolve(), "rev-parse", "--show-toplevel")).resolve()
    output = Path(output_dir).resolve()
    evidence = Path(evidence_dir).resolve()
    if output.exists():
        raise CandidateFreezeError("candidate output directory already exists; freeze is create-only")
    if os.path.commonpath([str(repo), str(output)]) == str(repo):
        raise CandidateFreezeError("candidate output must be outside the source repository")

    _clean_status(repo)
    branch = str(_git(repo, "branch", "--show-current"))
    commit = str(_git(repo, "rev-parse", "HEAD"))
    if not branch or not _COMMIT.fullmatch(commit):
        raise CandidateFreezeError("candidate requires a named branch and full 40-character Git commit")
    entries = _tree_entries(repo, commit)
    entry_by_path = {entry["path"]: entry for entry in entries}
    if SKILL_PATH not in entry_by_path or REQUIREMENTS_LOCK_PATH not in entry_by_path:
        raise CandidateFreezeError("candidate source tree is missing the Skill or requirements.lock")

    evidence_summary = evidence / "e2e-summary.json"
    _, summary_bytes = _validate_e2e_summary(evidence_summary, commit)
    inventory_entries, inventory_bytes = _evidence_inventory(evidence)
    stage = output.parent / f".{output.name}.tmp-{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        source_manifest_bytes = _source_manifest(commit, entries)
        source_manifest_sha256 = hashlib.sha256(source_manifest_bytes).hexdigest()
        (stage / "source-manifest.json").write_bytes(source_manifest_bytes)
        (stage / "evidence-inventory.json").write_bytes(inventory_bytes)
        copied_evidence = stage / "evidence"
        shutil.copytree(evidence, copied_evidence)
        if (copied_evidence / "e2e-summary.json").read_bytes() != summary_bytes:
            raise CandidateFreezeError("E2E evidence changed while the candidate was being frozen")

        evidence_inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
        directory_digest = _directory_digest(entries)
        id_material = "\n".join((IDENTITY_SCHEMA, branch, commit, source_manifest_sha256, evidence_inventory_sha256)).encode("utf-8")
        candidate_id = f"MIEXIANG-GENERIC-PUBLISH-FIX01-{hashlib.sha256(id_material).hexdigest()[:24]}"
        identity: dict[str, Any] = {
            "IDENTITY_SCHEMA": IDENTITY_SCHEMA,
            "CANDIDATE_ID": candidate_id,
            "BRANCH": branch,
            "IMPLEMENTATION_COMMIT": commit,
            "FULL_40_CHAR_HASH": commit,
            "WORKTREE_STATUS": "CLEAN",
            "SOURCE_MANIFEST_PATH": "source-manifest.json",
            "SOURCE_MANIFEST_SHA256": source_manifest_sha256,
            "SKILL_PATH": SKILL_PATH,
            "SKILL_SHA256": entry_by_path[SKILL_PATH]["sha256"],
            "REQUIREMENTS_LOCK_PATH": REQUIREMENTS_LOCK_PATH,
            "REQUIREMENTS_LOCK_SHA256": entry_by_path[REQUIREMENTS_LOCK_PATH]["sha256"],
            "DIRECTORY_DIGEST": directory_digest,
            **SELECTION,
            "E2E_COMMIT": str(_validate_e2e_summary(evidence_summary, commit)[0]["E2E_COMMIT"]),
            "E2E_SUMMARY_SHA256": hashlib.sha256(summary_bytes).hexdigest(),
            "EVIDENCE_INVENTORY_SHA256": evidence_inventory_sha256,
        }
        identity["IDENTITY_SHA256"] = _identity_fingerprint(identity)
        identity["CANDIDATE_IDENTITY_SELF_CHECK"] = "PASS"
        raw_identity = (json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        (stage / "candidate-identity.json").write_bytes(raw_identity)

        # Verify all generated bindings before making the frozen directory visible.
        if hashlib.sha256((stage / "source-manifest.json").read_bytes()).hexdigest() != identity["SOURCE_MANIFEST_SHA256"]:
            raise CandidateFreezeError("source manifest self-check failed")
        if _directory_digest(entries) != identity["DIRECTORY_DIGEST"] or identity["IDENTITY_SHA256"] != _identity_fingerprint(identity):
            raise CandidateFreezeError("candidate identity self-check failed")
        _clean_status(repo)
        os.replace(stage, output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze a clean Git candidate with automated source and E2E identity bindings.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    try:
        identity = freeze_candidate(args.repo_root, args.output_dir, args.evidence_dir)
    except (CandidateFreezeError, OSError) as error:
        print(json.dumps({"FREEZE_STATUS": "REJECTED", "ERROR": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"FREEZE_STATUS": "FROZEN", **identity}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
