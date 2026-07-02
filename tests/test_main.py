"""Tests for the restricted receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from zfs_unlock.config import is_safe_dataset_name
from zfs_unlock.process import CommandResult
from zfs_unlock.receiver import Receiver, _ReceiverRequest, parse_receiver_command

if TYPE_CHECKING:
    from pathlib import Path


class RecordingLocalRunner:
    """Sync runner that records local ZFS calls."""

    def __init__(self, *results: CommandResult) -> None:
        """Initialize with queued command results."""
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Record a command and return the next queued result."""
        self.calls.append((args, input_text))
        if not self.results:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self.results.pop(0)


def write_allowlist(tmp_path: Path, *datasets: str) -> Path:
    """Write an allowlist file and return its path."""
    allow_file = tmp_path / "allowed-datasets"
    allow_file.write_text("\n".join(datasets) + "\n")
    return allow_file


def parse_request(receiver: Receiver, *args: str) -> _ReceiverRequest:
    """Parse raw receiver args and assert they are accepted."""
    request = receiver.parse(list(args))
    assert isinstance(request, _ReceiverRequest), request.stderr
    return request


def handle_request(receiver: Receiver, *args: str, stdin_text: str = "") -> CommandResult:
    """Parse and handle a receiver request."""
    return receiver.handle(parse_request(receiver, *args), stdin_text=stdin_text)


def test_is_safe_dataset_name_accepts_zfs_dataset_paths() -> None:
    """Normal ZFS dataset names are accepted."""
    assert is_safe_dataset_name("tank/photos")
    assert is_safe_dataset_name("ssd/.ix-virt/containers")
    assert is_safe_dataset_name("tank/data_2026-06-28")


def test_is_safe_dataset_name_rejects_shell_like_input() -> None:
    """Unsafe dataset-looking command input is rejected."""
    assert not is_safe_dataset_name("")
    assert not is_safe_dataset_name("-tank/photos")
    assert not is_safe_dataset_name("tank/photos;reboot")
    assert not is_safe_dataset_name("tank/photos with spaces")
    assert not is_safe_dataset_name("../tank/photos")


def test_parse_receiver_command_uses_shell_words() -> None:
    """Receiver commands are parsed with shell-compatible quoting."""
    assert parse_receiver_command("status tank/photos") == ["status", "tank/photos"]
    assert parse_receiver_command("lock tank/photos --force") == ["lock", "tank/photos", "--force"]


def test_receiver_rejects_dataset_not_in_allowlist(tmp_path: Path) -> None:
    """The receiver refuses datasets that are not explicitly allowlisted."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.parse(["status", "tank/media"])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "not allowed" in response.stderr
    assert runner.calls == []


@pytest.mark.parametrize("action", ["status", "unlock", "lock"])
@pytest.mark.parametrize("dataset", ["-L", "../etc/shadow", "tank/x;reboot"])
def test_receiver_rejects_unsafe_dataset_name_even_when_allowlisted(
    tmp_path: Path,
    action: str,
    dataset: str,
) -> None:
    """An unsafe dataset name is refused before the allowlist check, even if listed.

    ``is_safe_dataset_name`` re-validates the allowlist file contents so an
    option-looking or traversal token can never reach a root ``zfs`` invocation
    as an operand. Deleting that guard from ``_validate_dataset`` makes this
    test fail while the rest of the suite stays green.
    """
    allow_file = write_allowlist(tmp_path, dataset)
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.parse([action, dataset])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "unsafe dataset name" in response.stderr
    assert runner.calls == []


def test_receiver_status_maps_keystatus(tmp_path: Path) -> None:
    """Receiver maps ZFS keystatus values to client status words."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="unavailable\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "locked\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
    ]


def test_receiver_unlock_loads_key_from_stdin_and_mounts_subtree(tmp_path: Path) -> None:
    """Receiver unlocks with stdin passphrase and mounts only the target subtree."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(
            returncode=0,
            stdout=(
                "tank/photos\ton\tno\tavailable\n"
                "tank/photos/raw\ton\tno\tavailable\n"
                "tank/photos/cache\toff\tno\tavailable\n"
            ),
            stderr="",
        ),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert response.stdout == "unlocked tank/photos\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
        (["zfs", "load-key", "-L", "prompt", "tank/photos"], "secret-pass\n"),
        (["zfs", "list", "-H", "-o", "name,canmount,mounted,keystatus", "-r", "tank/photos"], None),
        (["zfs", "mount", "tank/photos"], None),
        (["zfs", "mount", "tank/photos/raw"], None),
    ]


def test_receiver_unlock_mount_skips_mounted_and_locked_children(tmp_path: Path) -> None:
    """Subtree mounting skips already mounted datasets and nested locked encryption roots."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(
            returncode=0,
            stdout=(
                "tank/photos\ton\tyes\tavailable\n"
                "tank/photos/raw\ton\tno\tavailable\n"
                "tank/photos/vault\ton\tno\tunavailable\n"
            ),
            stderr="",
        ),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert [call[0] for call in runner.calls[3:]] == [["zfs", "mount", "tank/photos/raw"]]


def test_receiver_unlock_fails_when_mount_fails(tmp_path: Path) -> None:
    """Receiver does not report unlock success if mounting fails afterwards."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="mount failed\n"),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert response.stderr == "mount failed\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
        (["zfs", "load-key", "-L", "prompt", "tank/photos"], "secret-pass\n"),
        (["zfs", "list", "-H", "-o", "name,canmount,mounted,keystatus", "-r", "tank/photos"], None),
        (["zfs", "mount", "tank/photos"], None),
    ]


def test_receiver_unlock_rejects_subtree_listing_outside_target(tmp_path: Path) -> None:
    """Subtree mounting refuses zfs list output outside the unlocked dataset."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="tank/other\ton\tno\tavailable\n", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert "outside target subtree" in response.stderr
    assert len(runner.calls) == 3  # noqa: PLR2004


def test_receiver_unlock_skips_already_available_key(tmp_path: Path) -> None:
    """Receiver does not re-load an already available key."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="available\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert response.stdout == "already unlocked tank/photos\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
    ]


def test_receiver_lock_force_unmounts_descendants_then_unloads_key(tmp_path: Path) -> None:
    """Forced lock unmounts mounted descendants before unloading the key."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(
            returncode=0,
            stdout="tank/photos\tyes\ntank/photos/raw\tyes\ntank/photos/cache\tno\n",
            stderr="",
        ),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "lock", "tank/photos", "--force")

    assert response.returncode == 0
    assert response.stdout == "locked tank/photos\n"
    assert runner.calls == [
        (["zfs", "list", "-H", "-o", "name,mounted", "-r", "tank/photos"], None),
        (["zfs", "unmount", "-f", "tank/photos/raw"], None),
        (["zfs", "unmount", "-f", "tank/photos"], None),
        (["zfs", "unload-key", "-r", "tank/photos"], None),
    ]


def test_receiver_lock_force_stops_when_descendant_listing_fails(tmp_path: Path) -> None:
    """Forced lock does not unload keys after a failed descendant listing."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=1, stdout="", stderr="list failed\n"))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "lock", "tank/photos", "--force")

    assert response.returncode == 1
    assert response.stderr == "list failed\n"
    assert runner.calls == [
        (["zfs", "list", "-H", "-o", "name,mounted", "-r", "tank/photos"], None),
    ]


def test_receiver_lock_force_rejects_unrelated_mounted_dataset(tmp_path: Path) -> None:
    """Forced lock refuses unexpected zfs list output outside the target subtree."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="tank/photos\tyes\ntank/other\tyes\n", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "lock", "tank/photos", "--force")

    assert response.returncode == 1
    assert "outside target subtree" in response.stderr
    assert runner.calls == [
        (["zfs", "list", "-H", "-o", "name,mounted", "-r", "tank/photos"], None),
    ]
