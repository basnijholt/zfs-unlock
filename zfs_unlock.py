"""OpenZFS dataset unlock over a restricted SSH receiver."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

import typer
import yaml
from pydantic import BaseModel
from rich.console import Console

try:
    __version__ = importlib.metadata.version("zfs-unlock")
except importlib.metadata.PackageNotFoundError:
    try:
        from _version import __version__
    except ImportError:
        __version__ = "unknown"

console = Console()
err_console = Console(stderr=True)

DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")
COMMAND_TIMEOUT_RETURNCODE = 124
RECEIVER_UNLOCK_ARG_COUNT = 2

CONFIG_SEARCH_PATHS = [
    Path("config.yaml"),
    Path("config.yml"),
    Path.home() / ".config" / "zfs-unlock" / "config.yaml",
    Path.home() / ".config" / "zfs-unlock" / "config.yml",
]
DEFAULT_IDENTITY_FILE = Path("~/.ssh/zfs-unlock-nas")

EXAMPLE_CONFIG = """\
host: nas.local
user: zfs-unlock
# port: 22
# identity_file: ~/.ssh/zfs-unlock-nas
# connect_timeout: 5
# command_timeout: 30
# secrets: auto  # auto (default), files, or inline

datasets:
  tank/syncthing: ~/.secrets/syncthing-key
  tank/photos: my-literal-passphrase
"""

SYSTEMD_SERVICE = """\
[Unit]
Description=ZFS Unlock
After=network-online.target
Wants=network-online.target

[Service]
Environment="PATH={path}"
ExecStart={uv_path} tool run zfs-unlock unlock --daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.zfs_unlock</string>
  <key>ProgramArguments</key>
  <array>
    <string>{uv_path}</string>
    <string>tool</string>
    <string>run</string>
    <string>zfs-unlock</string>
    <string>unlock</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>{home}</string>
  <key>StandardOutPath</key>
  <string>{log_dir}/zfs-unlock.out</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/zfs-unlock.err</string>
</dict>
</plist>
"""


class SecretsMode(StrEnum):
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
    command_timeout: float = 30
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

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and return its result."""


class SubprocessRunner:
    """Run commands through asyncio subprocesses."""

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and capture stdout/stderr."""
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(None if input_text is None else input_text.encode()),
                timeout=command_timeout,
            )
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            stdout, stderr = await process.communicate()
            return CommandResult(
                returncode=COMMAND_TIMEOUT_RETURNCODE,
                stdout=stdout.decode(errors="replace"),
                stderr=f"command timed out after {command_timeout:g}s\n{stderr.decode(errors='replace')}",
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        returncode = process.returncode if process.returncode is not None else 1
        return CommandResult(
            returncode=returncode,
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
        """Initialize the receiver with an allowlist file and command runner."""
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
        if command == "lock" and len(args) in {2, 3}:
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
                console.print(f"[green]OK[/green] {dataset.path}")
            return False
        return None

    async def unlock(self, dataset: Dataset) -> bool:
        """Unlock a dataset by sending its passphrase to the receiver."""
        passphrase = dataset.get_passphrase(self.config.secrets)
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

    async def check_and_unlock(self, dataset: Dataset, *, quiet: bool = False) -> bool:
        """Unlock a dataset only if it is currently locked."""
        locked = await self.is_locked(dataset, quiet=quiet)
        if locked is None:
            msg = "Failed to check lock status"
            raise ConnectionError(msg)
        if locked:
            console.print(f"[yellow]![/yellow] {dataset.path} locked, unlocking...")
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
            console.print(f"  - {dataset.path}")
        return True

    client = ZfsUnlockClient(config, runner=runner)
    statuses = await asyncio.gather(
        *[client.is_locked(dataset, quiet=quiet) for dataset in datasets],
        return_exceptions=True,
    )

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
            console.print(f"[yellow]LOCK[/yellow] {dataset.path} [dim]locked[/dim]")
        elif locked is False:
            console.print(f"[green]OPEN[/green] {dataset.path} [dim]unlocked[/dim]")
        else:
            console.print(f"[red]?[/red] {dataset.path} [dim]unknown[/dim]")


def find_config() -> Path | None:
    """Find config file in standard locations."""
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return None


def _version_callback(value: bool) -> None:  # noqa: FBT001
    if value:
        console.print(f"zfs-unlock {__version__}")
        raise typer.Exit


app = typer.Typer(
    help="Unlock OpenZFS datasets over a restricted SSH receiver",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

service_app = typer.Typer(help="Manage system service", no_args_is_help=True)
app.add_typer(service_app, name="service")


def _get_uv_path() -> Path | None:
    """Find uv executable."""
    uv = shutil.which("uv")
    return Path(uv) if uv else None


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _check_ok(message: str) -> None:
    console.print(f"[green]OK[/green] {message}", soft_wrap=True)


def _check_fail(message: str) -> None:
    console.print(f"[red]FAIL[/red] {message}", soft_wrap=True)


def _is_private_file(path: Path) -> bool:
    """Return whether a file is unreadable and unwritable by group/others."""
    if os.name == "nt":
        return True
    return path.stat().st_mode & 0o077 == 0


def _check_private_file(path: Path, label: str) -> bool:
    """Check that a sensitive file exists and has private permissions."""
    if not path.exists():
        _check_fail(f"{label} missing: {path}")
        return False
    if not _is_private_file(path):
        _check_fail(f"{label} permissions too open: {path}")
        return False
    _check_ok(f"{label} permissions private: {path}")
    return True


def _check_identity_file(identity_file: Path | None) -> bool:
    if identity_file is None:
        console.print("[yellow]WARN[/yellow] identity file not configured; SSH defaults will be used")
        return True

    identity_path = identity_file.expanduser()
    return _check_private_file(identity_path, "identity file")


def _check_secret_file_permissions(config: Config) -> bool:
    ok = True
    for dataset in config.datasets:
        path = Path(dataset.secret).expanduser()
        if config.secrets == SecretsMode.INLINE:
            continue
        if config.secrets == SecretsMode.AUTO and not path.exists():
            continue
        if not path.exists():
            _check_fail(f"secret file missing for {dataset.path}: {path}")
            ok = False
            continue
        if not _is_private_file(path):
            _check_fail(f"secret file permissions too open for {dataset.path}: {path}")
            ok = False
            continue
        _check_ok(f"secret file permissions private for {dataset.path}: {path}")
    return ok


def _check_ssh_executable() -> bool:
    ssh_path = shutil.which("ssh")
    if ssh_path is None:
        _check_fail("ssh executable missing from PATH")
        return False

    _check_ok(f"ssh executable exists: {ssh_path}")
    return True


def _check_host_reachable(config: Config) -> bool:
    ok = True
    try:
        socket.getaddrinfo(config.host, config.port)
    except OSError as exc:
        _check_fail(f"host resolution failed for {config.host}: {exc}")
        ok = False
    else:
        _check_ok(f"host resolves: {config.host}")

    try:
        with socket.create_connection((config.host, config.port), timeout=config.connect_timeout):
            pass
    except OSError as exc:
        _check_fail(f"tcp connect failed for {config.host}:{config.port}: {exc}")
        ok = False
    else:
        _check_ok(f"tcp connect ok: {config.host}:{config.port}")

    return ok


def _select_doctor_datasets(datasets: list[Dataset], dataset: str | None) -> list[Dataset]:
    if dataset is None:
        return datasets

    for configured in datasets:
        if configured.path == dataset:
            return [configured]

    _check_fail(f"dataset not configured: {dataset}")
    raise typer.Exit(1)


async def _check_receiver_status(client: ZfsUnlockClient, dataset: Dataset) -> bool:
    console.print(f"[dim]checking receiver status: {dataset.path}[/dim]", soft_wrap=True)
    result = await client.run_remote(["status", dataset.path])
    if result.returncode != 0:
        _check_fail(f"receiver status failed: {dataset.path}: {result.stderr.strip()}")
        return False

    status = result.stdout.strip()
    if status in {"locked", "unlocked"}:
        _check_ok(f"receiver status ok: {dataset.path} -> {status}")
        return True

    _check_fail(f"receiver status unexpected: {dataset.path} -> {status}")
    return False


async def _check_receiver_statuses(config: Config, datasets: list[Dataset]) -> bool:
    client = ZfsUnlockClient(config)
    ok = True
    for dataset in datasets:
        if not await _check_receiver_status(client, dataset):
            ok = False
    return ok


@app.command()
def keygen(
    identity_file: Annotated[
        Path,
        typer.Option("--identity-file", "-i", help="SSH identity file to create"),
    ] = DEFAULT_IDENTITY_FILE,
    comment: Annotated[str, typer.Option("--comment", "-C", help="SSH key comment")] = "zfs-unlock",
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace an existing key")] = False,
) -> None:
    """Generate a dedicated SSH key for zfs-unlock."""
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        err_console.print("[red]Error: ssh-keygen not found.[/red]")
        raise typer.Exit(1)

    identity_path = identity_file.expanduser()
    public_path = Path(f"{identity_path}.pub")
    if not overwrite and (identity_path.exists() or public_path.exists()):
        err_console.print(f"[red]Refusing to overwrite existing key: {identity_path}[/red]")
        raise typer.Exit(1)

    identity_path.parent.mkdir(parents=True, exist_ok=True)
    _run([ssh_keygen, "-t", "ed25519", "-N", "", "-C", comment, "-f", str(identity_path)])
    identity_path.chmod(0o600)
    public_path.chmod(0o644)

    public_key = public_path.read_text().strip()
    _check_ok(f"created {identity_path}")
    console.print("\nPublic key for services.zfsUnlock.receiver.authorizedKeys:\n")
    console.print(public_key)


@app.command()
def doctor(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dataset: Annotated[str | None, typer.Option("--dataset", "-D", help="Dataset to check")] = None,
) -> None:
    """Check client config, SSH key, host reachability, and receiver status."""
    config_path, config = _load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]", soft_wrap=True)
    _check_ok("config parsed")

    if not _check_identity_file(config.identity_file):
        raise typer.Exit(1)

    if not _check_secret_file_permissions(config):
        raise typer.Exit(1)

    if not _check_ssh_executable():
        raise typer.Exit(1)

    ok = _check_host_reachable(config)
    check_datasets = _select_doctor_datasets(config.datasets, dataset)
    if ok and check_datasets:
        ok = asyncio.run(_check_receiver_statuses(config, check_datasets))
    raise typer.Exit(0 if ok else 1)


@service_app.command("install")
def service_install() -> None:
    """Install and start the system service."""
    uv_path = _get_uv_path()
    if not uv_path:
        err_console.print("[red]Error: uv not found. Install from https://docs.astral.sh/uv/[/red]")
        raise typer.Exit(1)

    config_path = find_config()
    if not config_path:
        err_console.print("[yellow]Warning: Config not found.[/yellow]")
        err_console.print("Create ~/.config/zfs-unlock/config.yaml before starting.")

    system = platform.system()
    if system == "Darwin":
        _install_macos(uv_path)
    elif system == "Linux":
        _install_linux(uv_path)
    else:
        err_console.print(f"[red]Unsupported OS: {system}[/red]")
        raise typer.Exit(1)


def _install_macos(uv_path: Path) -> None:
    """Install launchd service on macOS."""
    plist_name = "com.zfs_unlock.plist"
    plist_dst = Path.home() / "Library" / "LaunchAgents" / plist_name
    log_dir = Path.home() / "Library" / "Logs" / "zfs-unlock"

    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    plist_dst.write_text(LAUNCHD_PLIST.format(uv_path=uv_path, home=Path.home(), log_dir=log_dir))
    _run(["launchctl", "load", str(plist_dst)])

    console.print("[green]OK[/green] Service installed and started")
    console.print(f"  Logs: {log_dir}/")
    console.print("\n  Uninstall: [bold]zfs-unlock service uninstall[/bold]")


def _install_linux(uv_path: Path) -> None:
    """Install systemd user service on Linux."""
    service_name = "zfs-unlock.service"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dst = service_dir / service_name

    service_dir.mkdir(parents=True, exist_ok=True)
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    service_dst.write_text(SYSTEMD_SERVICE.format(uv_path=uv_path, path=current_path))

    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", "zfs-unlock"])

    console.print("[green]OK[/green] Service installed and started")
    console.print("\n  View logs: [bold]journalctl --user -u zfs-unlock -f[/bold]")
    console.print("  Run at boot: [bold]sudo loginctl enable-linger $USER[/bold]")
    console.print("\n  Uninstall: [bold]zfs-unlock service uninstall[/bold]")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop and remove the system service."""
    system = platform.system()
    if system == "Darwin":
        _uninstall_macos()
    elif system == "Linux":
        _uninstall_linux()
    else:
        err_console.print(f"[red]Unsupported OS: {system}[/red]")
        raise typer.Exit(1)


def _uninstall_macos() -> None:
    """Uninstall launchd service on macOS."""
    plist_dst = Path.home() / "Library" / "LaunchAgents" / "com.zfs_unlock.plist"
    if not plist_dst.exists():
        console.print("Service not installed.")
        return

    _run(["launchctl", "unload", str(plist_dst)], check=False)
    plist_dst.unlink()
    console.print("[green]OK[/green] Service uninstalled")


def _uninstall_linux() -> None:
    """Uninstall systemd user service on Linux."""
    service_dst = Path.home() / ".config" / "systemd" / "user" / "zfs-unlock.service"
    if not service_dst.exists():
        console.print("Service not installed.")
        return

    _run(["systemctl", "--user", "stop", "zfs-unlock"], check=False)
    _run(["systemctl", "--user", "disable", "zfs-unlock"], check=False)
    service_dst.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    console.print("[green]OK[/green] Service uninstalled")


@service_app.command("status")
def service_status() -> None:
    """Check service status."""
    system = platform.system()
    if system == "Darwin":
        result = _run(["launchctl", "list"], check=False)
        if "com.zfs_unlock" in result.stdout:
            console.print("[green]ACTIVE[/green] Service is running")
        else:
            console.print("[dim]INACTIVE[/dim] Service is not running")
    elif system == "Linux":
        result = _run(["systemctl", "--user", "is-active", "zfs-unlock"], check=False)
        if result.stdout.strip() == "active":
            console.print("[green]ACTIVE[/green] Service is running")
        else:
            console.print("[dim]INACTIVE[/dim] Service is not running")
    else:
        err_console.print(f"[red]Unsupported OS: {system}[/red]")
        raise typer.Exit(1)


@service_app.command("logs")
def service_logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow log output")] = True,
) -> None:
    """View service logs."""
    system = platform.system()
    if system == "Darwin":
        log_dir = Path.home() / "Library" / "Logs" / "zfs-unlock"
        out_log = log_dir / "zfs-unlock.out"
        err_log = log_dir / "zfs-unlock.err"
        if not log_dir.exists():
            err_console.print("[yellow]No logs found. Is the service installed?[/yellow]")
            raise typer.Exit(1)

        tail_path = shutil.which("tail")
        if not tail_path:
            err_console.print("[red]Error: tail not found.[/red]")
            raise typer.Exit(1)

        cmd = [tail_path]
        if follow:
            cmd.append("-f")
        cmd.extend([str(out_log), str(err_log)])
        os.execvp(tail_path, cmd)  # noqa: S606
    elif system == "Linux":
        journalctl_path = shutil.which("journalctl")
        if not journalctl_path:
            err_console.print("[red]Error: journalctl not found.[/red]")
            raise typer.Exit(1)

        cmd = [journalctl_path, "--user", "-u", "zfs-unlock"]
        if follow:
            cmd.append("-f")
        os.execvp(journalctl_path, cmd)  # noqa: S606
    else:
        err_console.print(f"[red]Unsupported OS: {system}[/red]")
        raise typer.Exit(1)


def _load_config(config_path: Path | None) -> tuple[Path, Config]:
    if config_path is None:
        config_path = find_config()

    if config_path is None or not config_path.exists():
        err_console.print("[red]Config not found.[/red]")
        err_console.print("\nCreate ~/.config/zfs-unlock/config.yaml:\n")
        err_console.print(EXAMPLE_CONFIG)
        raise typer.Exit(1)

    return config_path, Config.from_yaml(config_path)


@app.command()
def unlock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be done")] = False,
    daemon: Annotated[bool, typer.Option("--daemon", "-d", help="Run continuously")] = False,
    interval: Annotated[int, typer.Option("--interval", "-i", help="Seconds between checks (1s if unreachable)")] = 30,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Unlock configured datasets."""
    config_path, config = _load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")

    if daemon:
        console.print(f"[bold]Running with smart polling (interval: {interval}s)[/bold]")
        current_interval = interval
        last_success = True

        while True:
            try:
                success = asyncio.run(run_unlock(config, dry_run=dry_run, quiet=True, dataset_filters=dataset))
                if success:
                    if not last_success:
                        console.print("[green]Connection restored.[/green]")
                    current_interval = interval
                else:
                    if last_success:
                        console.print(
                            "[yellow]Connection lost/unstable. Switching to panic mode (1s interval).[/yellow]",
                        )
                    current_interval = 1

                last_success = success
                time.sleep(current_interval)
            except KeyboardInterrupt:
                console.print("\n[bold]Stopped[/bold]")
                break
    else:
        asyncio.run(run_unlock(config, dry_run=dry_run, dataset_filters=dataset))


@app.command()
def lock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force unmount before locking")] = False,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Lock configured datasets."""
    config_path, config = _load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    asyncio.run(run_lock(config, force=force, dataset_filters=dataset))


@app.command()
def status(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Show lock status of configured datasets."""
    config_path, config = _load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    asyncio.run(run_status(config, dataset_filters=dataset))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def receiver(
    ctx: typer.Context,
    allow_file: Annotated[
        Path,
        typer.Option("--allow-file", help="File containing allowed dataset names"),
    ] = Path("/etc/zfs-unlock/allowed-datasets"),
    zfs_path: Annotated[
        str,
        typer.Option("--zfs-path", help="Path to the zfs executable"),
    ] = "zfs",
) -> None:
    """Run the restricted NAS-side receiver."""
    args = list(ctx.args)
    if len(args) == 1:
        args = parse_receiver_command(args[0])
    elif not args:
        original_command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        args = parse_receiver_command(original_command) if original_command else []

    stdin_text = sys.stdin.read() if len(args) == RECEIVER_UNLOCK_ARG_COUNT and args[0] == "unlock" else ""
    response = Receiver(allow_file=allow_file, zfs_path=zfs_path).handle(args, stdin_text=stdin_text)
    if response.stdout:
        sys.stdout.write(response.stdout)
    if response.stderr:
        sys.stderr.write(response.stderr)
    raise typer.Exit(response.returncode)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool | None,
        typer.Option("--version", "-v", help="Show version and exit", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Unlock encrypted OpenZFS datasets."""
    if ctx.invoked_subcommand is not None:
        return
    console.print(ctx.get_help())


if __name__ == "__main__":
    app()
