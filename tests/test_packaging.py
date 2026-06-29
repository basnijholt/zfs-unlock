"""Packaging tests for installed zfs-unlock wheels."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_built_wheel_reports_distribution_version(tmp_path: Path) -> None:
    """Installed wheels expose the same version through the CLI module."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")

    repo = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    wheels = list(dist.glob("zfs_unlock-*.whl"))
    assert len(wheels) == 1

    check = subprocess.run(
        [
            uv,
            "run",
            "--isolated",
            "--with",
            str(wheels[0]),
            "python",
            "-c",
            (
                "import importlib.metadata;"
                "import zfs_unlock;"
                "assert zfs_unlock.__version__ == importlib.metadata.version('zfs-unlock'), "
                "(zfs_unlock.__version__, importlib.metadata.version('zfs-unlock'))"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
