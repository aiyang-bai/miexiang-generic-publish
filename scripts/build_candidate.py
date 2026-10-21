"""Build using the backend pinned in requirements-build.lock."""
from pathlib import Path
import os
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
for operation in ("build_sdist", "build_wheel"):
    subprocess.run([sys.executable, "-c", f"from setuptools.build_meta import {operation}; {operation}('dist')"], cwd=root, check=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
