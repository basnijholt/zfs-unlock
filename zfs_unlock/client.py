"""Client operations for remote OpenZFS datasets."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .constants import (
    COMMAND_STARTUP_ERROR_RETURNCODE,
    COMMAND_TIMEOUT_RETURNCODE,
    SSH_CONNECTION_ERROR_RETURNCODE,
)
from .output import console, err_console
from .process import CommandResult, CommandRunner, SubprocessRunner

if TYPE_CHECKING:
    from .config import Config, Dataset

_CONNECTION_ERROR_RETURNCODES = frozenset(
    {
        SSH_CONNECTION_ERROR_RETURNCODE,
        COMMAND_TIMEOUT_RETURNCODE,
        COMMAND_STARTUP_ERROR_RETURNCODE,
    },
)


class UnlockOutcome(StrEnum):
    """Aggregate result of one unlock pass."""

    OK = "ok"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class DatasetStatus:
    """Lock status of a single dataset; ``locked`` is None when unknown."""

    locked: bool | None
    connection_error: bool = False


class ZfsUnlockClient:
    """Client for a restricted SSH ZFS unlock receiver."""

    def __init__(self, config: Config, *, runner: CommandRunner | None = None) -> None:  # noqa: D107
        self.config = config
        self.runner = runner or SubprocessRunner()

    def _ssh_args(self, remote_args: list[str], *, close_stdin: bool = False) -> list[str]:
        args = [
            "ssh",
            "-p",
            str(self.config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
        ]
        if close_stdin:
            args.append("-n")
        if self.config.identity_file is not None:
            args.extend(["-o", "IdentitiesOnly=yes", "-i", str(self.config.identity_file.expanduser())])
        args.extend([f"{self.config.user}@{self.config.host}", shlex.join(remote_args)])
        return args

    async def run_remote(self, remote_args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a receiver command over SSH."""
        return await self.runner.run(
            self._ssh_args(remote_args, close_stdin=input_text is None),
            input_text=input_text,
            command_timeout=self.config.command_timeout,
        )

    async def is_locked(self, dataset: Dataset, *, quiet: bool = False) -> DatasetStatus:
        """Check whether a dataset key is unavailable."""
        result = await self.run_remote(["status", dataset.path])
        if result.returncode != 0:
            if not quiet:
                err_console.print(f"[red]status failed for {dataset.path}: {result.stderr.strip()}[/red]")
            return DatasetStatus(
                locked=None,
                connection_error=result.returncode in _CONNECTION_ERROR_RETURNCODES,
            )

        status = result.stdout.strip()
        if status == "locked":
            return DatasetStatus(locked=True)
        if status == "unlocked":
            if not quiet:
                console.print(f"[green]OK[/green] {dataset.path}")
            return DatasetStatus(locked=False)
        return DatasetStatus(locked=None)

    async def unlock(self, dataset: Dataset) -> bool:
        """Unlock a dataset by sending its passphrase to the receiver."""
        try:
            passphrase = dataset.get_passphrase(self.config.secrets)
        except OSError as exc:
            err_console.print(f"[red]secret failed for {dataset.path}: {exc}[/red]")
            return False

        result = await self.run_remote(["unlock", dataset.path], input_text=f"{passphrase}\n")
        if result.returncode != 0:
            err_console.print(f"[red]unlock failed for {dataset.path}: {result.stderr.strip()}[/red]")
            return False
        console.print(f"[blue]->[/blue] Unlocked {dataset.path}")
        return True

    async def lock(self, dataset: Dataset, *, force: bool = False) -> bool:
        """Lock a dataset by unloading its key on the receiver."""
        remote_args = ["lock", dataset.path]
        if force:
            remote_args.append("--force")
        result = await self.run_remote(remote_args)
        if result.returncode != 0:
            err_console.print(f"[red]lock failed for {dataset.path}: {result.stderr.strip()}[/red]")
            return False
        console.print(f"[yellow]LOCK[/yellow] Locked {dataset.path}")
        return True


def _filter_datasets(datasets: list[Dataset], filters: list[str] | None) -> list[Dataset]:
    """Filter datasets by path patterns."""
    if not filters:
        return datasets
    return [ds for ds in datasets if any(f in ds.path for f in filters)]


def _select_datasets(config: Config, filters: list[str] | None) -> list[Dataset] | None:
    datasets = _filter_datasets(config.datasets, filters)
    if not datasets:
        err_console.print("[yellow]No matching datasets found.[/yellow]")
        return None
    return datasets


async def _lock_statuses(
    client: ZfsUnlockClient,
    datasets: list[Dataset],
    *,
    quiet: bool,
) -> list[DatasetStatus]:
    statuses = await asyncio.gather(
        *[client.is_locked(dataset, quiet=quiet) for dataset in datasets],
        return_exceptions=True,
    )
    # Exceptions (e.g. cancellation during shutdown) deliberately do NOT count
    # as connection errors: a cancelled pass must never trip panic-mode polling.
    return [DatasetStatus(locked=None) if isinstance(status, BaseException) else status for status in statuses]


async def run_unlock(
    config: Config,
    *,
    dry_run: bool = False,
    quiet: bool = False,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> UnlockOutcome:
    """Run one unlock pass over all matching datasets.

    Every locked dataset gets an unlock attempt even when others fail;
    UNREACHABLE is reported only when every status check failed at the
    connection level, so callers can tell "host is down" from "something
    on this host or in this config is wrong".
    """
    datasets = _select_datasets(config, dataset_filters)
    if datasets is None:
        return UnlockOutcome.FAILED

    if dry_run:
        console.print("[yellow]Dry run:[/yellow]")
        for dataset in datasets:
            console.print(f"  - {dataset.path}")
        return UnlockOutcome.OK

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await _lock_statuses(client, datasets, quiet=quiet)
    if all(status.connection_error for status in statuses):
        return UnlockOutcome.UNREACHABLE

    failed = any(status.locked is None for status in statuses)
    for dataset, status in zip(datasets, statuses, strict=True):
        if status.locked is True and not await client.unlock(dataset):
            failed = True
    return UnlockOutcome.FAILED if failed else UnlockOutcome.OK


async def run_lock(
    config: Config,
    *,
    force: bool = False,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    """Lock all configured datasets that are currently unlocked."""
    datasets = _select_datasets(config, dataset_filters)
    if datasets is None:
        return False

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await _lock_statuses(client, datasets, quiet=True)
    success = True
    for dataset, status in zip(datasets, statuses, strict=True):
        if status.locked is None:
            success = False
        elif status.locked is False:
            success = await client.lock(dataset, force=force) and success
        else:
            console.print(f"[dim]Already locked: {dataset.path}[/dim]")
    return success


async def run_status(
    config: Config,
    *,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    """Show lock status of all configured datasets."""
    datasets = _select_datasets(config, dataset_filters)
    if datasets is None:
        return False

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await _lock_statuses(client, datasets, quiet=True)
    success = True
    for dataset, status in zip(datasets, statuses, strict=True):
        if status.locked is None:
            console.print(f"[red]?[/red] {dataset.path} [dim]unknown[/dim]")
            success = False
        elif status.locked is True:
            console.print(f"[yellow]LOCK[/yellow] {dataset.path} [dim]locked[/dim]")
        else:
            console.print(f"[green]OPEN[/green] {dataset.path} [dim]unlocked[/dim]")
    return success
