"""Restricted receiver implementation."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from .config import is_safe_dataset_name
from .constants import RECEIVER_UNLOCK_ARG_COUNT
from .process import CommandResult, LocalCommandRunner, LocalSubprocessRunner

if TYPE_CHECKING:
    from pathlib import Path


def parse_receiver_command(command: str) -> list[str]:
    """Parse an SSH_ORIGINAL_COMMAND string into receiver arguments."""
    return shlex.split(command)


class Receiver:
    """Restricted receiver for ZFS unlock commands."""

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

    def preflight(self, args: list[str]) -> CommandResult | None:
        """Validate receiver command metadata before reading stdin."""
        if not args:
            return self._error("missing command")

        command = args[0]
        if command in {"status", "unlock"} and len(args) == 2:  # noqa: PLR2004
            return self._preflight_dataset(args[1])
        if command == "lock" and len(args) in {2, 3}:
            if len(args) == 3 and args[2] != "--force":  # noqa: PLR2004
                return self._error("unsupported lock option")
            return self._preflight_dataset(args[1])
        return None

    @staticmethod
    def requires_stdin(args: list[str]) -> bool:
        """Return whether a validated receiver command needs stdin."""
        return len(args) == RECEIVER_UNLOCK_ARG_COUNT and args[0] == "unlock"

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

    def _preflight_dataset(self, dataset: str) -> CommandResult | None:
        if error := self._validate_dataset(dataset):
            return self._error(error)
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
            response = CommandResult(returncode=1, stdout="", stderr=result.stderr)
        else:
            mount = self.runner.run([self.zfs_path, "mount", "-a"])
            response = (
                CommandResult(returncode=0, stdout=f"unlocked {dataset}\n", stderr="")
                if mount.returncode == 0
                else CommandResult(returncode=1, stdout="", stderr=mount.stderr)
            )
        return response

    def _mounted_datasets(self, dataset: str) -> list[str] | CommandResult:
        listed = self.runner.run([self.zfs_path, "list", "-H", "-o", "name,mounted", "-r", dataset])
        if listed.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=listed.stderr)

        mounted_datasets: list[str] = []
        for line in listed.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 2:  # noqa: PLR2004
                return self._error(f"unexpected zfs list output: {line}")

            child_dataset, mounted = fields
            if mounted != "yes":
                continue
            if not is_safe_dataset_name(child_dataset):
                return self._error(f"unsafe dataset name from zfs list: {child_dataset}")
            if child_dataset != dataset and not child_dataset.startswith(f"{dataset}/"):
                return self._error(f"mounted dataset outside target subtree: {child_dataset}")
            mounted_datasets.append(child_dataset)

        return sorted(mounted_datasets, key=lambda name: name.count("/"))

    def _force_unmount_mounted_datasets(self, dataset: str) -> CommandResult | None:
        mounted_datasets = self._mounted_datasets(dataset)
        if isinstance(mounted_datasets, CommandResult):
            return mounted_datasets

        for mounted_dataset in reversed(mounted_datasets):
            unmount = self.runner.run([self.zfs_path, "unmount", "-f", mounted_dataset])
            if unmount.returncode != 0:
                return CommandResult(returncode=1, stdout="", stderr=unmount.stderr)

        return None

    def _lock(self, dataset: str, *, force: bool) -> CommandResult:
        if error := self._validate_dataset(dataset):
            return self._error(error)

        if force and (error_result := self._force_unmount_mounted_datasets(dataset)):
            return error_result

        result = self.runner.run([self.zfs_path, "unload-key", "-r", dataset])
        if result.returncode != 0:
            return CommandResult(returncode=1, stdout="", stderr=result.stderr)
        return CommandResult(returncode=0, stdout=f"locked {dataset}\n", stderr="")

    @staticmethod
    def _error(message: str) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr=f"{message}\n")
