"""Version discovery."""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("zfs-unlock")
except importlib.metadata.PackageNotFoundError:
    try:
        from ._version import __version__
    except ImportError:
        __version__ = "unknown"
