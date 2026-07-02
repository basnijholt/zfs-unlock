"""Client-side diagnostics for zfs-unlock."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from pathlib import Path
from typing import Annotated

import typer

from .client import ZfsUnlockClient
from .config import Config, Dataset, SecretsMode, load_config
from .output import console, print_fail, print_ok


def _is_private_file(path: Path) -> bool:
    """Return whether a file is unreadable and unwritable by group/others."""
    if os.name == "nt":
        return True
    return path.stat().st_mode & 0o077 == 0


def _check_private_file(path: Path, label: str) -> bool:
    """Check that a sensitive file exists and has private permissions."""
    if not path.exists():
        print_fail(f"{label} missing: {path}")
        return False
    if not _is_private_file(path):
        print_fail(f"{label} permissions too open: {path}")
        return False
    print_ok(f"{label} permissions private: {path}")
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
        path = Path(dataset.secret.get_secret_value()).expanduser()
        if config.secrets == SecretsMode.INLINE:
            continue
        if config.secrets == SecretsMode.AUTO and not path.exists():
            continue
        if not path.exists():
            print_fail(f"secret file missing for {dataset.path}: {path}")
            ok = False
            continue
        if not _is_private_file(path):
            print_fail(f"secret file permissions too open for {dataset.path}: {path}")
            ok = False
            continue
        print_ok(f"secret file permissions private for {dataset.path}: {path}")
    return ok


def _check_ssh_executable() -> bool:
    ssh_path = shutil.which("ssh")
    if ssh_path is None:
        print_fail("ssh executable missing from PATH")
        return False

    print_ok(f"ssh executable exists: {ssh_path}")
    return True


def _check_host_reachable(config: Config) -> bool:
    ok = True
    try:
        socket.getaddrinfo(config.host, config.port)
    except OSError as exc:
        print_fail(f"host resolution failed for {config.host}: {exc}")
        ok = False
    else:
        print_ok(f"host resolves: {config.host}")

    try:
        with socket.create_connection((config.host, config.port), timeout=config.connect_timeout):
            pass
    except OSError as exc:
        print_fail(f"tcp connect failed for {config.host}:{config.port}: {exc}")
        ok = False
    else:
        print_ok(f"tcp connect ok: {config.host}:{config.port}")

    return ok


def _select_doctor_datasets(datasets: list[Dataset], dataset: str | None) -> list[Dataset]:
    if dataset is None:
        return datasets

    for configured in datasets:
        if configured.path == dataset:
            return [configured]

    print_fail(f"dataset not configured: {dataset}")
    raise typer.Exit(1)


async def _check_receiver_status(client: ZfsUnlockClient, dataset: Dataset) -> bool:
    console.print(f"[dim]checking receiver status: {dataset.path}[/dim]", soft_wrap=True)
    result = await client.run_remote(["status", dataset.path])
    if result.returncode != 0:
        stderr = result.stderr.strip()
        print_fail(f"receiver status failed: {dataset.path}: {stderr}")
        if "Host key verification failed" in stderr:
            config = client.config
            console.print(
                "[yellow]hint:[/yellow] the receiver host key is not in known_hosts yet; run"
                f" [bold]ssh-keyscan -p {config.port} {config.host} >> ~/.ssh/known_hosts[/bold]"
                " after verifying the fingerprint out of band",
                soft_wrap=True,
            )
        return False

    status = result.stdout.strip()
    if status in {"locked", "unlocked"}:
        print_ok(f"receiver status ok: {dataset.path} -> {status}")
        return True

    print_fail(f"receiver status unexpected: {dataset.path} -> {status}")
    return False


async def _check_receiver_statuses(config: Config, datasets: list[Dataset]) -> bool:
    client = ZfsUnlockClient(config)
    results = await asyncio.gather(
        *[_check_receiver_status(client, dataset) for dataset in datasets],
        return_exceptions=True,
    )
    ok = True
    for dataset, result in zip(datasets, results, strict=True):
        if isinstance(result, BaseException):
            print_fail(f"receiver status check failed for {dataset.path}: {result!r}")
            ok = False
        elif not result:
            ok = False
    return ok


def doctor(
    config_path: Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")] = None,
    dataset: Annotated[str | None, typer.Option("--dataset", "-D", help="Dataset to check")] = None,
) -> None:
    """Check client config, SSH key, host reachability, and receiver status."""
    config_path, config = load_config(config_path)
    console.print(f"[dim]{config_path}[/dim]", soft_wrap=True)
    print_ok("config parsed")

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
