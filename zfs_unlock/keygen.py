"""SSH key generation command."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from .constants import DEFAULT_IDENTITY_FILE
from .output import console, err_console, print_ok
from .process import run_process


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
    if identity_path.exists() or public_path.exists():
        if not overwrite:
            err_console.print(f"[red]Refusing to overwrite existing key: {identity_path}[/red]")
            raise typer.Exit(1)
        # ssh-keygen prompts "Overwrite (y/n)?" on captured stdout and fails
        # without a terminal, so remove the old key pair up front.
        identity_path.unlink(missing_ok=True)
        public_path.unlink(missing_ok=True)

    identity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run_process([ssh_keygen, "-t", "ed25519", "-N", "", "-C", comment, "-f", str(identity_path)])
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or exc
        err_console.print(f"[red]ssh-keygen failed:[/red] {detail}")
        raise typer.Exit(1) from exc
    identity_path.chmod(0o600)
    public_path.chmod(0o644)

    public_key = public_path.read_text().strip()
    print_ok(f"created {identity_path}")
    console.print("\nPublic key for services.zfsUnlock.receiver.authorizedKeys:\n")
    console.print(public_key)
