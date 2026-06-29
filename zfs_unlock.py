"""OpenZFS dataset unlock over a restricted SSH receiver."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


class SecretsMode(str, Enum):
    """How to interpret secret values."""

    AUTO = "auto"
    FILES = "files"
    INLINE = "inline"


def resolve_secret(value: str, mode: SecretsMode) -> str:
    """Resolve a secret value based on the configured mode."""
    if mode == SecretsMode.INLINE:
        return value

    path = Path(value).expanduser()

    if mode == SecretsMode.FILES:
        return path.read_text().strip()

    if path.exists() and path.is_file():
        return path.read_text().strip()
    return value


class Dataset(BaseModel):
    """A ZFS dataset to unlock."""

    path: str
    secret: str

    @property
    def pool(self) -> str:  # noqa: D102
        return self.path.split("/")[0]

    @property
    def name(self) -> str:  # noqa: D102
        return "/".join(self.path.split("/")[1:])

    def get_passphrase(self, mode: SecretsMode) -> str:  # noqa: D102
        return resolve_secret(self.secret, mode)


class Config(BaseModel):
    """Application configuration for the off-box unlock client."""

    host: str
    user: str = "zfs-unlock"
    port: int = 22
    identity_file: Path | None = None
    connect_timeout: int = 5
    secrets: SecretsMode = SecretsMode.AUTO
    datasets: list[Dataset]

    @classmethod
    def from_yaml(cls, path: Path) -> Config:  # noqa: D102
        data = yaml.safe_load(path.read_text())

        datasets_raw = data.pop("datasets", {})
        datasets = [Dataset(path=ds_path, secret=secret) for ds_path, secret in datasets_raw.items()]

        return cls(datasets=datasets, **data)


@dataclass(frozen=True)
class CommandResult:
    """Result from a local command runner."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Async command runner protocol."""

    async def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a command and return its result."""


class SubprocessRunner:
    """Run commands through asyncio subprocesses."""

    async def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a command and capture stdout/stderr."""
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(None if input_text is None else input_text.encode())
        return CommandResult(
            returncode=process.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


class ZfsUnlockClient:
    """Client for a restricted SSH ZFS unlock receiver."""

    def __init__(self, config: Config, *, runner: CommandRunner | None = None) -> None:  # noqa: D107
        self.config = config
        self.runner = runner or SubprocessRunner()

    def _ssh_args(self, remote_args: list[str]) -> list[str]:
        args = [
            "ssh",
            "-p",
            str(self.config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
        ]
        if self.config.identity_file is not None:
            args.extend(["-i", str(self.config.identity_file.expanduser())])
        args.extend([f"{self.config.user}@{self.config.host}", shlex.join(remote_args)])
        return args

    async def run_remote(self, remote_args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a receiver command over SSH."""
        return await self.runner.run(self._ssh_args(remote_args), input_text=input_text)

    async def is_locked(self, dataset: Dataset, *, quiet: bool = False) -> bool | None:
        """Check whether a dataset key is unavailable."""
        result = await self.run_remote(["status", dataset.path])
        if result.returncode != 0:
            if not quiet:
                err_console.print(f"[red]status failed for {dataset.path}: {result.stderr.strip()}[/red]")
            return None

        status = result.stdout.strip()
        if status == "locked":
            return True
        if status == "unlocked":
            if not quiet:
                console.print(f"[green]✓[/green] {dataset.path}")
            return False
        return None

    async def unlock(self, dataset: Dataset) -> bool:
        """Unlock a dataset by sending its passphrase to the receiver."""
        passphrase = dataset.get_passphrase(self.config.secrets)
        result = await self.run_remote(["unlock", dataset.path], input_text=f"{passphrase}\n")
        if result.returncode != 0:
            err_console.print(f"[red]unlock failed for {dataset.path}: {result.stderr.strip()}[/red]")
            return False
        console.print(f"[blue]→[/blue] Unlocked {dataset.path}")
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
        console.print(f"[yellow]🔒[/yellow] Locked {dataset.path}")
        return True

    async def check_and_unlock(self, dataset: Dataset, *, quiet: bool = False) -> bool:
        """Unlock a dataset only if it is currently locked."""
        locked = await self.is_locked(dataset, quiet=quiet)
        if locked is None:
            raise ConnectionError("Failed to check lock status")
        if locked:
            console.print(f"[yellow]⚡[/yellow] {dataset.path} locked, unlocking...")
            return await self.unlock(dataset)
        return False


def filter_datasets(datasets: list[Dataset], filters: list[str] | None) -> list[Dataset]:
    """Filter datasets by path patterns."""
    if not filters:
        return datasets
    return [ds for ds in datasets if any(f in ds.path for f in filters)]


async def run_unlock(
    config: Config,
    *,
    dry_run: bool = False,
    quiet: bool = False,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    """Run one unlock pass. Returns False when any dataset check or unlock fails."""
    datasets = filter_datasets(config.datasets, dataset_filters)
    if not datasets:
        err_console.print("[yellow]No matching datasets found.[/yellow]")
        return True

    if dry_run:
        console.print("[yellow]Dry run:[/yellow]")
        for dataset in datasets:
            console.print(f"  • {dataset.path}")
        return True

    client = ZfsUnlockClient(config, runner=runner)
    try:
        statuses = await asyncio.gather(
            *[client.is_locked(dataset, quiet=quiet) for dataset in datasets],
            return_exceptions=True,
        )
    except Exception:
        return False

    for status in statuses:
        if isinstance(status, Exception) or status is None:
            return False

    for dataset, locked in zip(datasets, statuses, strict=True):
        if locked is True and not await client.unlock(dataset):
            return False
    return True


async def run_lock(
    config: Config,
    *,
    force: bool = False,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> None:
    """Lock all configured datasets that are currently unlocked."""
    datasets = filter_datasets(config.datasets, dataset_filters)
    if not datasets:
        err_console.print("[yellow]No matching datasets found.[/yellow]")
        return

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await asyncio.gather(*[client.is_locked(dataset, quiet=True) for dataset in datasets])
    for dataset, locked in zip(datasets, statuses, strict=True):
        if locked is False:
            await client.lock(dataset, force=force)
        elif locked is True:
            console.print(f"[dim]Already locked: {dataset.path}[/dim]")


async def run_status(
    config: Config,
    *,
    dataset_filters: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> None:
    """Show lock status of all configured datasets."""
    datasets = filter_datasets(config.datasets, dataset_filters)
    if not datasets:
        err_console.print("[yellow]No matching datasets found.[/yellow]")
        return

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await asyncio.gather(*[client.is_locked(dataset, quiet=True) for dataset in datasets])
    for dataset, locked in zip(datasets, statuses, strict=True):
        if locked is True:
            console.print(f"[yellow]🔒[/yellow] {dataset.path} [dim]locked[/dim]")
        elif locked is False:
            console.print(f"[green]🔓[/green] {dataset.path} [dim]unlocked[/dim]")
        else:
            console.print(f"[red]?[/red] {dataset.path} [dim]unknown[/dim]")
