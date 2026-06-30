"""Tests for the SSH unlock client."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from zfs_unlock import (
    COMMAND_STARTUP_ERROR_RETURNCODE,
    COMMAND_TIMEOUT_RETURNCODE,
    CommandResult,
    Config,
    Dataset,
    SubprocessRunner,
    ZfsUnlockClient,
)

if TYPE_CHECKING:
    from pathlib import Path

CUSTOM_SSH_PORT = 2222


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
