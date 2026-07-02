"""Command line interface."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from .client import UnlockOutcome, filter_datasets, run_lock, run_status, run_unlock
from .config import Config, load_config
from .constants import MAX_PASSPHRASE_BYTES, PANIC_INTERVAL_SECONDS, PANIC_MODE_MAX_SECONDS
from .diagnostics import doctor
from .keygen import keygen
from .output import console, err_console
from .process import CommandResult
from .receiver import Receiver, parse_receiver_command
from .service import service_app
from .version import __version__


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

_DEFAULT_RECEIVER_ALLOW_FILE = Path("/etc/zfs-unlock/allowed-datasets")
_DEFAULT_RECEIVER_ZFS_PATH = "zfs"
_T = TypeVar("_T")


def _single_receiver_option(
    values: _T | list[_T] | tuple[_T, ...] | None,
    *,
    default: _T,
    flag: str,
) -> _T:
    """Resolve a receiver option that a wrapper may pin, rejecting duplicates.

    The SSH wrapper invokes ``receiver`` with pinned options and appends the
    attacker-controlled ``SSH_ORIGINAL_COMMAND`` after them. Click resolves a
    repeated single-valued option as last-wins, which would let that suffix
    replace the pinned value. Every security-relevant receiver option must
    therefore be declared as ``list[X] | None`` (so repeats accumulate instead
    of overwriting) and be resolved through this helper, which exits if the
    option was given more than once.
    """
    if values is None:
        return default
    if isinstance(values, list | tuple):
        if len(values) > 1:
            sys.stderr.write(f"duplicate receiver option: {flag}\n")
            raise typer.Exit(1)
        if not values:
            return default
        return values[0]
    return values


def _unlock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be done")] = False,
    daemon: Annotated[bool, typer.Option("--daemon", "-d", help="Run continuously")] = False,
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", min=1, help="Seconds between checks (1s if unreachable)"),
    ] = 30,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Dataset path or glob to select")] = None,
) -> None:
    """Unlock configured datasets."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")

    # Validate the filter up front: in daemon mode a typo like `-D tank/typo`
    # would otherwise print "No matching datasets found" every interval forever.
    if dataset and not filter_datasets(config.datasets, dataset):
        err_console.print(f"[red]No configured datasets match: {', '.join(dataset)}[/red]")
        raise typer.Exit(1)

    if daemon:
        console.print(f"[bold]Running with smart polling (interval: {interval}s)[/bold]")
        _run_daemon(config, interval=interval, dry_run=dry_run, dataset=dataset)
    else:
        outcome = asyncio.run(run_unlock(config, dry_run=dry_run, dataset_filters=dataset))
        if outcome is not UnlockOutcome.OK:
            raise typer.Exit(1)


def _run_daemon(config: Config, *, interval: int, dry_run: bool, dataset: list[str] | None) -> None:
    """Poll the receiver until interrupted, unlocking datasets as they appear.

    While the receiver is unreachable we poll every ``PANIC_INTERVAL_SECONDS`` so a
    just-rebooted host is unlocked promptly. That fast window is capped at
    ``PANIC_MODE_MAX_SECONDS``: a genuinely persistent failure (wrong key, host-key
    mismatch, host powered off) then backs off to ``interval`` instead of hammering
    the host at 1s forever.
    """
    current_interval = interval
    reachable = True
    # Wall-clock deadline, not a sum of nominal intervals: each unreachable
    # probe also blocks for up to connect_timeout, which iteration counting
    # would ignore, stretching the cap ~6x past PANIC_MODE_MAX_SECONDS.
    panic_started = 0.0

    while True:
        try:
            outcome = asyncio.run(run_unlock(config, dry_run=dry_run, quiet=True, dataset_filters=dataset))
            if outcome is UnlockOutcome.UNREACHABLE:
                if reachable:
                    console.print(
                        f"[yellow]Receiver unreachable. Switching to panic mode"
                        f" ({PANIC_INTERVAL_SECONDS}s interval).[/yellow]",
                    )
                    panic_started = time.monotonic()
                if time.monotonic() - panic_started < PANIC_MODE_MAX_SECONDS:
                    current_interval = PANIC_INTERVAL_SECONDS
                else:
                    if current_interval != interval:
                        console.print(
                            f"[yellow]Receiver still unreachable after {PANIC_MODE_MAX_SECONDS}s;"
                            f" backing off to {interval}s.[/yellow]",
                        )
                    current_interval = interval
            else:
                if not reachable and outcome is UnlockOutcome.OK:
                    console.print("[green]Connection restored.[/green]")
                elif not reachable:
                    console.print("[yellow]Receiver reachable again, but the unlock pass failed.[/yellow]")
                current_interval = interval

            reachable = outcome is not UnlockOutcome.UNREACHABLE
            time.sleep(current_interval)
        except KeyboardInterrupt:
            console.print("\n[bold]Stopped[/bold]")
            break


def _lock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force unmount before locking")] = False,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Dataset path or glob to select")] = None,
) -> None:
    """Lock configured datasets."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    success = asyncio.run(run_lock(config, force=force, dataset_filters=dataset))
    if not success:
        raise typer.Exit(1)


def _status(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Dataset path or glob to select")] = None,
) -> None:
    """Show lock status of configured datasets."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    success = asyncio.run(run_status(config, dataset_filters=dataset))
    if not success:
        raise typer.Exit(1)


def _read_passphrase(limit: int) -> str | None:
    """Read a passphrase from stdin, capped at `limit` bytes.

    The receiver runs as root behind an SSH forced command, so cap the read
    rather than pulling unbounded input into memory. The client terminates the
    passphrase with a single newline, which does not count against the limit —
    a passphrase of exactly `limit` bytes is accepted. Returns None when the
    cap is exceeded or the bytes are not valid UTF-8 (a lenient decode would
    hand ``zfs load-key`` a silently corrupted passphrase).
    """
    data = sys.stdin.buffer.read(limit + 2)
    if len(data.removesuffix(b"\n")) > limit:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def _receiver(
    ctx: typer.Context,
    allow_file: Annotated[
        list[Path] | None,
        typer.Option("--allow-file", help="File containing allowed dataset names"),
    ] = None,
    zfs_path: Annotated[
        list[str] | None,
        typer.Option("--zfs-path", help="Path to the zfs executable"),
    ] = None,
) -> None:
    """Run the restricted receiver."""
    # These options are pinned by the SSH wrapper, which appends untrusted
    # input after them. They must stay list-typed and go through
    # _single_receiver_option so a duplicate cannot override the pinned value;
    # any new receiver option needs the same treatment.
    resolved_allow_file = _single_receiver_option(
        allow_file,
        default=_DEFAULT_RECEIVER_ALLOW_FILE,
        flag="--allow-file",
    )
    resolved_zfs_path = _single_receiver_option(
        zfs_path,
        default=_DEFAULT_RECEIVER_ZFS_PATH,
        flag="--zfs-path",
    )

    try:
        args = list(ctx.args)
        if len(args) == 1:
            args = parse_receiver_command(args[0])
        elif not args:
            original_command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
            args = parse_receiver_command(original_command) if original_command else []
    except ValueError as exc:
        sys.stderr.write(f"invalid receiver command: {exc}\n")
        raise typer.Exit(1) from exc

    receiver_instance = Receiver(allow_file=resolved_allow_file, zfs_path=resolved_zfs_path)
    request = receiver_instance.parse(args)
    if isinstance(request, CommandResult):
        response = request
    elif request.requires_stdin:
        stdin_text = _read_passphrase(MAX_PASSPHRASE_BYTES)
        if stdin_text is None:
            response = CommandResult(
                returncode=1,
                stdout="",
                stderr=f"refusing passphrase: longer than {MAX_PASSPHRASE_BYTES} bytes or not valid UTF-8\n",
            )
        else:
            response = receiver_instance.handle(request, stdin_text=stdin_text)
    else:
        response = receiver_instance.handle(request, stdin_text="")

    if response.stdout:
        sys.stdout.write(response.stdout)
    if response.stderr:
        sys.stderr.write(response.stderr)
    raise typer.Exit(response.returncode)


def _register_top_level_commands() -> None:
    """Register top-level commands in the order shown by --help."""
    app.command("unlock", rich_help_panel="Client Commands")(_unlock)
    app.command("lock", rich_help_panel="Client Commands")(_lock)
    app.command("status", rich_help_panel="Client Commands")(_status)
    app.command(rich_help_panel="Client Commands")(doctor)
    app.command(rich_help_panel="Setup Commands")(keygen)
    app.command(
        "receiver",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        rich_help_panel="Receiver Commands",
    )(_receiver)
    app.add_typer(service_app, name="service", rich_help_panel="Service Commands")


_register_top_level_commands()


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
