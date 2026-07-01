"""Restricted receiver implementation."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING

from .config import is_safe_dataset_name
from .process import CommandResult, LocalCommandRunner, LocalSubprocessRunner

if TYPE_CHECKING:
    from pathlib import Path


def parse_receiver_command(command: str) -> list[str]:
    """Parse an SSH_ORIGINAL_COMMAND string into receiver arguments."""
    return shlex.split(command)


_DATASET_COMMAND_ARG_COUNT = 2
_LOCK_COMMAND_ARG_COUNTS = {2, 3}
_LOCK_FORCE_ARG_COUNT = 3
_FORCE_LOCK_OPTION = "--force"


class _ReceiverAction(StrEnum):
    """Receiver actions accepted over the forced SSH command."""

    STATUS = "status"
    UNLOCK = "unlock"
    LOCK = "lock"


@dataclass(frozen=True)
class _ReceiverRequest:
    """Validated receiver request."""

    action: _ReceiverAction
    dataset: str
    force: bool = False

    @property
    def requires_stdin(self) -> bool:
        """Return whether the request needs stdin from the SSH client."""
        return self.action == _ReceiverAction.UNLOCK


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

    def parse(self, args: list[str]) -> _ReceiverRequest | CommandResult:
        """Validate raw receiver arguments before reading stdin."""
        if not args:
            return self._error("missing command")

        try:
            action = _ReceiverAction(args[0])
        except ValueError:
            return self._error("unsupported command")

        if action in {_ReceiverAction.STATUS, _ReceiverAction.UNLOCK}:
            return self._parse_dataset_request(action, args)
        if action == _ReceiverAction.LOCK:
            return self._parse_lock_request(args)
        return self._error("unsupported command")

    def _parse_dataset_request(self, action: _ReceiverAction, args: list[str]) -> _ReceiverRequest | CommandResult:
        if len(args) != _DATASET_COMMAND_ARG_COUNT:
            return self._error("unsupported command")
        return self._validated_request(action, args[1])

    def _parse_lock_request(self, args: list[str]) -> _ReceiverRequest | CommandResult:
        if len(args) not in _LOCK_COMMAND_ARG_COUNTS:
            return self._error("unsupported command")

        force = len(args) == _LOCK_FORCE_ARG_COUNT
        if force and args[2] != _FORCE_LOCK_OPTION:
            return self._error("unsupported lock option")
        return self._validated_request(_ReceiverAction.LOCK, args[1], force=force)

    def _validated_request(
        self,
        action: _ReceiverAction,
        dataset: str,
        *,
        force: bool = False,
    ) -> _ReceiverRequest | CommandResult:
        if error := self._validate_dataset(dataset):
            return self._error(error)
        return _ReceiverRequest(action=action, dataset=dataset, force=force)

    def handle(self, request: _ReceiverRequest, *, stdin_text: str) -> CommandResult:
        """Handle a restricted receiver command."""
        if request.action == _ReceiverAction.STATUS:
            return self._status(request.dataset)
        if request.action == _ReceiverAction.UNLOCK:
            return self._unlock(request.dataset, stdin_text=stdin_text)
        if request.action == _ReceiverAction.LOCK:
            return self._lock(request.dataset, force=request.force)
        return self._error("unsupported command")

    @cached_property
    def _allowed_datasets(self) -> frozenset[str]:
        if not self.allow_file.exists():
            return frozenset()

        datasets: set[str] = set()
        for line in self.allow_file.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                datasets.add(stripped)
        return frozenset(datasets)

    def _validate_dataset(self, dataset: str) -> str | None:
        if not is_safe_dataset_name(dataset):
            return f"unsafe dataset name: {dataset}"
        if dataset not in self._allowed_datasets:
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
