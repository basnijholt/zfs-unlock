"""Command line interface."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from .client import run_lock, run_status, run_unlock
from .config import load_config
from .diagnostics import doctor
from .keygen import keygen
from .output import console
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


def unlock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be done")] = False,
    daemon: Annotated[bool, typer.Option("--daemon", "-d", help="Run continuously")] = False,
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", min=1, help="Seconds between checks (1s if unreachable)"),
    ] = 30,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Unlock configured datasets."""
    config_path, config = load_config(config_path)
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
        success = asyncio.run(run_unlock(config, dry_run=dry_run, dataset_filters=dataset))
        if not success:
            raise typer.Exit(1)


def lock(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force unmount before locking")] = False,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Lock configured datasets."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    success = asyncio.run(run_lock(config, force=force, dataset_filters=dataset))
    if not success:
        raise typer.Exit(1)


def status(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-D", help="Filter by dataset path")] = None,
) -> None:
    """Show lock status of configured datasets."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]")
    success = asyncio.run(run_status(config, dataset_filters=dataset))
    if not success:
        raise typer.Exit(1)


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
    if response := receiver_instance.preflight(args):
        if response.stdout:
            sys.stdout.write(response.stdout)
        if response.stderr:
            sys.stderr.write(response.stderr)
        raise typer.Exit(response.returncode)

    stdin_text = sys.stdin.read() if receiver_instance.requires_stdin(args) else ""
    response = receiver_instance.handle(args, stdin_text=stdin_text)
    if response.stdout:
        sys.stdout.write(response.stdout)
    if response.stderr:
        sys.stderr.write(response.stderr)
    raise typer.Exit(response.returncode)


def _register_top_level_commands() -> None:
    """Register top-level commands in the order shown by --help."""
    app.command(rich_help_panel="Client Commands")(unlock)
    app.command(rich_help_panel="Client Commands")(lock)
    app.command(rich_help_panel="Client Commands")(status)
    app.command(rich_help_panel="Client Commands")(doctor)
    app.command(rich_help_panel="Setup Commands")(keygen)
    app.command(
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        rich_help_panel="Receiver Commands",
    )(receiver)
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
