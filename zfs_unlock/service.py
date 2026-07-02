"""Local service installation helpers."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
from pathlib import Path
from typing import Annotated
from xml.sax.saxutils import escape

import typer

from .config import find_config
from .output import console, err_console
from .process import run_process

_SYSTEMD_SERVICE = """\
[Unit]
Description=ZFS Unlock
After=network-online.target
Wants=network-online.target

[Service]
Environment="PATH={path}"
ExecStart={exec_start}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.zfs_unlock</string>
  <key>ProgramArguments</key>
  <array>
{program_arguments}
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

service_app = typer.Typer(help="Manage system service", no_args_is_help=True)


def _daemon_argv() -> list[str]:
    """Resolve the daemon command, pinning the installed zfs-unlock executable.

    The service must run the same binary the user installed, not whatever a
    runtime resolver picks later; `uv tool run` is only a fallback for setups
    where zfs-unlock itself is not on PATH.
    """
    exe = shutil.which("zfs-unlock")
    if exe is not None:
        return [exe, "unlock", "--daemon"]

    uv = shutil.which("uv")
    if uv is not None:
        err_console.print(
            "[yellow]Warning: zfs-unlock not found on PATH; the service will resolve"
            " the latest release through 'uv tool run' at startup.[/yellow]",
        )
        err_console.print("Prefer 'uv tool install zfs-unlock' and reinstall the service.")
        return [uv, "tool", "run", "zfs-unlock", "unlock", "--daemon"]

    err_console.print("[red]Error: neither zfs-unlock nor uv found on PATH.[/red]")
    raise typer.Exit(1)


@service_app.command("install")
def service_install() -> None:
    """Install and start the system service."""
    argv = _daemon_argv()

    config_path = find_config()
    if not config_path:
        err_console.print("[yellow]Warning: Config not found.[/yellow]")
        err_console.print("Create ~/.config/zfs-unlock/config.yaml before starting.")

    system = platform.system()
    if system == "Darwin":
        _install_macos(argv)
    elif system == "Linux":
        _install_linux(argv)
    else:
        err_console.print(f"[red]Unsupported OS: {system}[/red]")
        raise typer.Exit(1)


def _install_macos(argv: list[str]) -> None:
    """Install launchd service on macOS."""
    plist_name = "com.zfs_unlock.plist"
    plist_dst = Path.home() / "Library" / "LaunchAgents" / plist_name
    log_dir = Path.home() / "Library" / "Logs" / "zfs-unlock"

    program_arguments = "\n".join(f"    <string>{escape(arg)}</string>" for arg in argv)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    plist_dst.write_text(_LAUNCHD_PLIST.format(program_arguments=program_arguments, home=Path.home(), log_dir=log_dir))
    run_process(["launchctl", "load", str(plist_dst)])

    console.print("[green]OK[/green] Service installed and started")
    console.print(f"  Logs: {log_dir}/")
    console.print("\n  Uninstall: [bold]zfs-unlock service uninstall[/bold]")


def _install_linux(argv: list[str]) -> None:
    """Install systemd user service on Linux."""
    service_name = "zfs-unlock.service"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dst = service_dir / service_name

    service_dir.mkdir(parents=True, exist_ok=True)
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    service_dst.write_text(_SYSTEMD_SERVICE.format(exec_start=shlex.join(argv), path=current_path))

    run_process(["systemctl", "--user", "daemon-reload"])
    run_process(["systemctl", "--user", "enable", "--now", "zfs-unlock"])

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

    run_process(["launchctl", "unload", str(plist_dst)], check=False)
    plist_dst.unlink()
    console.print("[green]OK[/green] Service uninstalled")


def _uninstall_linux() -> None:
    """Uninstall systemd user service on Linux."""
    service_dst = Path.home() / ".config" / "systemd" / "user" / "zfs-unlock.service"
    if not service_dst.exists():
        console.print("Service not installed.")
        return

    run_process(["systemctl", "--user", "stop", "zfs-unlock"], check=False)
    run_process(["systemctl", "--user", "disable", "zfs-unlock"], check=False)
    service_dst.unlink()
    run_process(["systemctl", "--user", "daemon-reload"])
    console.print("[green]OK[/green] Service uninstalled")


@service_app.command("status")
def service_status() -> None:
    """Check service status."""
    system = platform.system()
    if system == "Darwin":
        result = run_process(["launchctl", "list"], check=False)
        if "com.zfs_unlock" in result.stdout:
            console.print("[green]ACTIVE[/green] Service is running")
        else:
            console.print("[dim]INACTIVE[/dim] Service is not running")
    elif system == "Linux":
        result = run_process(["systemctl", "--user", "is-active", "zfs-unlock"], check=False)
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
