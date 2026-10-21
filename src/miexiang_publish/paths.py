"""Runtime roots are independent of the installed package; configure before concurrent use."""
from __future__ import annotations
import os
import sys
from pathlib import Path
from .errors import PublishError


def default_data_root() -> Path:
    override = os.environ.get("MIEXIANG_PUBLISH_DATA_DIR")
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "miexiang-publish"


def configure_runtime(data_dir: str | Path | None = None) -> None:
    """Set once at process startup. Do not change while publishing in another thread."""
    global RUNTIME_ROOT, STAGING_ROOT, LOCK_ROOT, PROFILE_PATH
    RUNTIME_ROOT = Path(data_dir).expanduser().absolute() if data_dir is not None else default_data_root()
    STAGING_ROOT = RUNTIME_ROOT / "staging"
    LOCK_ROOT = RUNTIME_ROOT / "locks"
    PROFILE_PATH = RUNTIME_ROOT / "profile.json"


def assert_runtime_path(path: Path) -> None:
    candidate = path.absolute()
    if not candidate.is_relative_to(RUNTIME_ROOT.absolute()):
        raise PublishError("POLICY_REJECTED", "runtime path is outside the configured data root")
    try:
        for component in (candidate, *candidate.parents):
            if component.is_symlink() or (component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400):
                raise PublishError("POLICY_REJECTED", "runtime path may not traverse links or reparse points")
    except OSError:
        raise PublishError("POLICY_REJECTED", "runtime path cannot be inspected") from None


def ensure_runtime_directory(path: Path) -> None:
    assert_runtime_path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PublishError("POLICY_REJECTED", "runtime directory is unavailable or not writable") from None
    assert_runtime_path(path)


def ensure_runtime_roots() -> None:
    for path in (RUNTIME_ROOT, STAGING_ROOT, LOCK_ROOT):
        ensure_runtime_directory(path)

configure_runtime()
