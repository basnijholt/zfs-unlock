"""OpenZFS dataset unlock over a restricted SSH receiver."""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")


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


class LocalCommandRunner(Protocol):
    """Synchronous local command runner protocol for the receiver."""

    def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a local command and return its result."""


class LocalSubprocessRunner:
    """Run local receiver commands through subprocess."""

    def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a local command and capture stdout/stderr."""
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def is_safe_dataset_name(name: str) -> bool:
    """Return whether a string is a conservative ZFS dataset name."""
    if not DATASET_NAME_RE.fullmatch(name):
        return False
    return all(segment not in {".", ".."} and not segment.startswith("-") for segment in name.split("/"))


def parse_receiver_command(command: str) -> list[str]:
    """Parse an SSH_ORIGINAL_COMMAND string into receiver arguments."""
    return shlex.split(command)


class Receiver:
    """Restricted NAS-side receiver for ZFS unlock commands."""

    def __init__(
        self,
        *,
        allow_file: Path,
        runner: LocalCommandRunner | None = None,
        zfs_path: str = "zfs",
    ) -> None:
        self.allow_file = allow_file
        self.runner = runner or LocalSubprocessRunner()
        self.zfs_path = zfs_path

    def handle(self, args: list[str], *, stdin_text: str) -> CommandResult:
        """Handle a restricted receiver command."""
        if not args:
            return self._error("missing command")

        command = args[0]
        if command == "status" and len(args) == 2:  # noqa: PLR2004
            return self._status(args[1])
        if command == "unlock" and len(args) == 2:  # noqa: PLR2004
            return self._unlock(args[1], stdin_text=stdin_text)
        if command == "lock" and len(args) in {2, 3}:  # noqa: PLR2004
            force = len(args) == 3 and args[2] == "--force"  # noqa: PLR2004
            if len(args) == 3 and not force:  # noqa: PLR2004
                return self._error("unsupported lock option")
            return self._lock(args[1], force=force)
        return self._error("unsupported command")

    def _allowed_datasets(self) -> set[str]:
        if not self.allow_file.exists():
            return set()

        datasets: set[str] = set()
        for line in self.allow_file.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                datasets.add(stripped)
        return datasets

    def _validate_dataset(self, dataset: str) -> str | None:
        if not is_safe_dataset_name(dataset):
            return f"unsafe dataset name: {dataset}"
        if dataset not in self._allowed_datasets():
            return f"dataset not allowed: {dataset}"
        return None

    def _keystatus(self, dataset: str) -> CommandResult:
        return self.runner.run([self.zfs_path, "get", "-H", "-o", "value", "keystatus", dataset])

    def _status(self, dataset: str) -> CommandResult:
        if error := self._validate_dataset(dataset):
            return self._error(error)

        result = self._keystatus(dataset)
        if result.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=result.stderr)

        status = result.stdout.strip()
        if status == "unavailable":
            return CommandResult(returncode=0, stdout="locked\n", stderr="")
        if status == "available":
            return CommandResult(returncode=0, stdout="unlocked\n", stderr="")
        return CommandResult(returncode=0, stdout="unknown\n", stderr="")

    def _unlock(self, dataset: str, *, stdin_text: str) -> CommandResult:
        if error := self._validate_dataset(dataset):
            return self._error(error)

        status = self._keystatus(dataset)
        if status.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=status.stderr)
        if status.stdout.strip() == "available":
            return CommandResult(returncode=0, stdout=f"already unlocked {dataset}\n", stderr="")
        if not stdin_text:
            return self._error("missing passphrase on stdin")

        result = self.runner.run([self.zfs_path, "load-key", "-L", "prompt", dataset], input_text=stdin_text)
        if result.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=result.stderr)

        self.runner.run([self.zfs_path, "mount", "-a"])
        return CommandResult(returncode=0, stdout=f"unlocked {dataset}\n", stderr="")

    def _lock(self, dataset: str, *, force: bool) -> CommandResult:
        if error := self._validate_dataset(dataset):
            return self._error(error)

        if force:
            unmount = self.runner.run([self.zfs_path, "unmount", "-f", "-r", dataset])
            if unmount.returncode != 0:
                return CommandResult(returncode=1, stdout="", stderr=unmount.stderr)

        result = self.runner.run([self.zfs_path, "unload-key", "-r", dataset])
        if result.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=result.stderr)
        return CommandResult(returncode=0, stdout=f"locked {dataset}\n", stderr="")

    @staticmethod
    def _error(message: str) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr=f"{message}\n")


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
