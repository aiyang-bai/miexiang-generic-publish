import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.parse
import urllib.request

from miexiang_publish.errors import PublishError
from miexiang_publish.manifest import build_manifest, encode_manifest, validate_manifest
from miexiang_publish.models import Artifact, PublicationRequest, StreamVerification, canonical_artifact_name, identify_artifact, verify_stream
from miexiang_publish.policy import check_version_binding
from miexiang_publish.credentials import CosCredentials
from miexiang_publish.credentials import read_cos_credentials
from miexiang_publish.profile import CosProfile, GitHubProfile, PublishProfile, load_profile
from miexiang_publish.redaction import redact_text
from miexiang_publish.publisher import GenericPublisher
from miexiang_publish.cli import main as cli_main
from miexiang_publish.providers.github_release import GitHubReleaseAdapter
from miexiang_publish.providers.tencent_cos import TencentCosAdapter, make_no_overwrite_client_type
from miexiang_publish.http_verify import _HttpsOnlyRedirects, _copy_bounded


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "runtime" / "test-cases"


class CoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)

    def test_identifies_unicode_spaced_windows_path_without_changing_source(self):
        with tempfile.TemporaryDirectory(prefix="用户 路径 ", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "安装 包 fixture.bin"
            payload = b"harmless-publisher-probe\x00" * 128
            source.write_bytes(payload)

            artifact = identify_artifact(
                source,
                product_id="miexiang-test-publisher",
                version="0.1.0-rc.1",
                channel="test",
                display_name="咩想测试文件.bin",
            )

            self.assertEqual(artifact.sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(artifact.size_bytes, len(payload))
            self.assertEqual(artifact.filename, "miexiang-test-publisher-0.1.0-rc.1.bin")
            self.assertEqual(source.read_bytes(), payload)
            self.assertNotIn(str(source), json.dumps(artifact.to_public_dict(), ensure_ascii=False))

    def test_rejects_missing_or_non_file_artifact_with_structured_error(self):
        with self.assertRaises(PublishError) as caught:
            identify_artifact(
                TEST_ROOT / "missing.bin",
                product_id="miexiang-test-publisher",
                version="0.1.0",
                channel="test",
            )
        self.assertEqual(caught.exception.code, "ARTIFACT_NOT_FOUND")

    def test_provider_safe_filename_uses_explicit_identity_and_extension(self):
        self.assertEqual(
            canonical_artifact_name("miexiang-test-publisher", "v1.2.3-rc.4", "安装包.exe"),
            "miexiang-test-publisher-v1.2.3-rc.4.exe",
        )

    def test_wheel_tags_are_preserved_and_identity_cannot_be_substituted(self):
        name = "miexiang_generic_publish-0.2.0rc1-py3-none-any.whl"
        self.assertEqual(canonical_artifact_name("miexiang-generic-publish", "0.2.0rc1", name), name)
        for invalid in ("miexiang-generic-publish-0.2.0rc1.whl", "other-0.2.0rc1-py3-none-any.whl", "miexiang_generic_publish-9.0-py3-none-any.whl", "../" + name, "miexiang_generic_publish-0.2.0rc1-..-none-any.whl"):
            with self.subTest(invalid=invalid), self.assertRaises(PublishError):
                canonical_artifact_name("miexiang-generic-publish", "0.2.0rc1", invalid)

    def test_wheel_version_rejects_unsupported_non_numeric_identity(self):
        with self.assertRaises(PublishError):
            canonical_artifact_name("fixture", "foo", "fixture-foo-py3-none-any.whl")

    def test_release_metadata_rejects_local_paths_and_inline_credentials(self):
        cases = [
            {"display_name": "E:\\Users\\Owner\\private-installer.exe"},
            {"release_notes": "Built from C:\\work\\app"},
            {"release_notes": "Built from /workspace/private/app"},
            {"release_notes": "See file:///E:/private/app"},
            {"release_notes": "SecretKey=do-not-publish"},
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(PublishError) as caught:
                PublicationRequest("miexiang-test-publisher", "0.1.0", "test", **extra)
            self.assertEqual(caught.exception.code, "POLICY_REJECTED")

    def test_actual_remote_stream_must_match_size_and_sha256(self):
        payload = b"complete bytes from the consumer URL"
        result = verify_stream(io.BytesIO(payload), len(payload), hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        with self.assertRaises(PublishError) as caught:
            verify_stream(io.BytesIO(payload[:-1]), len(payload), hashlib.sha256(payload).hexdigest())
        self.assertEqual(caught.exception.code, "HASH_MISMATCH")

    def test_same_version_hash_is_idempotent_but_changed_hash_conflicts(self):
        check_version_binding(existing_sha256="a" * 64, requested_sha256="a" * 64)
        with self.assertRaises(PublishError) as caught:
            check_version_binding(existing_sha256="a" * 64, requested_sha256="b" * 64)
        self.assertEqual(caught.exception.code, "VERSION_HASH_CONFLICT")

    def test_manifest_is_valid_and_contains_no_local_path(self):
        artifact = Artifact(
            filename="miexiang-test-publisher-0.1.0.bin",
            display_name="咩想试验文件.bin",
            size_bytes=5,
            sha256=hashlib.sha256(b"bytes").hexdigest(),
            content_type="application/octet-stream",
            source_path=Path("E:/protected/source.bin"),
        )
        manifest = build_manifest(
            PublicationRequest("miexiang-test-publisher", "0.1.0", "test"),
            artifact,
            providers=[{
                "provider": "github-releases",
                "role": "mirror",
                "status": "VERIFIED",
                "url": "https://github.com/example/repo/releases/download/test/file.bin",
                "published_at": "2026-09-24T00:00:00Z",
                "remote_verified_at": "2026-09-24T00:01:00Z",
                "verified_sha256": artifact.sha256,
            }],
            mirror_policy="required",
        )
        validate_manifest(manifest)
        encoded = json.dumps(manifest, ensure_ascii=False)
        self.assertIn("MIEXIANG_RELEASE_MANIFEST_V1", encoded)
        self.assertNotIn("C:\\", encoded)
        self.assertNotIn("E:\\", encoded)

    def test_manifest_contains_flat_provider_e2e_fields(self):
        digest = hashlib.sha256(b"release bytes").hexdigest()
        artifact = Artifact(
            filename="miexiang-test-publisher-0.1.0.bin",
            display_name="咩想试验文件.bin",
            size_bytes=13,
            sha256=digest,
            content_type="application/octet-stream",
            source_path=Path("E:/protected/source.bin"),
        )
        request = PublicationRequest("miexiang-test-publisher", "0.1.0", "test")
        provider = {
            "provider": "github-releases",
            "role": "mirror",
            "status": "VERIFIED",
            "url": "https://github.com/example/repo/releases/download/test/file.bin",
            "published_at": "2026-09-24T00:00:00Z",
            "remote_verified_at": "2026-09-24T00:01:00Z",
            "verified_sha256": digest,
        }

        manifest = build_manifest(
            request,
            artifact,
            providers=[provider],
            mirror_policy="optional",
            generated_at="2026-09-24T00:00:00Z",
            published_at="2026-09-24T00:00:00Z",
        )

        self.assertEqual(manifest["schema_version"], "MIEXIANG_RELEASE_MANIFEST_V1")
        self.assertEqual(manifest["channel"], "test")
        self.assertEqual(manifest["display_name"], "咩想试验文件.bin")
        self.assertEqual(manifest["artifact_name"], artifact.filename)
        self.assertEqual(manifest["size"], 13)
        self.assertEqual(manifest["sha256"], digest)
        self.assertEqual(manifest["provider"], "github-releases")
        self.assertEqual(manifest["remote_url"], provider["url"])
        self.assertEqual(manifest["verified_at"], provider["remote_verified_at"])
        self.assertEqual(manifest["remote_sha256"], digest)
        self.assertEqual(manifest["verification_status"], "VERIFIED")

    def test_manifest_rejects_provider_hash_mismatch(self):
        artifact = Artifact("file.bin", "file.bin", 1, "a" * 64, "application/octet-stream", Path("E:/protected/source.bin"))
        with self.assertRaises(PublishError) as caught:
            build_manifest(
                PublicationRequest("miexiang-test-publisher", "0.1.0", "test"),
                artifact,
                providers=[{
                    "provider": "github-releases",
                    "role": "mirror",
                    "status": "VERIFIED",
                    "url": "https://github.com/example/repo/releases/download/test/file.bin",
                    "published_at": "2026-09-24T00:00:00Z",
                    "remote_verified_at": "2026-09-24T00:01:00Z",
                    "verified_sha256": "b" * 64,
                }],
                mirror_policy="required",
            )
        self.assertEqual(caught.exception.code, "MANIFEST_INVALID")

    def test_secret_and_token_values_are_redacted(self):
        text = "SecretId=AKID012345678901234567890; SecretKey=private-value; Authorization: Bearer ghp_123456789012345678901234567890123456"
        safe = redact_text(text)
        self.assertNotIn("AKID012345678901234567890", safe)
        self.assertNotIn("private-value", safe)
        self.assertNotIn("ghp_123456789012345678901234567890123456", safe)
        self.assertIn("[REDACTED]", safe)

    def test_redirects_must_remain_https_and_inside_provider_host_allowlist(self):
        handler = _HttpsOnlyRedirects(("github.com", "githubusercontent.com"))
        request = urllib.request.Request("https://github.com/owner/repo/file")
        with self.assertRaises(PublishError) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, "http://github.com/file")
        self.assertEqual(caught.exception.code, "DOWNLOAD_FAILED")
        with self.assertRaises(PublishError) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, "https://github.com.attacker.invalid/file")
        self.assertEqual(caught.exception.code, "DOWNLOAD_FAILED")
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://objects.githubusercontent.com/file")
        self.assertEqual(redirected.full_url, "https://objects.githubusercontent.com/file")

    def test_consumer_download_stream_is_bounded_to_expected_size(self):
        destination = io.BytesIO()
        with self.assertRaises(PublishError) as caught:
            _copy_bounded(io.BytesIO(b"123456789"), destination, 8)
        self.assertEqual(caught.exception.code, "HASH_MISMATCH")
        self.assertEqual(destination.getvalue(), b"")

    def test_configured_cos_session_reference_cannot_silently_fall_back_to_long_lived_key(self):
        targets = {
            "secret_id": "MiexiangGenericPublish/COS/test/id",
            "secret_key": "MiexiangGenericPublish/COS/test/key",
            "session_token": "MiexiangGenericPublish/COS/test/session",
        }
        with patch("miexiang_publish.credentials._read_generic_credential", side_effect=["id-value", "key-value", None]):
            self.assertIsNone(read_cos_credentials(targets))

    def test_profile_rejects_incomplete_credential_target_references(self):
        with tempfile.TemporaryDirectory(prefix="profile ", dir=TEST_ROOT) as tmp:
            profile_path = Path(tmp) / "profile.json"
            profile_path.write_text(json.dumps({
                "profile_version": 1,
                "provider_policy": "cn-software-v1",
                "cos": {
                    "bucket": "probe-1234567890",
                    "region": "ap-beijing",
                    "prefix": "MiexiangGenericPublish/COS/test",
                    "credential_targets": {"session_token": "MiexiangGenericPublish/COS/session"},
                    "cost_budget_bytes": 2_000_000,
                },
            }), encoding="utf-8")
            with self.assertRaises(PublishError) as caught:
                load_profile(profile_path)
            self.assertEqual(caught.exception.code, "POLICY_REJECTED")

    def test_cos_sdk_bridge_applies_no_overwrite_on_single_and_multipart_completion(self):
        calls = []

        class FakeSdkClient:
            def __init__(self, config):
                self.config = config

            def upload_file(self, **kwargs):
                calls.append(("init", kwargs["ForbidOverwrite"]))
                return self.complete_multipart_upload(Bucket=kwargs["Bucket"], Key=kwargs["Key"])

            def complete_multipart_upload(self, **kwargs):
                calls.append(("complete", kwargs.get("ForbidOverwrite")))
                return {"ok": True}

        header_map = {}
        client_type = make_no_overwrite_client_type(FakeSdkClient, header_map)
        client = client_type(None)
        client.upload_file(Bucket="bucket", Key="object", LocalFilePath="fixture", ForbidOverwrite="true")
        self.assertEqual(header_map["ForbidOverwrite"], "x-cos-forbid-overwrite")
        self.assertEqual(calls, [("init", "true"), ("complete", "true")])

    @unittest.skipUnless(__import__("importlib.util").util.find_spec("qcloud_cos"), "optional COS SDK absent; tested in COS job")
    def test_cos_installed_sdk_header_mapping(self):
        class FakeSdkClient:
            pass
        from qcloud_cos.cos_comm import maplist, mapped

        make_no_overwrite_client_type(FakeSdkClient, maplist)
        self.assertEqual(mapped({"ForbidOverwrite": "true"}), {"x-cos-forbid-overwrite": "true"})

    def test_required_provider_failure_is_partial_and_withholds_success_manifest(self):
        payload = b"fixed test payload"
        with tempfile.TemporaryDirectory(prefix="publisher-partial-", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "artifact.bin"
            source.write_bytes(payload)

            class GoodProvider:
                provider = "test-primary"
                role = "primary"

                def publish_artifact(self, request, artifact):
                    return {
                        "provider": self.provider,
                        "role": self.role,
                        "status": "VERIFIED",
                        "url": "https://example.invalid/artifact.bin",
                        "published_at": "2026-09-24T00:00:00Z",
                        "remote_verified_at": "2026-09-24T00:01:00Z",
                        "verified_sha256": artifact.sha256,
                        "remote_size_bytes": artifact.size_bytes,
                        "idempotent": False,
                    }

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    raise AssertionError("required-provider failure must withhold the manifest")

            class DownProvider:
                provider = "test-mirror"
                role = "mirror"

                def publish_artifact(self, request, artifact):
                    raise PublishError("PROVIDER_UNAVAILABLE")

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    raise AssertionError("required-provider failure must withhold the manifest")

            profile = PublishProfile("required", frozenset({"test"}), None, None)
            publisher = GenericPublisher(profile, [GoodProvider(), DownProvider()])
            result = publisher.publish(PublicationRequest("miexiang-test-publisher", "0.1.0-partial.1", "test"), source)
            self.assertEqual(result["PUBLISH_STATUS"], "PARTIAL")
            self.assertIsNone(result["REMOTE_WRITE_PERFORMED"])
            self.assertIsNone(result["MANIFEST_PATH"])
            self.assertEqual(result["COS_BYTES_UPLOADED"], 0)

    def test_publisher_maps_auth_failure_to_contract_status_without_manifest(self):
        payload = b"auth not ready test"
        with tempfile.TemporaryDirectory(prefix="publisher-auth-", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "artifact.bin"
            source.write_bytes(payload)

            class AuthProvider:
                provider = "tencent-cos"
                role = "primary"

                def publish_artifact(self, request, artifact):
                    raise PublishError("AUTH_NOT_READY")

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    raise AssertionError("manifest must not be attempted before provider verification")

            profile = PublishProfile("none", frozenset({"test"}), None, None)
            result = GenericPublisher(profile, [AuthProvider()]).publish(
                PublicationRequest("miexiang-test-publisher", "0.1.0-auth.1", "test"), source
            )
            self.assertEqual(result["PUBLISH_STATUS"], "AUTH_REQUIRED")
            self.assertFalse(result["PRIMARY_REMOTE_VERIFIED"])
            self.assertIsNone(result["MANIFEST_SHA256"])
            self.assertEqual(result["COS_BYTES_DOWNLOADED"], 0)

    def test_cli_maps_missing_publish_authorization_to_auth_required(self):
        with tempfile.TemporaryDirectory(prefix="cli-auth-", dir=TEST_ROOT) as tmp:
            artifact = Path(tmp) / "artifact.bin"
            artifact.write_bytes(b"publish authorization test")
            printed = []
            with patch("miexiang_publish.cli._print", side_effect=printed.append):
                exit_code = cli_main([
                    "publish", "--product-id", "miexiang-test-publisher", "--version", "0.1.0-no-auth.1",
                    "--channel", "test", "--artifact", str(artifact),
                ])
            self.assertEqual(exit_code, 2)
            self.assertEqual(printed[0]["PUBLISH_STATUS"], "AUTH_REQUIRED")
            self.assertEqual(printed[0]["ERROR_CODE"], "AUTH_NOT_READY")

    def test_optional_mirror_failure_is_not_mislabeled_as_idempotent_success(self):
        with tempfile.TemporaryDirectory(prefix="publisher-optional-", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "artifact.bin"
            source.write_bytes(b"optional mirror test")

            class Primary:
                provider = "tencent-cos"
                role = "primary"

                def publish_artifact(self, request, artifact):
                    return {
                        "provider": self.provider, "role": self.role, "status": "VERIFIED",
                        "url": "https://bucket.cos.ap-beijing.myqcloud.com/artifact.bin",
                        "published_at": "2026-09-24T00:00:00Z", "remote_verified_at": "2026-09-24T00:01:00Z",
                        "verified_sha256": artifact.sha256, "remote_size_bytes": artifact.size_bytes,
                        "idempotent": True, "uploaded_bytes": 0, "downloaded_bytes": artifact.size_bytes,
                    }

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    return {"url": "https://bucket.cos.ap-beijing.myqcloud.com/manifest.json", "idempotent": True}

            class MirrorDown:
                provider = "github-releases"
                role = "mirror"

                def publish_artifact(self, request, artifact):
                    raise PublishError("PROVIDER_UNAVAILABLE")

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    raise AssertionError("failed artifact mirror must not receive the manifest")

            profile = PublishProfile("optional", frozenset({"test"}), None, None)
            result = GenericPublisher(profile, [Primary(), MirrorDown()]).publish(
                PublicationRequest("miexiang-test-publisher", "0.1.0-optional.2", "test"), source
            )
            self.assertEqual(result["PUBLISH_STATUS"], "PUBLISH_SUCCESS_WITH_OPTIONAL_DEGRADED")

    def test_github_provider_unavailable_withholds_verified_manifest_and_success(self):
        with tempfile.TemporaryDirectory(prefix="publisher-github-down-", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "artifact.bin"
            source.write_bytes(b"controlled provider outage")

            class GithubDown:
                provider = "github-releases"
                role = "mirror"

                def publish_artifact(self, request, artifact):
                    raise PublishError("PROVIDER_UNAVAILABLE", "controlled network outage")

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    raise AssertionError("a failed provider must not receive a VERIFIED Manifest")

            profile = PublishProfile("optional", frozenset({"test"}), None, None)
            result = GenericPublisher(profile, [GithubDown()]).publish(
                PublicationRequest("miexiang-test-publisher", "0.1.0-github-down.1", "test"), source
            )

            self.assertEqual(result["PUBLISH_STATUS"], "FAILED")
            self.assertFalse(result["MIRROR_REMOTE_VERIFIED"])
            self.assertIsNone(result["MANIFEST_PATH"])
            self.assertIsNone(result["MANIFEST_SHA256"])
            self.assertEqual(result["MANIFEST_PROVIDERS"], [])
            self.assertEqual(result["PROVIDERS"][0]["error_code"], "PROVIDER_UNAVAILABLE")

    def test_same_identity_retry_keeps_manifest_bytes_stable(self):
        with tempfile.TemporaryDirectory(prefix="publisher-idempotent-", dir=TEST_ROOT) as tmp:
            source = Path(tmp) / "artifact.bin"
            source.write_bytes(b"stable manifest bytes")
            artifact_calls = 0
            manifest_calls = 0
            manifests = []

            class Primary:
                provider = "tencent-cos"
                role = "primary"

                def publish_artifact(self, request, artifact):
                    nonlocal artifact_calls
                    artifact_calls += 1
                    return {
                        "provider": self.provider, "role": self.role, "status": "VERIFIED",
                        "url": "https://bucket.cos.ap-beijing.myqcloud.com/release.bin",
                        "published_at": "2026-09-24T05:22:25Z",
                        "remote_verified_at": f"2026-09-24T05:22:{25 + artifact_calls:02d}Z",
                        "verified_sha256": artifact.sha256, "remote_size_bytes": artifact.size_bytes,
                        "idempotent": artifact_calls > 1, "uploaded_bytes": 0, "downloaded_bytes": artifact.size_bytes,
                    }

                def publish_manifest(self, request, manifest_bytes, manifest_sha256):
                    nonlocal manifest_calls
                    manifest_calls += 1
                    manifests.append(manifest_bytes)
                    return {
                        "url": "https://bucket.cos.ap-beijing.myqcloud.com/manifest.json",
                        "idempotent": manifest_calls > 1,
                        "uploaded_bytes": 0,
                        "downloaded_bytes": len(manifest_bytes),
                    }

            profile = PublishProfile("required", frozenset({"test"}), None, None)
            publisher = GenericPublisher(profile, [Primary()])
            request = PublicationRequest("miexiang-test-publisher", "0.1.0-repeat.2", "test")
            first = publisher.publish(request, source)
            second = publisher.publish(request, source)
            self.assertEqual(first["PUBLISH_STATUS"], "PUBLISH_SUCCESS")
            self.assertEqual(second["PUBLISH_STATUS"], "IDEMPOTENT_ALREADY_PUBLISHED")
            self.assertEqual(first["MANIFEST_SHA256"], second["MANIFEST_SHA256"])
            self.assertEqual(manifests[0], manifests[1])

    def test_cos_artifact_and_manifest_use_no_overwrite_and_public_get_verification(self):
        payload = b"cos test payload"
        digest = hashlib.sha256(payload).hexdigest()
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-cos.1", "test")
        with tempfile.TemporaryDirectory(prefix="cos-provider-", dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            source = root / "cos input.bin"
            source.write_bytes(payload)
            artifact = identify_artifact(source, request.product_id, request.version, request.channel)

            class NotFound(Exception):
                def get_status_code(self):
                    return 404

                def get_error_code(self):
                    return "NoSuchKey"

            class AlreadyExists(Exception):
                def get_status_code(self):
                    return 409

                def get_error_code(self):
                    return "FileAlreadyExists"

            class FakeCosClient:
                def __init__(self):
                    self.objects = {}
                    self.write_headers = []
                    self.upload_count = 0
                    self.copy_count = 0

                def head_bucket(self, **kwargs):
                    return {"ok": True}

                def get_bucket_versioning(self, **kwargs):
                    return {"Status": "Disabled"}

                def head_object(self, Bucket, Key):
                    if Key not in self.objects:
                        raise NotFound()
                    return {"ContentLength": len(self.objects[Key]), "LastModified": "Wed, 24 Sep 2026 05:22:25 GMT"}

                def get_object(self, Bucket, Key):
                    if Key not in self.objects:
                        raise NotFound()
                    return {"Body": io.BytesIO(self.objects[Key])}

                def put_object(self, Bucket, Key, Body, **kwargs):
                    self.write_headers.append(kwargs.get("ForbidOverwrite"))
                    if Key in self.objects:
                        raise AlreadyExists()
                    self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
                    return {"ok": True}

                def upload_file(self, Bucket, Key, LocalFilePath, **kwargs):
                    self.write_headers.append(kwargs.get("ForbidOverwrite"))
                    if Key in self.objects:
                        raise AlreadyExists()
                    self.upload_count += 1
                    self.objects[Key] = Path(LocalFilePath).read_bytes()
                    return {"ok": True}

                def copy_object(self, Bucket, Key, CopySource, **kwargs):
                    self.write_headers.append(kwargs.get("ForbidOverwrite"))
                    if Key in self.objects:
                        raise AlreadyExists()
                    self.copy_count += 1
                    self.objects[Key] = self.objects[CopySource["Key"]]
                    return {"ETag": '"copied"'}

            client = FakeCosClient()
            profile = CosProfile(
                bucket="probe-1234567890", region="ap-beijing", prefix="MiexiangGenericPublish/COS/test",
                public_write_authorized=True, versioning_never_enabled=True,
                forbid_overwrite_policy_enforced=True, private_staging_prefix_enforced=True,
                credential_targets={"secret_id": "MiexiangGenericPublish/COS/test/secret-id", "secret_key": "MiexiangGenericPublish/COS/test/secret-key"},
                cost_budget_bytes=2_000_000,
            )
            adapter = TencentCosAdapter(
                profile, root, credential_reader=lambda _: CosCredentials("id-placeholder", "key-placeholder"), client_factory=lambda *_: client
            )
            preview = adapter.preview_artifact(request, artifact)
            self.assertEqual(preview["identity"], "NEW")
            self.assertEqual(client.write_headers, [])

            def fake_download(url, destination, expected_size, expected_sha256, allowed_hosts):
                key = urllib.parse.unquote(urllib.parse.urlparse(url).path.lstrip("/"))
                data = client.objects[key]
                destination.write_bytes(data)
                actual = hashlib.sha256(data).hexdigest()
                if len(data) != expected_size or actual != expected_sha256:
                    raise PublishError("HASH_MISMATCH")
                return SimpleNamespace(final_host="probe-1234567890.cos.ap-beijing.myqcloud.com", verification=StreamVerification(len(data), actual))

            with patch("miexiang_publish.providers.tencent_cos.download_and_verify", side_effect=fake_download):
                artifact_result = adapter.publish_artifact(request, artifact)
                repeat_result = adapter.publish_artifact(request, artifact)
                changed_path = root / "changed.bin"
                changed_path.write_bytes(b"different bytes, same immutable version")
                changed_artifact = identify_artifact(changed_path, request.product_id, request.version, request.channel)
                with self.assertRaises(PublishError) as caught:
                    adapter.publish_artifact(request, changed_artifact)
                self.assertEqual(caught.exception.code, "VERSION_HASH_CONFLICT")
                manifest_bytes = b'{"schema":"MIEXIANG_RELEASE_MANIFEST_V1"}\n'
                manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
                manifest_result = adapter.publish_manifest(request, manifest_bytes, manifest_digest)

            self.assertEqual(artifact_result["status"], "VERIFIED")
            self.assertEqual(artifact_result["verified_sha256"], digest)
            self.assertEqual(artifact_result["remote_size_bytes"], len(payload))
            self.assertGreaterEqual(artifact_result["downloaded_bytes"], 2 * len(payload))
            self.assertFalse(artifact_result["idempotent"])
            self.assertTrue(repeat_result["idempotent"])
            self.assertEqual(client.upload_count, 1)
            self.assertEqual(client.copy_count, 1)
            self.assertEqual(artifact_result["checksums_sha256"], hashlib.sha256(f"{digest}  {artifact.filename}\n".encode()).hexdigest())
            self.assertEqual(manifest_result["verified_sha256"], manifest_digest)
            self.assertTrue(client.write_headers)
            self.assertEqual(set(client.write_headers), {"true"})
            self.assertTrue(any(key.startswith("MiexiangGenericPublish/COS/test/private-staging/") for key in client.objects))

    def test_github_real_limit_is_rejected_before_any_provider_call(self):
        calls = []
        adapter = GitHubReleaseAdapter(
            GitHubProfile("owner/repo", True),
            TEST_ROOT,
            runner=lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        artifact = Artifact("large.bin", "large.bin", 2 * 1024 * 1024 * 1024, "a" * 64, "application/octet-stream", TEST_ROOT / "not-read.bin")
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-large.1", "test")
        with self.assertRaises(PublishError) as caught:
            adapter.publish_artifact(request, artifact)
        self.assertEqual(caught.exception.code, "FILE_TOO_LARGE")
        self.assertEqual(calls, [])

    def test_github_preview_inspects_existing_identity_without_writing(self):
        payload = b"preview-only bytes"
        artifact = Artifact("miexiang-test-publisher-0.1.0-preview.1.bin", "fixture.bin", len(payload), hashlib.sha256(payload).hexdigest(), "application/octet-stream", TEST_ROOT / "fixture.bin")
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-preview.1", "test")
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="Logged in (keyring)", stderr="")
            if args[1] == "api" and args[2] == "repos/owner/repo":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"private": False, "default_branch": "main", "permissions": {"push": True}}), stderr="")
            if args[1] == "api" and "/releases/tags/" in args[2]:
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
            if args[1] == "api" and args[2] == "repos/owner/repo/releases":
                return SimpleNamespace(returncode=0, stdout=json.dumps([[]]), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        adapter = GitHubReleaseAdapter(GitHubProfile("owner/repo", True), TEST_ROOT, runner=runner)
        result = adapter.preview_artifact(request, artifact)
        self.assertEqual(result["identity"], "NEW")
        self.assertTrue(all(not (len(args) > 2 and args[1] == "release" and args[2] in {"create", "upload", "edit"}) for args in calls))

    def test_github_preview_marks_only_an_exact_draft_as_resumable(self):
        payload = b"exact draft resume bytes"
        digest = hashlib.sha256(payload).hexdigest()
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-draft-resume.1", "test")
        artifact = Artifact(
            "miexiang-test-publisher-0.1.0-draft-resume.1.bin",
            "fixture.bin",
            len(payload),
            digest,
            "application/octet-stream",
            TEST_ROOT / "fixture.bin",
        )
        sums_bytes = f"{digest}  {artifact.filename}\n".encode("ascii")
        draft = {
            "tag_name": "test-miexiang-generic-publish-miexiang-test-publisher-0.1.0-draft-resume.1",
            "draft": True,
            "assets": [
                {
                    "name": artifact.filename,
                    "size": len(payload),
                    "digest": f"sha256:{digest}",
                    "browser_download_url": "https://github.com/owner/repo/releases/download/untagged/test.bin",
                },
                {
                    "name": "SHA256SUMS.txt",
                    "size": len(sums_bytes),
                    "digest": f"sha256:{hashlib.sha256(sums_bytes).hexdigest()}",
                    "browser_download_url": "https://github.com/owner/repo/releases/download/untagged/SHA256SUMS.txt",
                },
            ],
        }
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[1:3] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="Logged in (keyring)", stderr="")
            if args[1] == "api" and args[2] == "repos/owner/repo":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"private": False, "default_branch": "main", "permissions": {"push": True}}), stderr="")
            if args[1] == "api" and "/releases/tags/" in args[2]:
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
            if args[1] == "api" and args[2] == "repos/owner/repo/releases":
                return SimpleNamespace(returncode=0, stdout=json.dumps([[draft]]), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        adapter = GitHubReleaseAdapter(GitHubProfile("owner/repo", True), TEST_ROOT, runner=runner)
        result = adapter.preview_artifact(request, artifact)

        self.assertEqual(result["identity"], "DRAFT_RESUMABLE")
        self.assertTrue(all(not (len(args) > 2 and args[1] == "release" and args[2] in {"create", "upload", "edit"}) for args in calls))

    def test_manifest_creation_uses_verified_public_noreply_identity(self):
        request = PublicationRequest("fixture", "1.0", "candidate")
        artifact = Artifact("fixture-1.0.bin", "fixture.bin", 1, hashlib.sha256(b"x").hexdigest(), "application/octet-stream", Path("synthetic.bin"))
        encoded = encode_manifest(build_manifest(request, artifact, [{"provider":"github-releases", "role":"mirror", "status":"VERIFIED", "url":"https://github.com/example/fixture/releases/download/fixture/fixture.bin", "published_at":"2026-01-01T00:00:00Z", "remote_verified_at":"2026-01-01T00:00:00Z", "verified_sha256":artifact.sha256}], "required"))
        adapter = GitHubReleaseAdapter(GitHubProfile("example/fixture", True, "release-manifests"), TEST_ROOT)
        calls = []
        def api(endpoint, args=None):
            calls.append((endpoint, args or []))
            if endpoint == "user":
                self.assertEqual(args, ["--jq", "{login,id}"])
                return {"login":"fixture-user", "id":1234}
            if args and "PUT" in args:
                return {"commit":{"sha":"a"*40}}
            raise PublishError("REMOTE_CONFLICT")
        def download(url, destination, expected_size, expected_sha256, allowed_hosts):
            return SimpleNamespace(verification=StreamVerification(expected_size, expected_sha256))
        with patch.object(adapter, "_api_json", side_effect=api), patch("miexiang_publish.providers.github_release.download_and_verify", side_effect=download):
            result = adapter.publish_manifest(request, encoded, hashlib.sha256(encoded).hexdigest())
        fields = next(args for endpoint, args in calls if "PUT" in args)
        self.assertIn("author[email]=1234+fixture-user@users.noreply.github.com", fields)
        self.assertIn("committer[email]=1234+fixture-user@users.noreply.github.com", fields)
        self.assertFalse(result["idempotent"])

    def test_github_manifest_idempotency_uses_a_read_only_commit_lookup(self):
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-manifest-retry.1", "test")
        artifact = Artifact(
            "miexiang-test-publisher-0.1.0-manifest-retry.1.bin",
            "fixture.bin",
            4,
            hashlib.sha256(b"test").hexdigest(),
            "application/octet-stream",
            Path("E:/fixture.bin"),
        )
        manifest_bytes = encode_manifest(build_manifest(
            request,
            artifact,
            providers=[{
                "provider": "github-releases",
                "role": "mirror",
                "status": "VERIFIED",
                "url": "https://github.com/owner/repo/releases/download/test/fixture.bin",
                "published_at": "2026-09-24T06:32:40Z",
                "remote_verified_at": "2026-09-24T06:33:00Z",
                "verified_sha256": artifact.sha256,
            }],
            mirror_policy="required",
            generated_at="2026-09-24T06:33:00Z",
            published_at="2026-09-24T06:32:40Z",
            verified_at="2026-09-24T06:33:00Z",
        ))
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        encoded_content = base64.b64encode(manifest_bytes).decode("ascii")
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == "api" and urllib.parse.urlparse(args[2]).path == "repos/owner/repo/contents/manifests/miexiang-test-publisher/test/0.1.0-manifest-retry.1/release-manifest.json":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"content": encoded_content}), stderr="")
            if args[1] == "api" and args[2].startswith("repos/owner/repo/commits?"):
                return SimpleNamespace(returncode=0, stdout=json.dumps([{"sha": "a" * 40}]), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        adapter = GitHubReleaseAdapter(GitHubProfile("owner/repo", True, "release-manifests"), TEST_ROOT, runner=runner)

        def fake_download(url, destination, expected_size, expected_sha256, allowed_hosts):
            return SimpleNamespace(
                final_host="raw.githubusercontent.com",
                verification=StreamVerification(expected_size, expected_sha256),
            )

        with patch("miexiang_publish.providers.github_release.download_and_verify", side_effect=fake_download):
            result = adapter.publish_manifest(request, manifest_bytes, manifest_digest)

        commit_lookup = next(args for args in calls if args[1] == "api" and "/commits" in args[2])
        parsed_lookup = urllib.parse.urlparse(commit_lookup[2])
        self.assertEqual(parsed_lookup.path, "repos/owner/repo/commits")
        self.assertEqual(
            urllib.parse.parse_qs(parsed_lookup.query),
            {"path": ["manifests/miexiang-test-publisher/test/0.1.0-manifest-retry.1/release-manifest.json"], "per_page": ["1"], "sha": ["release-manifests"]},
        )
        contents_lookup = next(args for args in calls if args[1] == "api" and "/contents/" in args[2])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(contents_lookup[2]).query), {"ref": ["release-manifests"]})
        self.assertNotIn("--field", commit_lookup)
        self.assertFalse(any("--method" in args and "PUT" in args for args in calls))
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["verified_sha256"], manifest_digest)

    def test_github_adapter_drafts_then_publishes_without_clobber_and_verifies_consumer_url(self):
        payload = b"github release test bytes"
        digest = hashlib.sha256(payload).hexdigest()
        source_temp = tempfile.TemporaryDirectory(prefix="github-provider-", dir=TEST_ROOT)
        self.addCleanup(source_temp.cleanup)
        source_path = Path(source_temp.name) / "fixture.bin"
        source_path.write_bytes(payload)
        request = PublicationRequest("miexiang-test-publisher", "0.1.0-probe.2", "test")
        artifact = Artifact("miexiang-test-publisher-0.1.0-probe.2.bin", "fixture.bin", len(payload), digest, "application/octet-stream", source_path)
        sums_bytes = f"{digest}  {artifact.filename}\n".encode("ascii")
        sums_digest = hashlib.sha256(sums_bytes).hexdigest()
        sums_asset = {
            "name": "SHA256SUMS.txt",
            "size": len(sums_bytes),
            "digest": f"sha256:{sums_digest}",
            "browser_download_url": "https://github.com/owner/repo/releases/download/test/SHA256SUMS.txt",
        }
        published = {
            "tag_name": "test-miexiang-generic-publish-miexiang-test-publisher-0.1.0-probe.2",
            "draft": False,
            "immutable": True,
            "published_at": "2026-09-24T01:00:00Z",
            "assets": [{
                "name": artifact.filename,
                "size": len(payload),
                "digest": f"sha256:{digest}",
                "browser_download_url": "https://github.com/owner/repo/releases/download/test/fixture.bin",
            }, sums_asset],
        }
        draft = dict(published, draft=True)
        calls = []
        uploaded = False
        published_state = False

        def runner(args, **kwargs):
            nonlocal uploaded, published_state
            calls.append(args)
            if args[1:3] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="Logged in (keyring)", stderr="")
            if args[1] == "api" and args[2] == "repos/owner/repo":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"private": False, "default_branch": "main", "permissions": {"push": True}}), stderr="")
            if args[1] == "api" and "/releases/tags/" in args[2]:
                if not published_state:
                    return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
                return SimpleNamespace(returncode=0, stdout=json.dumps(published), stderr="")
            if args[1] == "api" and args[2] == "repos/owner/repo/releases":
                states = [published if published_state else draft] if uploaded else []
                return SimpleNamespace(returncode=0, stdout=json.dumps([states]), stderr="")
            if args[1:3] == ["release", "upload"]:
                uploaded = True
            if args[1:3] == ["release", "edit"]:
                published_state = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        adapter = GitHubReleaseAdapter(GitHubProfile("owner/repo", True), TEST_ROOT, runner=runner)
        verified_downloads = []

        def fake_download(url, destination, expected_size, expected_sha256, allowed_hosts):
            verified_downloads.append((url, expected_size, expected_sha256, allowed_hosts))
            host = "github.com" if "github.com/" in url else "objects.githubusercontent.com"
            return SimpleNamespace(final_host=host, verification=StreamVerification(expected_size, expected_sha256))

        with patch("miexiang_publish.providers.github_release.download_and_verify", side_effect=fake_download):
            result = adapter.publish_artifact(request, artifact)

        upload_commands = [args for args in calls if len(args) > 2 and args[1:3] == ["release", "upload"]]
        self.assertEqual(len(upload_commands), 1)
        self.assertNotIn("--clobber", upload_commands[0])
        artifact_upload_paths = [arg for arg in upload_commands[0] if arg.lower().endswith(".bin")]
        self.assertEqual([Path(arg).name for arg in artifact_upload_paths], [artifact.filename])
        self.assertIn("SHA256SUMS.txt", [Path(arg).name for arg in upload_commands[0] if arg.lower().endswith(".txt")])
        self.assertTrue(any(args[1:3] == ["release", "create"] and "--draft" in args for args in calls))
        self.assertTrue(any(args[1:3] == ["release", "edit"] and "--draft=false" in args for args in calls))
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["immutable_enforced"])
        self.assertEqual(result["verified_sha256"], digest)
        self.assertEqual(result["checksums_sha256"], sums_digest)
        self.assertEqual(len(verified_downloads), 2)
        self.assertEqual({entry[2] for entry in verified_downloads}, {digest, sums_digest})


if __name__ == "__main__":
    unittest.main()
