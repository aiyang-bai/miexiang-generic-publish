from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from miexiang_publish.candidate_freeze import CandidateFreezeError, freeze_candidate


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "runtime" / "test-cases"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_candidate_identity.py"


class CandidateFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip()

    def _repo(self, parent: Path) -> tuple[Path, str]:
        repo = parent / "source"
        repo.mkdir()
        self._git(repo, "init", "--quiet", "-b", "main")
        self._git(repo, "config", "user.name", "Candidate Test")
        self._git(repo, "config", "user.email", "candidate-test@example.invalid")
        skill = repo / "skill" / "miexiang-generic-publish" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# harmless candidate skill\n", encoding="utf-8")
        (repo / "requirements.lock").write_text("sample==1.0\n", encoding="utf-8")
        (repo / "src.txt").write_bytes(b"tracked source bytes\x00\xff")
        self._git(repo, "add", "--all")
        self._git(repo, "commit", "--quiet", "-m", "fixture candidate")
        return repo, self._git(repo, "rev-parse", "HEAD")

    def _evidence(self, parent: Path, commit: str, *, e2e_commit: str | None = None) -> Path:
        evidence = parent / "evidence-input"
        evidence.mkdir()
        summary = {
            "E2E_COMMIT": e2e_commit or commit,
            "FRESH_GITHUB_RELEASE_E2E": "PASS",
            "SAME_WORKSPACE_IDEMPOTENCY": "PASS",
            "CLEAN_WORKSPACE_IDEMPOTENCY": "PASS",
            "CLEAN_R2_REPLAY": "PASS",
            "CLEAN_FRESH_RELEASE_REPLAY": "PASS",
            "REMOTE_HASH_VERIFY": "PASS",
            "VERSION_HASH_CONFLICT": "PASS",
            "TEST_SUITE": "PASS",
            "REAL_PROVIDER_E2E_COUNT": 1,
            "GITHUB_PUBLIC_REPO": "example/release-fixture",
        }
        (evidence / "e2e-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        (evidence / "e2e-evidence.json").write_text("{\"result\":\"PASS\"}\n", encoding="utf-8")
        return evidence

    def test_freeze_binds_commit_source_hashes_and_independent_digest(self):
        with tempfile.TemporaryDirectory(prefix="candidate-freeze-test-", dir=TEST_ROOT) as tmp:
            parent = Path(tmp)
            repo, commit = self._repo(parent)
            evidence = self._evidence(parent, commit)
            output = parent / "candidate-output"

            identity = freeze_candidate(repo, output, evidence)

            self.assertEqual(identity["IMPLEMENTATION_COMMIT"], commit)
            self.assertEqual(identity["FULL_40_CHAR_HASH"], commit)
            self.assertEqual(len(identity["FULL_40_CHAR_HASH"]), 40)
            self.assertEqual(identity["E2E_COMMIT"], commit)
            self.assertEqual(identity["WORKTREE_STATUS"], "CLEAN")
            self.assertEqual(identity["CANDIDATE_IDENTITY_SELF_CHECK"], "PASS")
            source_bytes = (output / "source-manifest.json").read_bytes()
            self.assertEqual(identity["SOURCE_MANIFEST_SHA256"], hashlib.sha256(source_bytes).hexdigest())
            replay_output = parent / "candidate-output-replay"
            replay_identity = freeze_candidate(repo, replay_output, evidence)
            self.assertEqual(replay_identity["DIRECTORY_DIGEST"], identity["DIRECTORY_DIGEST"])
            self.assertEqual(replay_identity["SOURCE_MANIFEST_SHA256"], identity["SOURCE_MANIFEST_SHA256"])
            verification = subprocess.run(
                [sys.executable, str(VERIFY_SCRIPT), "--repo-root", str(repo), "--candidate-dir", str(output)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            independent = json.loads(verification.stdout)
            self.assertEqual(independent["VERIFY_STATUS"], "PASS")
            self.assertEqual(independent["DIRECTORY_DIGEST_INDEPENDENT_RECALC"], "PASS")
            self.assertEqual(independent["CANDIDATE_ID"], identity["CANDIDATE_ID"])

    def test_freeze_rejects_dirty_source_and_mismatched_e2e_commit(self):
        with tempfile.TemporaryDirectory(prefix="candidate-freeze-reject-", dir=TEST_ROOT) as tmp:
            parent = Path(tmp)
            repo, commit = self._repo(parent)
            evidence = self._evidence(parent, commit)
            (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
            with self.assertRaises(CandidateFreezeError):
                freeze_candidate(repo, parent / "dirty-candidate", evidence)

        with tempfile.TemporaryDirectory(prefix="candidate-freeze-e2e-mismatch-", dir=TEST_ROOT) as tmp:
            parent = Path(tmp)
            repo, commit = self._repo(parent)
            evidence = self._evidence(parent, commit, e2e_commit="0" * 40)
            with self.assertRaises(CandidateFreezeError):
                freeze_candidate(repo, parent / "mismatched-candidate", evidence)

    def test_freeze_is_create_only(self):
        with tempfile.TemporaryDirectory(prefix="candidate-freeze-create-only-", dir=TEST_ROOT) as tmp:
            parent = Path(tmp)
            repo, commit = self._repo(parent)
            evidence = self._evidence(parent, commit)
            output = parent / "exists"
            output.mkdir()
            with self.assertRaises(CandidateFreezeError):
                freeze_candidate(repo, output, evidence)


if __name__ == "__main__":
    unittest.main()
