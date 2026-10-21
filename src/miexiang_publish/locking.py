from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import PublishError
from . import paths


@dataclass
class IdentityLock:
    product_id: str
    channel: str
    version: str
    _path: Path | None = None
    _nonce: str | None = None

    def acquire(self) -> "IdentityLock":
        paths.ensure_runtime_roots()
        identity = f"{self.product_id}\n{self.channel}\n{self.version}".encode("utf-8")
        name = hashlib.sha256(identity).hexdigest() + ".lock"
        path = paths.LOCK_ROOT / name
        nonce = secrets.token_hex(16)
        record = json.dumps({"nonce": nonce, "pid": os.getpid(), "created_unix": int(time.time())}).encode("ascii")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PublishError("LOCK_HELD", "another local publish run holds this version identity") from None
        try:
            with os.fdopen(descriptor, "wb") as lock_file:
                lock_file.write(record)
                lock_file.flush()
                os.fsync(lock_file.fileno())
        except OSError:
            try:
                path.unlink(missing_ok=True)
            finally:
                raise PublishError("LOCK_HELD", "could not establish the local version lock") from None
        self._path, self._nonce = path, nonce
        return self

    def release(self) -> None:
        if self._path is None or self._nonce is None:
            return
        try:
            current = json.loads(self._path.read_text(encoding="ascii"))
            if current.get("nonce") == self._nonce:
                self._path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
        self._path = None
        self._nonce = None

    def __enter__(self) -> "IdentityLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
