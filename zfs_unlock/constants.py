"""Shared constants for zfs-unlock."""

from __future__ import annotations

import re
from pathlib import Path

DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")
COMMAND_TIMEOUT_RETURNCODE = 124
COMMAND_STARTUP_ERROR_RETURNCODE = 127

# The receiver runs as root; bound what an SSH client can make it read.
MAX_PASSPHRASE_BYTES = 64 * 1024
RECEIVER_STDIN_TIMEOUT_SECONDS = 30.0

CONFIG_SEARCH_PATHS = [
    Path("config.yaml"),
    Path("config.yml"),
    Path.home() / ".config" / "zfs-unlock" / "config.yaml",
    Path.home() / ".config" / "zfs-unlock" / "config.yml",
]
DEFAULT_IDENTITY_FILE = Path("~/.ssh/zfs-unlock-receiver")

EXAMPLE_CONFIG = """\
host: zfs-host.example.lan
user: zfs-unlock
# port: 22
# identity_file: ~/.ssh/zfs-unlock-receiver
# connect_timeout: 5
# command_timeout: 30
# secrets: auto  # auto (default), files, or inline

datasets:
  tank/syncthing: ~/.secrets/syncthing-key
  tank/photos: my-literal-passphrase
"""
