import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from miexiang_publish.errors import PublishError
from miexiang_publish.manifest import build_manifest, encode_manifest, same_release_identity
from miexiang_publish.models import Artifact, PublicationRequest, StreamVerification, identify_artifact
from miexiang_publish.profile import GitHubProfile, PublishProfile
from miexiang_publish.publisher import GenericPublisher
from miexiang_publish.providers.github_release import GitHubReleaseAdapter


class ManifestReplayTests(unittest.TestCase):
    def _manifest(
        self,
        artifact,
        request,
        *,
        generated_at,
        verified_at,
        remote_verified_at,
        sha256=None,
        published_at="2026-09-24T06:32:40Z",
    ):
        digest = sha256 or artifact.sha256
        release = Artifact(
            artifact.filename,
            artifact.display_name,
            artifact.size_bytes,
            digest,
            artifact.content_type,
            artifact.source_path,
        )
        provider = {
            "provider": "github-releases",
            "role": "mirror",
            "status": "VERIFIED",
            "url": f"https://github.com/example/release-fixture/releases/download/test-{request.version}/{release.filename}",
            "published_at": published_at,
            "remote_verified_at": remote_verified_at,
            "verified_sha256": digest,
        }
        return encode_manifest(build_manifest(
            request,
            release,
            providers=[provider],
            mirror_policy="required",
            generated_at=generated_at,
            published_at=published_at,
            verified_at=verified_at,
        ))

    def test_release_identity_ignores_only_verification_observation_timestamps(self):
        request = PublicationRequest("miexiang-publish-e2e", "20260924-fixture.1", "test")
        artifact = Artifact("fixture.bin", "fixture.bin", 4, hashlib.sha256(b"test").hexdigest(), "application/octet-stream", Path("E:/fixture.bin"))
        first = self._manifest(
            artifact, request,
            generated_at="2026-09-24T06:00:00Z",
            verified_at="2026-09-24T06:01:00Z",
            remote_verified_at="2026-09-24T06:01:00Z",
        )
        replay = self._manifest(
            artifact, request,
            generated_at="2026-09-24T07:00:00Z",
            verified_at="2026-09-24T07:02:00Z",
            remote_verified_at="2026-09-24T07:02:00Z",
        )

        self.assertNotEqual(first, replay)
        self.assertTrue(same_release_identity(first, replay))

    def test_release_identity_observation_and_stable_field_contract_cases_a_through_h(self):
        request = PublicationRequest("miexiang-publish-e2e", "20260924-fixture.contract", "test")
        artifact = Artifact("fixture.bin", "fixture.bin", 4, hashlib.sha256(b"test").hexdigest(), "application/octet-stream", Path("E:/fixture.bin"))
        baseline = self._manifest(
            artifact, request,
            generated_at="2026-09-24T06:00:00Z",
            published_at="2026-09-24T06:00:30Z",
            verified_at="2026-09-24T06:01:00Z",
            remote_verified_at="2026-09-24T06:01:30Z",
        )

        def observation_variant(**changes):
            values = {
                "generated_at": "2026-09-24T06:00:00Z",
                "published_at": "2026-09-24T06:00:30Z",
                "verified_at": "2026-09-24T06:01:00Z",
                "remote_verified_at": "2026-09-24T06:01:30Z",
            }
            values.update(changes)
            return self._manifest(artifact, request, **values)

        changed_all_observations = observation_variant(
            generated_at="2026-09-24T07:00:00Z",
            published_at="2026-09-24T07:00:30Z",
            verified_at="2026-09-24T07:01:00Z",
            remote_verified_at="2026-09-24T07:01:30Z",
        )
        changed_artifact_sha = self._manifest(
            artifact, request,
            generated_at="2026-09-24T06:00:00Z",
            published_at="2026-09-24T06:00:30Z",
            verified_at="2026-09-24T06:01:00Z",
            remote_verified_at="2026-09-24T06:01:30Z",
            sha256=hashlib.sha256(b"else").hexdigest(),
        )
        changed_version = json.loads(baseline)
        changed_version["version"] = "20260924-fixture.contract.2"
        changed_target = json.loads(baseline)
        changed_target["providers"][0]["url"] = "https://github.com/other/release/file.bin"
        changed_target["remote_url"] = changed_target["providers"][0]["url"]

        cases = (
            ("A published_at only", observation_variant(published_at="2026-09-24T08:00:30Z"), True),
            ("B generated_at only", observation_variant(generated_at="2026-09-24T08:00:00Z"), True),
            ("C verified_at only", observation_variant(verified_at="2026-09-24T08:01:00Z"), True),
            ("D remote_verified_at only", observation_variant(remote_verified_at="2026-09-24T08:01:30Z"), True),
            ("E all observation timestamps", changed_all_observations, True),
            ("F artifact SHA", changed_artifact_sha, False),
            ("G version", encode_manifest(changed_version), False),
            ("H provider target", encode_manifest(changed_target), False),
        )
        for label, candidate, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(same_release_identity(baseline, candidate), expected)

    def test_release_identity_still_rejects_stable_artifact_or_provider_changes(self):
        request = PublicationRequest("miexiang-publish-e2e", "20260924-fixture.2", "test")
        artifact = Artifact("fixture.bin", "fixture.bin", 4, hashlib.sha256(b"test").hexdigest(), "application/octet-stream", Path("E:/fixture.bin"))
        baseline = self._manifest(
            artifact, request,
            generated_at="2026-09-24T06:00:00Z",
            verified_at="2026-09-24T06:01:00Z",
            remote_verified_at="2026-09-24T06:01:00Z",
        )
        different_artifact = self._manifest(
            artifact, request,
            generated_at="2026-09-24T07:00:00Z",
            verified_at="2026-09-24T07:02:00Z",
            remote_verified_at="2026-09-24T07:02:00Z",
            sha256=hashlib.sha256(b"else").hexdigest(),
        )
        changed = json.loads(baseline)
        changed["providers"][0]["url"] = "https://github.com/other/release/file.bin"
        changed["remote_url"] = changed["providers"][0]["url"]
        self.assertFalse(same_release_identity(baseline, different_artifact))
        self.assertFalse(same_release_identity(baseline, encode_manifest(changed)))

    def test_clean_workspace_reuses_verified_remote_manifest_without_any_remote_write(self):
        with tempfile.TemporaryDirectory(prefix="manifest-clean-replay-") as tmp:
            root = Path(tmp)
            source = root / "fresh workspace artifact.bin"
            source.write_bytes(b"cross-workspace manifest replay")
            request = PublicationRequest("miexiang-publish-e2e", "20260924-clean-replay.1", "test")
            artifact = identify_artifact(source, request.product_id, request.version, request.channel)
            stable_url = f"https://github.com/example/release-fixture/releases/download/test-{request.version}/{artifact.filename}"
            old_manifest = encode_manifest(build_manifest(
                request,
                artifact,
                providers=[{
                    "provider": "github-releases",
                    "role": "mirror",
                    "status": "VERIFIED",
                    "url": stable_url,
                    "published_at": "2026-09-24T06:32:40Z",
                    "remote_verified_at": "2026-09-24T06:33:00Z",
                    "verified_sha256": artifact.sha256,
                }],
                mirror_policy="required",
                generated_at="2026-09-24T06:33:00Z",
                published_at="2026-09-24T06:32:40Z",
                verified_at="2026-09-24T06:33:00Z",
            ))
            manifest_writes = []
            manifest_reads = []

            class ExistingRemote:
                provider = "github-releases"
                role = "mirror"

                def publish_artifact(self, request, staged):
                    return {
                        "provider": self.provider,
                        "role": self.role,
                        "status": "VERIFIED",
                        "url": stable_url,
                        "published_at": "2026-09-24T06:32:40Z",
                        "remote_verified_at": "2026-09-24T07:00:00Z",
                        "verified_sha256": staged.sha256,
                        "remote_size_bytes": staged.size_bytes,
                        "idempotent": True,
                        "uploaded_bytes": 0,
                        "downloaded_bytes": staged.size_bytes,
                    }

                def read_manifest(self, request):
                    manifest_reads.append(request.version)
                    return old_manifest

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    manifest_writes.append(manifest_bytes)
                    if manifest_bytes != old_manifest:
                        raise PublishError("REMOTE_CONFLICT", "identity-matching remote Manifest bytes were not reused")
                    return {
                        "url": "https://raw.githubusercontent.com/example/release-fixture/commit/manifest.json",
                        "idempotent": True,
                        "verified_sha256": hashlib.sha256(old_manifest).hexdigest(),
                        "uploaded_bytes": 0,
                        "downloaded_bytes": len(old_manifest),
                    }

            with patch.multiple("miexiang_publish.paths", RUNTIME_ROOT=root / "runtime", STAGING_ROOT=root / "runtime" / "staging", LOCK_ROOT=root / "runtime" / "locks"):
                result = GenericPublisher(PublishProfile("required", frozenset({"test"}), GitHubProfile("example/release-fixture", True), None), [ExistingRemote()]).publish(request, source)

            local_manifest = root / "runtime" / "reports" / request.product_id / request.channel / request.version / "release-manifest.json"
            self.assertEqual(result["PUBLISH_STATUS"], "IDEMPOTENT_ALREADY_PUBLISHED")
            self.assertIs(result["REMOTE_WRITE_PERFORMED"], False)
            self.assertEqual(result["REMOTE_UPLOAD_BYTES"], 0)
            self.assertEqual(result["MANIFEST_SHA256"], hashlib.sha256(old_manifest).hexdigest())
            self.assertEqual(manifest_reads, [request.version])
            self.assertEqual(manifest_writes, [old_manifest])
            self.assertEqual(local_manifest.read_bytes(), old_manifest)
            collision = local_manifest.parent / ".manifest-fixture.tmp"
            collision.write_bytes(b"preexisting unrelated temporary file")
            with patch.multiple("miexiang_publish.paths", RUNTIME_ROOT=root / "runtime", STAGING_ROOT=root / "runtime" / "staging", LOCK_ROOT=root / "runtime" / "locks"), patch("miexiang_publish.publisher.uuid.uuid4") as unique:
                unique.return_value.hex = "fixture"
                with self.assertRaises(FileExistsError):
                    GenericPublisher(PublishProfile("required", frozenset({"test"}), GitHubProfile("example/release-fixture", True), None), [ExistingRemote()]).publish(request, source)
            self.assertEqual(collision.read_bytes(), b"preexisting unrelated temporary file")
            self.assertEqual(local_manifest.read_bytes(), old_manifest)


    def test_github_manifest_existing_timestamp_drift_is_idempotent_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix="github-manifest-drift-") as tmp:
            root = Path(tmp)
            source = root / "artifact.bin"
            source.write_bytes(b"manifest bytes")
            request = PublicationRequest("miexiang-publish-e2e", "20260924-manifest-drift.1", "test")
            artifact = identify_artifact(source, request.product_id, request.version, request.channel)
            stable_url = f"https://github.com/example/release-fixture/releases/download/test-{request.version}/{artifact.filename}"

            def make_manifest(timestamp):
                return encode_manifest(build_manifest(
                    request,
                    artifact,
                    providers=[{
                        "provider": "github-releases",
                        "role": "mirror",
                        "status": "VERIFIED",
                        "url": stable_url,
                        "published_at": "2026-09-24T06:32:40Z",
                        "remote_verified_at": timestamp,
                        "verified_sha256": artifact.sha256,
                    }],
                    mirror_policy="required",
                    generated_at=timestamp,
                    published_at="2026-09-24T06:32:40Z",
                    verified_at=timestamp,
                ))

            remote_bytes = make_manifest("2026-09-24T06:33:00Z")
            proposed_bytes = make_manifest("2026-09-24T07:00:00Z")
            remote_digest = hashlib.sha256(remote_bytes).hexdigest()
            encoded = base64.encodebytes(remote_bytes).decode("ascii")
            calls = []
            downloads = []

            def runner(args, **kwargs):
                calls.append(args)
                if args[1] == "api" and "/contents/manifests/" in args[2]:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"content": encoded}), stderr="")
                if args[1] == "api" and args[2].startswith("repos/example/release-fixture/commits?"):
                    return SimpleNamespace(returncode=0, stdout=json.dumps([{"sha": "a" * 40}]), stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_download(url, destination, expected_size, expected_sha256, allowed_hosts):
                downloads.append((url, expected_size, expected_sha256))
                return SimpleNamespace(
                    final_host="raw.githubusercontent.com",
                    verification=StreamVerification(expected_size, expected_sha256),
                )

            adapter = GitHubReleaseAdapter(GitHubProfile("example/release-fixture", True, "main"), root / "staging", runner=runner)
            with patch("miexiang_publish.providers.github_release.download_and_verify", side_effect=fake_download):
                result = adapter.publish_manifest(request, proposed_bytes, hashlib.sha256(proposed_bytes).hexdigest())

            self.assertTrue(result["idempotent"])
            self.assertEqual(result["verified_sha256"], remote_digest)
            self.assertEqual(result["uploaded_bytes"], 0)
            self.assertEqual(downloads[0][2], remote_digest)
            self.assertTrue(any("/commits?" in args[2] for args in calls if args[1] == "api"))
            self.assertFalse(any("--method" in args and "PUT" in args for args in calls))

    def test_github_manifest_stable_identity_conflict_is_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="github-manifest-real-conflict-") as tmp:
            root = Path(tmp)
            source = root / "artifact.bin"
            source.write_bytes(b"manifest bytes")
            request = PublicationRequest("miexiang-publish-e2e", "20260924-manifest-conflict.1", "test")
            artifact = identify_artifact(source, request.product_id, request.version, request.channel)
            stable_url = f"https://github.com/example/release-fixture/releases/download/test-{request.version}/{artifact.filename}"

            def make_manifest(release_notes):
                manifest_request = PublicationRequest(
                    request.product_id, request.version, request.channel, release_notes=release_notes
                )
                return encode_manifest(build_manifest(
                    manifest_request,
                    artifact,
                    providers=[{
                        "provider": "github-releases",
                        "role": "mirror",
                        "status": "VERIFIED",
                        "url": stable_url,
                        "published_at": "2026-09-24T06:32:40Z",
                        "remote_verified_at": "2026-09-24T06:33:00Z",
                        "verified_sha256": artifact.sha256,
                    }],
                    mirror_policy="required",
                    generated_at="2026-09-24T06:33:00Z",
                    published_at="2026-09-24T06:32:40Z",
                    verified_at="2026-09-24T06:33:00Z",
                ))

            remote_bytes = make_manifest(None)
            proposed_bytes = make_manifest("different stable release notes")
            encoded = base64.encodebytes(remote_bytes).decode("ascii")
            calls = []

            def runner(args, **kwargs):
                calls.append(args)
                if args[1] == "api" and "/contents/manifests/" in args[2]:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"content": encoded}), stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            adapter = GitHubReleaseAdapter(GitHubProfile("example/release-fixture", True, "main"), root / "staging", runner=runner)
            with self.assertRaises(PublishError) as caught:
                adapter.publish_manifest(request, proposed_bytes, hashlib.sha256(proposed_bytes).hexdigest())

            self.assertEqual(caught.exception.code, "REMOTE_CONFLICT")
            self.assertFalse(any("--method" in args and "PUT" in args for args in calls))


if __name__ == "__main__":
    unittest.main()
