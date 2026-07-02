"""Tests for the SSH unlock client."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from zfs_unlock.client import SubprocessRunner, ZfsUnlockClient
from zfs_unlock.config import Config, Dataset
from zfs_unlock.constants import (
    COMMAND_STARTUP_ERROR_RETURNCODE,
    COMMAND_TIMEOUT_RETURNCODE,
)
from zfs_unlock.process import CommandResult

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

CUSTOM_SSH_PORT = 2222
PRIVATE_DIR_MODE = 0o700


class RecordingRunner:
    """Async runner that records calls and returns queued results."""

    def __init__(self, *results: CommandResult) -> None:
        """Initialize with queued command results."""
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None, float | None]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,
    ) -> CommandResult:
        """Record a command and return the next queued result."""
        self.calls.append((args, input_text, command_timeout))
        if not self.results:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self.results.pop(0)


def test_run_remote_builds_ssh_command_with_identity_file(tmp_path: Path) -> None:
    """run_remote builds a restricted OpenSSH command."""
    identity_file = tmp_path / "zfs-unlock-key"
    config = Config(
        host="zfs-host.example.lan",
        user="unlocker",
        port=CUSTOM_SSH_PORT,
        identity_file=identity_file,
        command_timeout=12,
        datasets=[],
    )
    runner = RecordingRunner(CommandResult(returncode=0, stdout="unlocked\n", stderr=""))
    with patch("zfs_unlock.client._ssh_control_dir", return_value=None):
        client = ZfsUnlockClient(config, runner=runner)

    result = asyncio.run(client.run_remote(["status", "tank/photos"]))

    assert result.stdout == "unlocked\n"
    assert runner.calls == [
        (
            [
                "ssh",
                "-p",
                str(CUSTOM_SSH_PORT),
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-n",
                "-o",
                "IdentitiesOnly=yes",
                "-i",
                str(identity_file),
                "unlocker@zfs-host.example.lan",
                "status tank/photos",
            ],
            None,
            12,
        ),
    ]


def test_ssh_args_enable_multiplexing_in_private_control_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH connection reuse keeps its control sockets in a 0700 directory."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    config = Config(host="zfs-host.example.lan", datasets=[])
    runner = RecordingRunner()
    client = ZfsUnlockClient(config, runner=runner)

    asyncio.run(client.run_remote(["status", "tank/photos"]))

    control_dir = tmp_path / "zfs-unlock"
    assert control_dir.is_dir()
    assert control_dir.stat().st_mode & 0o777 == PRIVATE_DIR_MODE
    args = runner.calls[0][0]
    assert "ControlMaster=auto" in args
    assert f"ControlPath={control_dir}/%C" in args
    assert "ControlPersist=15s" in args


def test_ssh_args_skip_multiplexing_without_private_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """No multiplexing options are used when no private control dir exists."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/no-permission/here")
    monkeypatch.setattr("pathlib.Path.mkdir", _raise_oserror)
    config = Config(host="zfs-host.example.lan", datasets=[])
    runner = RecordingRunner()
    client = ZfsUnlockClient(config, runner=runner)

    asyncio.run(client.run_remote(["status", "tank/photos"]))

    assert not any("ControlMaster" in arg for arg in runner.calls[0][0])


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError


def test_unlock_keeps_stdin_open_for_passphrase() -> None:
    """Unlock must not pass -n because the passphrase is sent over stdin."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner(CommandResult(returncode=0, stdout="unlocked tank/photos\n", stderr=""))
    client = ZfsUnlockClient(config, runner=runner)

    assert asyncio.run(client.unlock(config.datasets[0])) is True

    args, input_text, _ = runner.calls[0]
    assert "-n" not in args
    assert input_text == "secret-pass\n"


def test_subprocess_runner_returns_timeout_for_hanging_command() -> None:
    """SubprocessRunner returns a timeout result instead of hanging forever."""
    runner = SubprocessRunner()

    result = asyncio.run(
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], command_timeout=0.01),
    )

    assert result.returncode == COMMAND_TIMEOUT_RETURNCODE
    assert "timed out after 0.01s" in result.stderr


def test_subprocess_runner_returns_error_for_missing_executable() -> None:
    """SubprocessRunner reports startup errors as command results."""
    runner = SubprocessRunner()

    result = asyncio.run(runner.run(["/definitely/missing/zfs-unlock-test-binary"]))

    assert result.returncode == COMMAND_STARTUP_ERROR_RETURNCODE
    assert "missing/zfs-unlock-test-binary" in result.stderr


def test_is_locked_maps_receiver_status() -> None:
    """is_locked maps receiver stdout to lock status."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="pass")])
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="boom"),
    )
    client = ZfsUnlockClient(config, runner=runner)
    dataset = config.datasets[0]

    assert asyncio.run(client.is_locked(dataset)) is True
    assert asyncio.run(client.is_locked(dataset)) is False
    assert asyncio.run(client.is_locked(dataset)) is None


def test_unlock_sends_passphrase_over_stdin() -> None:
    """Unlock sends the dataset passphrase to the receiver over stdin."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner(CommandResult(returncode=0, stdout="unlocked tank/photos\n", stderr=""))
    with patch("zfs_unlock.client._ssh_control_dir", return_value=None):
        client = ZfsUnlockClient(config, runner=runner)

    assert asyncio.run(client.unlock(config.datasets[0])) is True

    assert runner.calls == [
        (
            [
                "ssh",
                "-p",
                "22",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "zfs-unlock@zfs-host.example.lan",
                "unlock tank/photos",
            ],
            "secret-pass\n",
            30,
        ),
    ]


def test_lock_uses_force_flag() -> None:
    """Lock passes --force to the receiver only when requested."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked tank/photos\n", stderr=""))
    client = ZfsUnlockClient(config, runner=runner)

    assert asyncio.run(client.lock(config.datasets[0], force=True)) is True

    assert runner.calls[0][0][-1] == "lock tank/photos --force"
