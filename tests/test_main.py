"""Tests for the restricted receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zfs_unlock import CommandResult, Receiver, is_safe_dataset_name, parse_receiver_command

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

    response = receiver.handle(["status", "tank/media"], stdin_text="")

    assert response.returncode == 1
    assert "not allowed" in response.stderr
    assert runner.calls == []


def test_receiver_status_maps_keystatus(tmp_path: Path) -> None:
    """Receiver maps ZFS keystatus values to client status words."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="unavailable\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.handle(["status", "tank/photos"], stdin_text="")

    assert response.returncode == 0
    assert response.stdout == "locked\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
    ]


def test_receiver_unlock_loads_key_from_stdin_and_mounts(tmp_path: Path) -> None:
    """Receiver unlocks with stdin passphrase and mounts datasets afterwards."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.handle(["unlock", "tank/photos"], stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert response.stdout == "unlocked tank/photos\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
        (["zfs", "load-key", "-L", "prompt", "tank/photos"], "secret-pass\n"),
        (["zfs", "mount", "-a"], None),
    ]


def test_receiver_unlock_fails_when_mount_fails(tmp_path: Path) -> None:
    """Receiver does not report unlock success if mounting fails afterwards."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="mount failed\n"),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.handle(["unlock", "tank/photos"], stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert response.stderr == "mount failed\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
        (["zfs", "load-key", "-L", "prompt", "tank/photos"], "secret-pass\n"),
        (["zfs", "mount", "-a"], None),
    ]


def test_receiver_unlock_skips_already_available_key(tmp_path: Path) -> None:
    """Receiver does not re-load an already available key."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="available\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.handle(["unlock", "tank/photos"], stdin_text="secret-pass\n")

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

    response = receiver.handle(["lock", "tank/photos", "--force"], stdin_text="")

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

    response = receiver.handle(["lock", "tank/photos", "--force"], stdin_text="")

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

    response = receiver.handle(["lock", "tank/photos", "--force"], stdin_text="")

    assert response.returncode == 1
    assert "outside target subtree" in response.stderr
    assert runner.calls == [
        (["zfs", "list", "-H", "-o", "name,mounted", "-r", "tank/photos"], None),
    ]
