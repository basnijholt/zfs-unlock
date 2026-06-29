"""Integration-style tests for the full unlock flow with mocked SSH."""

from __future__ import annotations

import asyncio

from zfs_unlock import CommandResult, Config, Dataset, run_lock, run_status, run_unlock


class RecordingRunner:
    """Async runner that records calls and returns queued results."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        self.calls.append((args, input_text))
        if not self.results:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self.results.pop(0)


def test_run_unlock_unlocks_only_locked_datasets() -> None:
    """run_unlock unlocks locked datasets and skips available ones."""
    config = Config(
        host="nas.local",
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

    assert asyncio.run(run_unlock(config, runner=runner)) is True

    remote_commands = [call[0][-1] for call in runner.calls]
    assert remote_commands == [
        "status tank/locked",
        "status tank/open",
        "unlock tank/locked",
    ]
    assert runner.calls[2][1] == "pass1\n"


def test_run_unlock_returns_false_when_any_status_fails() -> None:
    """run_unlock returns False when a dataset status check fails."""
    config = Config(
        host="nas.local",
        datasets=[
            Dataset(path="tank/locked", secret="pass1"),
            Dataset(path="tank/open", secret="pass2"),
        ],
    )
    runner = RecordingRunner(
        CommandResult(returncode=1, stdout="", stderr="ssh failed"),
        CommandResult(returncode=0, stdout="unlocked\n", stderr=""),
    )

    assert asyncio.run(run_unlock(config, runner=runner)) is False


def test_run_lock_locks_only_unlocked_datasets() -> None:
    """run_lock skips already locked datasets."""
    config = Config(
        host="nas.local",
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

    asyncio.run(run_lock(config, force=True, runner=runner))

    assert [call[0][-1] for call in runner.calls] == [
        "status tank/open",
        "status tank/locked",
        "lock tank/open --force",
    ]


def test_run_status_checks_all_matching_datasets() -> None:
    """run_status checks all datasets that match the filter."""
    config = Config(
        host="nas.local",
        datasets=[
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/media", secret="pass2"),
        ],
    )
    runner = RecordingRunner(CommandResult(returncode=0, stdout="locked\n", stderr=""))

    asyncio.run(run_status(config, dataset_filters=["photos"], runner=runner))

    assert [call[0][-1] for call in runner.calls] == ["status tank/photos"]
