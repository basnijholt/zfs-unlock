"""Local service installation helpers."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Annotated

import typer

from .config import find_config
from .output import console, err_console
from .process import run_process

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

service_app = typer.Typer(help="Manage system service", no_args_is_help=True)


def _get_uv_path() -> Path | None:
    """Find uv executable."""
    uv = shutil.which("uv")
    return Path(uv) if uv else None


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
    run_process(["launchctl", "load", str(plist_dst)])

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
