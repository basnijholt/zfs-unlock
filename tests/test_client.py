"""Tests for the SSH unlock client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from zfs_unlock import CommandResult, Config, Dataset, ZfsUnlockClient

if TYPE_CHECKING:
    from pathlib import Path

CUSTOM_SSH_PORT = 2222


class RecordingRunner:
    """Async runner that records calls and returns queued results."""

    def __init__(self, *results: CommandResult) -> None:
        """Initialize with queued command results."""
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Record a command and return the next queued result."""
        self.calls.append((args, input_text))
        if not self.results:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self.results.pop(0)


def test_run_remote_builds_ssh_command_with_identity_file(tmp_path: Path) -> None:
    """run_remote builds a restricted OpenSSH command."""
    identity_file = tmp_path / "zfs-unlock-key"
    config = Config(
        host="nas.local",
        user="unlocker",
        port=CUSTOM_SSH_PORT,
        identity_file=identity_file,
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
                "-i",
                str(identity_file),
                "unlocker@nas.local",
                "status tank/photos",
            ],
            None,
        ),
    ]


def test_is_locked_maps_receiver_status() -> None:
    """is_locked maps receiver stdout to lock status."""
    config = Config(host="nas.local", datasets=[Dataset(path="tank/photos", secret="pass")])
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
    config = Config(host="nas.local", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
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
                "zfs-unlock@nas.local",
                "unlock tank/photos",
            ],
            "secret-pass\n",
        ),
    ]


def test_lock_uses_force_flag() -> None:
    """Lock passes --force to the receiver only when requested."""
    config = Config(host="nas.local", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked tank/photos\n", stderr=""))
    client = ZfsUnlockClient(config, runner=runner)

    assert asyncio.run(client.lock(config.datasets[0], force=True)) is True

    assert runner.calls[0][0][-1] == "lock tank/photos --force"
