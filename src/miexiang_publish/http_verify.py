from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import PublishError
from .models import StreamVerification, verify_stream


@dataclass(frozen=True)
class DownloadReceipt:
    final_url: str
    final_host: str
    http_status: int
    content_length: int | None
    verification: StreamVerification


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...] | None):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_initial_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_initial_url(url: str, allowed_hosts: tuple[str, ...] | None) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublishError("DOWNLOAD_FAILED", "consumer URL must use HTTPS")
    host = parsed.hostname.lower()
    if allowed_hosts and not any(host == suffix or host.endswith("." + suffix) for suffix in allowed_hosts):
        raise PublishError("DOWNLOAD_FAILED", "consumer URL host is outside the provider allowlist")


def _copy_bounded(source, destination, expected_size: int) -> int:
    if expected_size < 0:
        raise PublishError("DOWNLOAD_FAILED", "expected artifact size is invalid")
    copied = 0
    while True:
        block = source.read(min(1024 * 1024, expected_size - copied + 1))
        if not block:
            return copied
        copied += len(block)
        if copied > expected_size:
            raise PublishError("HASH_MISMATCH", "consumer response exceeds the expected artifact size")
        destination.write(block)


def download_and_verify(
    url: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
    allowed_hosts: tuple[str, ...] | None = None,
) -> DownloadReceipt:
    _check_initial_url(url, allowed_hosts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_HttpsOnlyRedirects(allowed_hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "MiexiangGenericPublisher/0.1"})
    try:
        with opener.open(request, timeout=60) as response, destination.open("wb") as output:
            status = int(response.status)
            content_length_value = response.headers.get("Content-Length")
            content_length = int(content_length_value) if content_length_value and content_length_value.isdigit() else None
            if status != 200:
                raise PublishError("DOWNLOAD_FAILED", "consumer URL did not return HTTP 200")
            if content_length is not None and content_length != expected_size:
                raise PublishError("HASH_MISMATCH", "consumer response length differs from the local artifact")
            _copy_bounded(response, output, expected_size)
            output.flush()
        with destination.open("rb") as downloaded:
            verification = verify_stream(downloaded, expected_size, expected_sha256)
        final_url = response.geturl()
        final_host = (urllib.parse.urlparse(final_url).hostname or "").lower()
        _check_initial_url(final_url, allowed_hosts)
        return DownloadReceipt(final_url, final_host, status, content_length, verification)
    except PublishError:
        destination.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, OSError, ValueError):
        destination.unlink(missing_ok=True)
        raise PublishError("DOWNLOAD_FAILED", "consumer URL download failed") from None
