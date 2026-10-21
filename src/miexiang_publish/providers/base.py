from __future__ import annotations

from typing import Any, Protocol

from ..models import Artifact, PublicationRequest


class ProviderAdapter(Protocol):
    provider: str
    role: str

    def preflight(self) -> dict[str, Any]: ...

    def publish_artifact(self, request: PublicationRequest, artifact: Artifact) -> dict[str, Any]: ...

    def publish_manifest(self, request: PublicationRequest, manifest_bytes: bytes, manifest_sha256: str) -> dict[str, Any]: ...
