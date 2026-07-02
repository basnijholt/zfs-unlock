"""Integration-style tests for the full unlock flow with mocked SSH."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from zfs_unlock.client import UnlockOutcome, run_lock, run_status, run_unlock
from zfs_unlock.config import Config, Dataset, SecretsMode
from zfs_unlock.constants import SSH_CONNECTION_ERROR_RETURNCODE
from zfs_unlock.process import CommandResult

if TYPE_CHECKING:
    from pathlib import Path


class RecordingRunner:
    """Async runner that records calls and returns queued results."""

    def __init__(self, *results: CommandResult) -> None:
        """Initialize with queued command results."""
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,  # noqa: ARG002
    ) -> CommandResult:
        """Record a command and return the next queued result."""
        self.calls.append((args, input_text))
        if not self.results:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self.results.pop(0)


class CancellingRunner:
    """Async runner that simulates a cancelled SSH status command."""

    def __init__(self) -> None:
        """Initialize an empty call log."""
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,  # noqa: ARG002
    ) -> CommandResult:
        """Record a command and raise cancellation."""
        self.calls.append((args, input_text))
        raise asyncio.CancelledError


def test_run_unlock_unlocks_only_locked_datasets() -> None:
    """run_unlock unlocks locked datasets and skips available ones."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/locked", secret="pass1"),
            Dataset(path="tank/open", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
        CommandResult(returncode=0, stdout="unlocked tank/locked\n", stderr=""),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.OK

    remote_commands = [call[0][-1] for call in runner.calls]
    assert remote_commands == [
        "status tank/locked",
        "status tank/open",
        "unlock tank/locked",
    ]
    assert runner.calls[2][1] == "pass1\n"


def test_run_unlock_still_unlocks_other_datasets_when_one_status_fails() -> None:
    """A failed status check for one dataset does not block unlocking the rest."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/broken", secret="pass1"),
            Dataset(path="tank/locked", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=1, stdout="", stderr="receiver error"),
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="unlocked tank/locked\n", stderr=""),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.FAILED

    assert [call[0][-1] for call in runner.calls] == [
        "status tank/broken",
        "status tank/locked",
        "unlock tank/locked",
    ]


def test_run_unlock_attempts_remaining_datasets_after_unlock_failure() -> None:
    """A failed unlock for one dataset does not skip the remaining datasets."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/first", secret="pass1"),
            Dataset(path="tank/second", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="wrong passphrase\n"),
        CommandResult(returncode=0, stdout="unlocked tank/second\n", stderr=""),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.FAILED

    assert [call[0][-1] for call in runner.calls] == [
        "status tank/first",
        "status tank/second",
        "unlock tank/first",
        "unlock tank/second",
    ]


def test_run_unlock_reports_unreachable_when_all_connections_fail() -> None:
    """Connection-level failures for every dataset report an unreachable host."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/one", secret="pass1"),
            Dataset(path="tank/two", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=SSH_CONNECTION_ERROR_RETURNCODE, stdout="", stderr="Connection refused\n"),
        CommandResult(returncode=SSH_CONNECTION_ERROR_RETURNCODE, stdout="", stderr="Connection refused\n"),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.UNREACHABLE

    assert [call[0][-1] for call in runner.calls] == ["status tank/one", "status tank/two"]


def test_run_unlock_reports_failed_when_only_some_connections_fail() -> None:
    """Partial connection failures are operation failures, not an unreachable host."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/one", secret="pass1"),
            Dataset(path="tank/two", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=SSH_CONNECTION_ERROR_RETURNCODE, stdout="", stderr="Connection refused\n"),
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.FAILED


def test_run_unlock_returns_failed_when_file_secret_is_missing(tmp_path: Path) -> None:
    """run_unlock reports missing file-backed secrets instead of raising."""
    config = Config(
        host="zfs-host.example.lan",
        secrets=SecretsMode.FILES,
        datasets=[Dataset(path="tank/locked", secret=str(tmp_path / "missing.key"))],
    )
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked\n", stderr=""))

    assert asyncio.run(run_unlock(config, runner=runner)) is UnlockOutcome.FAILED

    assert [call[0][-1] for call in runner.calls] == ["status tank/locked"]


def test_run_unlock_returns_failed_when_filter_matches_nothing() -> None:
    """run_unlock reports explicit filters that match no configured datasets."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/photos", secret="pass1")],
    )
    runner = RecordingRunner()

    assert asyncio.run(run_unlock(config, dataset_filters=["missing"], runner=runner)) is UnlockOutcome.FAILED

    assert runner.calls == []


def test_run_lock_locks_only_unlocked_datasets() -> None:
    """run_lock skips already locked datasets."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/open", secret="pass1"),
            Dataset(path="tank/locked", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="locked tank/open\n", stderr=""),
    )

    assert asyncio.run(run_lock(config, force=True, runner=runner)) is True

    assert [call[0][-1] for call in runner.calls] == [
        "status tank/open",
        "status tank/locked",
        "lock tank/open --force",
    ]


def test_run_lock_returns_false_when_status_fails() -> None:
    """run_lock reports status check failures to callers."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/open", secret="pass1")],
    )
    runner = RecordingRunner(CommandResult(returncode=1, stdout="", stderr="ssh failed\n"))

    assert asyncio.run(run_lock(config, runner=runner)) is False

    assert [call[0][-1] for call in runner.calls] == ["status tank/open"]


def test_run_lock_returns_false_when_lock_fails() -> None:
    """run_lock reports lock command failures to callers."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/open", secret="pass1")],
    )
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="busy\n"),
    )

    assert asyncio.run(run_lock(config, runner=runner)) is False

    assert [call[0][-1] for call in runner.calls] == [
        "status tank/open",
        "lock tank/open",
    ]


def test_run_lock_returns_false_when_filter_matches_nothing() -> None:
    """run_lock reports explicit filters that match no configured datasets."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/photos", secret="pass1")],
    )
    runner = RecordingRunner()

    assert asyncio.run(run_lock(config, dataset_filters=["missing"], runner=runner)) is False

    assert runner.calls == []


def test_run_status_checks_all_matching_datasets() -> None:
    """run_status checks all datasets that match the filter."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/media", secret="pass2"),
        ],
    )
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked\n", stderr=""))

    assert asyncio.run(run_status(config, dataset_filters=["tank/photos"], runner=runner)) is True

    assert [call[0][-1] for call in runner.calls] == ["status tank/photos"]


def test_run_status_supports_glob_filters() -> None:
    """Glob filters select every matching dataset."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/media", secret="pass2"),
            Dataset(path="ssd/frigate", secret="pass3"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
        CommandResult(returncode=0, stdout="locked\n", stderr=""),
    )

    assert asyncio.run(run_status(config, dataset_filters=["tank/*"], runner=runner)) is True

    assert [call[0][-1] for call in runner.calls] == ["status tank/photos", "status tank/media"]


def test_run_status_returns_false_for_unknown_status() -> None:
    """run_status reports unknown receiver status to callers."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/plain", secret="pass1")],
    )
    runner = RecordingRunner(CommandResult(returncode=0, stdout="unknown\n", stderr=""))

    assert asyncio.run(run_status(config, runner=runner)) is False

    assert [call[0][-1] for call in runner.calls] == ["status tank/plain"]


def test_run_status_returns_false_when_status_task_is_cancelled() -> None:
    """run_status treats cancelled status tasks as unknown status."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/plain", secret="pass1")],
    )
    runner = CancellingRunner()

    assert asyncio.run(run_status(config, runner=runner)) is False

    assert [call[0][-1] for call in runner.calls] == ["status tank/plain"]


def test_run_status_returns_false_when_filter_matches_nothing() -> None:
    """run_status reports explicit filters that match no configured datasets."""
    config = Config(
        host="zfs-host.example.lan",
        datasets=[Dataset(path="tank/photos", secret="pass1")],
    )
    runner = RecordingRunner()

    assert asyncio.run(run_status(config, dataset_filters=["missing"], runner=runner)) is False

    assert runner.calls == []
