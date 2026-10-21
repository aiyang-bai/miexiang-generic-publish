from __future__ import annotations

import shutil
import uuid
from dataclasses import replace
from pathlib import Path

from .errors import PublishError
from .models import Artifact, hash_file
from . import paths


def stage_snapshot(artifact: Artifact) -> Artifact:
    paths.ensure_runtime_roots()
    required_free = artifact.size_bytes * 2 + 16 * 1024 * 1024
    try:
        free = shutil.disk_usage(paths.STAGING_ROOT).free
    except OSError:
        raise PublishError("POLICY_REJECTED", "configured staging storage is unavailable") from None
    if free < required_free:
        raise PublishError("POLICY_REJECTED", "insufficient free space for a safe upload and readback")
    snapshot = paths.STAGING_ROOT / f"{uuid.uuid4().hex}-{artifact.filename}"
    try:
        before = artifact.source_path.stat()
        shutil.copyfile(artifact.source_path, snapshot)
        after = artifact.source_path.stat()
    except OSError:
        snapshot.unlink(missing_ok=True)
        raise PublishError("ARTIFACT_INVALID", "artifact could not be copied to configured staging") from None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        snapshot.unlink(missing_ok=True)
        raise PublishError("ARTIFACT_INVALID", "source artifact changed while a publication snapshot was made")
    staged = hash_file(snapshot)
    if staged.size_bytes != artifact.size_bytes or staged.sha256 != artifact.sha256:
        snapshot.unlink(missing_ok=True)
        raise PublishError("HASH_MISMATCH", "staged snapshot differs from the identified source artifact")
    return replace(artifact, source_path=snapshot)
