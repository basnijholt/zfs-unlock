"""Console output helpers."""

from __future__ import annotations

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def print_ok(message: str) -> None:
    """Print a successful diagnostic line."""
    console.print(f"[green]OK[/green] {message}", soft_wrap=True)


def print_fail(message: str) -> None:
    """Print a failed diagnostic line."""
    console.print(f"[red]FAIL[/red] {message}", soft_wrap=True)
