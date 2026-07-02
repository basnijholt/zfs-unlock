"""Command line interface."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from .client import UnlockOutcome, run_lock, run_status, run_unlock
from .config import load_config
from .constants import MAX_PASSPHRASE_BYTES
from .diagnostics import doctor
from .keygen import keygen
from .output import console
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

    if daemon:
        console.print(f"[bold]Running with smart polling (interval: {interval}s)[/bold]")
        current_interval = interval
        reachable = True

        while True:
            try:
                outcome = asyncio.run(run_unlock(config, dry_run=dry_run, quiet=True, dataset_filters=dataset))
                if outcome is UnlockOutcome.UNREACHABLE:
                    if reachable:
                        console.print("[yellow]Receiver unreachable. Switching to panic mode (1s interval).[/yellow]")
                    current_interval = 1
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
    else:
        outcome = asyncio.run(run_unlock(config, dry_run=dry_run, dataset_filters=dataset))
        if outcome is not UnlockOutcome.OK:
            raise typer.Exit(1)


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
    rather than pulling unbounded input into memory. Returns None when the cap
    is exceeded or the bytes are not valid UTF-8 (a lenient decode would hand
    ``zfs load-key`` a silently corrupted passphrase).
    """
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def _receiver(
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
    """Run the restricted receiver."""
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

    receiver_instance = Receiver(allow_file=allow_file, zfs_path=zfs_path)
    request = receiver_instance.parse(args)
    if isinstance(request, CommandResult):
        response = request
    elif request.requires_stdin:
        stdin_text = _read_passphrase(MAX_PASSPHRASE_BYTES)
        if stdin_text is None:
            response = CommandResult(
                returncode=1,
                stdout="",
                stderr=f"refusing passphrase: stdin exceeded {MAX_PASSPHRASE_BYTES} bytes or was not valid UTF-8\n",
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
