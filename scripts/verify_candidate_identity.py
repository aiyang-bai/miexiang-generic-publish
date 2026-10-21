from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


DIGEST_SCHEMA = "MIEXIANG_SOURCE_TREE_DIGEST"
DIGEST_VERSION = 1
IDENTITY_SCHEMA = "MIEXIANG_GENERIC_PUBLISH_CANDIDATE_IDENTITY_V1"
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
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class VerificationError(RuntimeError):
    pass


def git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, shell=False, capture_output=True,
            text=not binary, encoding=None if binary else "utf-8", errors=None if binary else "strict",
        )
    except (OSError, UnicodeError) as error:
        raise VerificationError(f"Git command could not be completed: {args[0]}") from error
    if result.returncode:
        raise VerificationError(f"Git command failed: {args[0]}")
    return result.stdout if binary else result.stdout.strip()


def canonical_tree(repo: Path, commit: str) -> list[dict[str, object]]:
    raw_tree = git(repo, "ls-tree", "-rz", "--full-tree", commit, binary=True)
    assert isinstance(raw_tree, bytes)
    parsed: list[tuple[bytes, str, str]] = []
    for row in raw_tree.split(b"\0"):
        if not row:
            continue
        try:
            header, path = row.split(b"\t", 1)
            mode_raw, kind, oid_raw = header.split(b" ", 2)
            path_text = path.decode("utf-8", "strict")
            mode = mode_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise VerificationError("Git tree contains a malformed or non-UTF-8 path") from error
        if not path_text or "\\" in path_text or any(part in {"", ".", ".."} for part in path_text.split("/")):
            raise VerificationError("Git tree contains a non-canonical path")
        if kind != b"blob" or mode not in {"100644", "100755", "120000"}:
            raise VerificationError(f"Git tree entry is not an allowed blob: {path_text}")
        parsed.append((path, mode, oid))
    parsed.sort(key=lambda item: item[0])

    entries: list[dict[str, object]] = []
    for path_bytes, mode, oid in parsed:
        blob = git(repo, "cat-file", "blob", oid, binary=True)
        assert isinstance(blob, bytes)
        entries.append({
            "path": path_bytes.decode("utf-8", "strict"),
            "mode": mode,
            "size_bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
        })
    return entries


def directory_digest(entries: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        path = str(entry["path"]).encode("utf-8")
        mode = str(entry["mode"]).encode("ascii")
        size = str(entry["size_bytes"]).encode("ascii")
        file_hash = str(entry["sha256"]).encode("ascii")
        digest.update(path + b"\0" + mode + b"\0" + size + b"\0" + file_hash + b"\n")
    return digest.hexdigest()


def encode_source_manifest(commit: str, entries: list[dict[str, object]]) -> bytes:
    value = {
        "schema": "MIEXIANG_GIT_SOURCE_MANIFEST_V1",
        "implementation_commit": commit,
        "digest_schema": DIGEST_SCHEMA,
        "digest_version": DIGEST_VERSION,
        "selection": SELECTION,
        "entries": entries,
    }
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def fingerprint(identity: dict[str, object]) -> str:
    unsigned = {key: value for key, value in identity.items() if key not in {"IDENTITY_SHA256", "CANDIDATE_IDENTITY_SELF_CHECK"}}
    payload = (json.dumps(unsigned, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify(repo_root: str | Path, candidate_dir: str | Path) -> dict[str, object]:
    repo = Path(git(Path(repo_root).resolve(), "rev-parse", "--show-toplevel")).resolve()
    candidate = Path(candidate_dir).resolve()
    if not candidate.is_dir() or os.path.commonpath([str(repo), str(candidate)]) == str(repo):
        raise VerificationError("candidate must exist outside the source repository")
    try:
        identity = json.loads((candidate / "candidate-identity.json").read_text(encoding="utf-8"))
        declared_source = (candidate / "source-manifest.json").read_bytes()
        inventory_bytes = (candidate / "evidence-inventory.json").read_bytes()
        inventory = json.loads(inventory_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError("candidate identity, source manifest, or inventory is unreadable") from error
    if not isinstance(identity, dict) or identity.get("IDENTITY_SCHEMA") != IDENTITY_SCHEMA:
        raise VerificationError("candidate identity schema is unsupported")

    status = str(git(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    branch = str(git(repo, "branch", "--show-current"))
    commit = str(git(repo, "rev-parse", "HEAD"))
    if status or identity.get("WORKTREE_STATUS") != "CLEAN":
        raise VerificationError("source worktree is not clean")
    if not COMMIT_RE.fullmatch(commit) or identity.get("BRANCH") != branch or identity.get("IMPLEMENTATION_COMMIT") != commit or identity.get("FULL_40_CHAR_HASH") != commit:
        raise VerificationError("candidate branch or full commit binding differs from the current source")

    entries = canonical_tree(repo, commit)
    entry_map = {str(row["path"]): row for row in entries}
    source_bytes = encode_source_manifest(commit, entries)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    digest = directory_digest(entries)
    if declared_source != source_bytes or identity.get("SOURCE_MANIFEST_SHA256") != source_hash:
        raise VerificationError("source manifest differs from the independently enumerated Git tree")
    if identity.get("DIRECTORY_DIGEST") != digest:
        raise VerificationError("directory digest differs from the independent recalculation")
    for key, value in SELECTION.items():
        if identity.get(key) != value:
            raise VerificationError(f"candidate digest rule differs at {key}")
    skill = entry_map.get("skill/miexiang-generic-publish/SKILL.md")
    lock = entry_map.get("requirements.lock")
    if not skill or not lock or identity.get("SKILL_SHA256") != skill["sha256"] or identity.get("REQUIREMENTS_LOCK_SHA256") != lock["sha256"]:
        raise VerificationError("Skill or requirements lock hash differs from the Git source tree")

    id_material = "\n".join((IDENTITY_SCHEMA, branch, commit, source_hash, str(identity.get("EVIDENCE_INVENTORY_SHA256")))).encode("utf-8")
    expected_id = f"MIEXIANG-GENERIC-PUBLISH-FIX01-{hashlib.sha256(id_material).hexdigest()[:24]}"
    if identity.get("CANDIDATE_ID") != expected_id or identity.get("IDENTITY_SHA256") != fingerprint(identity) or identity.get("CANDIDATE_IDENTITY_SELF_CHECK") != "PASS":
        raise VerificationError("candidate identity self-check failed")

    inventory_hash = hashlib.sha256(inventory_bytes).hexdigest()
    if inventory.get("schema") != "MIEXIANG_CANDIDATE_EVIDENCE_INVENTORY_V1" or identity.get("EVIDENCE_INVENTORY_SHA256") != inventory_hash:
        raise VerificationError("evidence inventory identity differs")
    expected_files = inventory.get("entries")
    if not isinstance(expected_files, list):
        raise VerificationError("evidence inventory entries are invalid")
    actual_files: list[dict[str, object]] = []
    evidence_root = candidate / "evidence"
    for path in evidence_root.rglob("*"):
        if path.is_symlink():
            raise VerificationError("candidate evidence contains a symbolic link")
        if path.is_file():
            content = path.read_bytes()
            actual_files.append({
                "path": path.relative_to(evidence_root).as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
    actual_files.sort(key=lambda row: str(row["path"]).encode("utf-8"))
    if actual_files != expected_files:
        raise VerificationError("candidate evidence differs from its inventory")
    summary_bytes = (evidence_root / "e2e-summary.json").read_bytes()
    summary = json.loads(summary_bytes.decode("utf-8"))
    if identity.get("E2E_COMMIT") != commit or summary.get("E2E_COMMIT") != commit or identity.get("E2E_SUMMARY_SHA256") != hashlib.sha256(summary_bytes).hexdigest():
        raise VerificationError("E2E evidence is not bound to the candidate commit")
    required_evidence = (
        "FRESH_GITHUB_RELEASE_E2E",
        "SAME_WORKSPACE_IDEMPOTENCY",
        "CLEAN_WORKSPACE_IDEMPOTENCY",
        "CLEAN_R2_REPLAY",
        "CLEAN_FRESH_RELEASE_REPLAY",
        "REMOTE_HASH_VERIFY",
        "VERSION_HASH_CONFLICT",
        "TEST_SUITE",
    )
    try:
        provider_e2e_count = int(summary.get("REAL_PROVIDER_E2E_COUNT", 0))
    except (TypeError, ValueError):
        provider_e2e_count = 0
    if any(summary.get(key) != "PASS" for key in required_evidence) or provider_e2e_count < 1 or summary.get("GITHUB_PUBLIC_REPO") != "example/release-fixture":
        raise VerificationError("E2E evidence does not satisfy the required release checks")

    return {
        "CANDIDATE_ID": expected_id,
        "IMPLEMENTATION_COMMIT": commit,
        "CANDIDATE_IDENTITY_SELF_CHECK": "PASS",
        "DIRECTORY_DIGEST_INDEPENDENT_RECALC": "PASS",
        "EVIDENCE_INVENTORY_CHECK": "PASS",
        "E2E_COMMIT_EQUALITY": "PASS",
        "WORKTREE_STATUS": "CLEAN",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently recalculate and verify a frozen candidate identity.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--candidate-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.repo_root, args.candidate_dir)
    except (VerificationError, OSError, ValueError) as error:
        print(json.dumps({"VERIFY_STATUS": "FAIL", "ERROR": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"VERIFY_STATUS": "PASS", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
