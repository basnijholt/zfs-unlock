"""Tests for the SSH unlock client."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from zfs_unlock.client import SubprocessRunner, UnlockOutcome, ZfsUnlockClient, run_unlock
from zfs_unlock.config import Config, Dataset
from zfs_unlock.constants import (
    COMMAND_STARTUP_ERROR_RETURNCODE,
    COMMAND_TIMEOUT_RETURNCODE,
    SSH_CONNECTION_ERROR_RETURNCODE,
)
from zfs_unlock.process import CommandResult

if TYPE_CHECKING:
    from pathlib import Path

CUSTOM_SSH_PORT = 2222

# Child process that records its own pid, then hangs until killed.
_PID_THEN_HANG = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"


def _pid_alive(pid: int) -> bool:
    """Return whether a process with this pid still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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
                "StrictHostKeyChecking=ask",
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


def test_ssh_pins_strict_host_key_checking() -> None:
    """Every ssh invocation pins StrictHostKeyChecking=ask on the command line.

    Passphrase secrecy depends on host-key verification failing closed; the
    command-line option overrides a user ssh_config with accept-new/no that
    would otherwise silently trust a first-contact (MITM) host key.
    """
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner()
    client = ZfsUnlockClient(config, runner=runner)

    asyncio.run(client.is_locked(config.datasets[0]))
    asyncio.run(client.unlock(config.datasets[0]))
    asyncio.run(client.lock(config.datasets[0]))

    for args, _, _ in runner.calls:
        assert "StrictHostKeyChecking=ask" in args


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals")
def test_subprocess_runner_kills_child_on_timeout(tmp_path: Path) -> None:
    """A timed-out child is killed, not left running behind the timeout result."""
    pid_file = tmp_path / "child.pid"
    runner = SubprocessRunner()

    result = asyncio.run(
        runner.run([sys.executable, "-c", _PID_THEN_HANG, str(pid_file)], command_timeout=2.0),
    )

    assert result.returncode == COMMAND_TIMEOUT_RETURNCODE
    assert not _pid_alive(int(pid_file.read_text()))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals")
def test_subprocess_runner_kills_child_on_cancel(tmp_path: Path) -> None:
    """Cancelling an in-flight run kills the child instead of orphaning it."""
    pid_file = tmp_path / "child.pid"

    async def scenario() -> int:
        runner = SubprocessRunner()
        task = asyncio.create_task(runner.run([sys.executable, "-c", _PID_THEN_HANG, str(pid_file)]))
        while not (pid_file.exists() and pid_file.read_text()):  # noqa: ASYNC110 -- polling a file written by the child
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pid_file.read_text())

    pid = asyncio.run(scenario())

    assert not _pid_alive(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
def test_subprocess_runner_timeout_returns_despite_inherited_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grandchild holding the stdout pipe open must not stall the timeout path.

    ssh ProxyCommand helpers inherit the pipes; draining them unbounded after
    kill() would block until the helper exits, defeating command_timeout.
    """
    monkeypatch.setattr("zfs_unlock.process._KILL_DRAIN_TIMEOUT", 0.2)
    grandchild_holds_pipe = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "time.sleep(60)"
    )
    runner = SubprocessRunner()

    start = time.monotonic()
    result = asyncio.run(runner.run([sys.executable, "-c", grandchild_holds_pipe], command_timeout=0.5))
    elapsed = time.monotonic() - start

    assert result.returncode == COMMAND_TIMEOUT_RETURNCODE
    assert elapsed < 10, "post-kill drain must be bounded, not wait for the grandchild"  # noqa: PLR2004


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
        CommandResult(returncode=255, stdout="", stderr="Connection refused"),
    )
    client = ZfsUnlockClient(config, runner=runner)
    dataset = config.datasets[0]

    assert asyncio.run(client.is_locked(dataset)).locked is True
    assert asyncio.run(client.is_locked(dataset)).locked is False

    receiver_error = asyncio.run(client.is_locked(dataset))
    assert receiver_error.locked is None
    assert receiver_error.connection_error is False

    connection_error = asyncio.run(client.is_locked(dataset))
    assert connection_error.locked is None
    assert connection_error.connection_error is True


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
                "StrictHostKeyChecking=ask",
                "-o",
                "ConnectTimeout=5",
                "zfs-unlock@zfs-host.example.lan",
                "unlock tank/photos",
            ],
            "secret-pass\n",
            30,
        ),
    ]


@pytest.mark.parametrize(
    "returncode",
    [SSH_CONNECTION_ERROR_RETURNCODE, COMMAND_TIMEOUT_RETURNCODE, COMMAND_STARTUP_ERROR_RETURNCODE],
)
def test_is_locked_flags_every_connection_error_returncode(returncode: int) -> None:
    """SSH failure (255), timeout (124), and startup error (127) are all connection errors."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="pass")])
    runner = RecordingRunner(CommandResult(returncode=returncode, stdout="", stderr="unreachable\n"))
    client = ZfsUnlockClient(config, runner=runner)

    status = asyncio.run(client.is_locked(config.datasets[0], quiet=True))

    assert status.locked is None
    assert status.connection_error is True


@pytest.mark.parametrize(
    "returncode",
    [SSH_CONNECTION_ERROR_RETURNCODE, COMMAND_TIMEOUT_RETURNCODE, COMMAND_STARTUP_ERROR_RETURNCODE],
)
def test_run_unlock_reports_unreachable_for_every_connection_error_returncode(returncode: int) -> None:
    """Timeouts and startup errors must trigger UNREACHABLE (panic mode), not FAILED."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="pass")])
    runner = RecordingRunner(CommandResult(returncode=returncode, stdout="", stderr="unreachable\n"))

    assert asyncio.run(run_unlock(config, quiet=True, runner=runner)) is UnlockOutcome.UNREACHABLE


def test_lock_uses_force_flag() -> None:
    """Lock passes --force to the receiver only when requested."""
    config = Config(host="zfs-host.example.lan", datasets=[Dataset(path="tank/photos", secret="secret-pass")])
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked tank/photos\n", stderr=""))
    client = ZfsUnlockClient(config, runner=runner)

    assert asyncio.run(client.lock(config.datasets[0], force=True)) is True

    assert runner.calls[0][0][-1] == "lock tank/photos --force"
