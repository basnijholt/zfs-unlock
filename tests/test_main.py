"""Tests for the restricted receiver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from zfs_unlock.config import is_safe_dataset_name
from zfs_unlock.process import CommandResult
from zfs_unlock.receiver import Receiver, _ReceiverAction, _ReceiverRequest, parse_receiver_command

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


def test_receiver_rejects_empty_command(tmp_path: Path) -> None:
    """The receiver refuses an empty command without running anything."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.parse([])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "missing command" in response.stderr
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["reboot"],
        ["cat", "/etc/shadow"],
        ["unlock"],
    ],
)
def test_receiver_rejects_unsupported_command(tmp_path: Path, args: list[str]) -> None:
    """The receiver refuses verbs outside status/unlock/lock and malformed argument counts."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.parse(args)

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "unsupported command" in response.stderr
    assert runner.calls == []


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


@pytest.mark.parametrize(
    "args",
    [
        ["unlock", "tank/media"],
        ["lock", "tank/media"],
        ["lock", "tank/media", "--force"],
    ],
)
def test_receiver_parse_rejects_unlock_and_lock_dataset_not_in_allowlist(tmp_path: Path, args: list[str]) -> None:
    """Parsing refuses unlock/lock requests for datasets that are not allowlisted."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.parse(args)

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "not allowed" in response.stderr
    assert runner.calls == []


def test_receiver_missing_allow_file_rejects_all_datasets(tmp_path: Path) -> None:
    """A missing allowlist file fails closed and rejects every dataset."""
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=tmp_path / "missing-allowed-datasets", runner=runner)

    response = receiver.parse(["status", "tank/photos"])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "not allowed" in response.stderr
    assert runner.calls == []


def test_receiver_allowlist_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    """Allowlist parsing skips comments and blank lines and strips whitespace."""
    allow_file = tmp_path / "allowed-datasets"
    allow_file.write_text("# comment\ntank/photos\n\n   \n  tank/media  \n# tank/secret\n")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    assert receiver._allowed_datasets == frozenset({"tank/photos", "tank/media"})  # noqa: SLF001
    assert isinstance(receiver.parse(["status", "tank/photos"]), _ReceiverRequest)
    assert isinstance(receiver.parse(["status", "tank/media"]), _ReceiverRequest)

    response = receiver.parse(["status", "tank/secret"])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "not allowed" in response.stderr
    assert runner.calls == []


@pytest.mark.parametrize(
    "request_",
    [
        _ReceiverRequest(action=_ReceiverAction.UNLOCK, dataset="tank/media"),
        _ReceiverRequest(action=_ReceiverAction.LOCK, dataset="tank/media"),
        _ReceiverRequest(action=_ReceiverAction.LOCK, dataset="tank/media", force=True),
    ],
)
def test_receiver_handle_revalidates_forged_request_against_allowlist(
    tmp_path: Path,
    request_: _ReceiverRequest,
) -> None:
    """Handling re-checks the allowlist even for requests that bypassed parse validation."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner()
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = receiver.handle(request_, stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert "not allowed" in response.stderr


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


def test_receiver_status_reports_available_key_as_unlocked(tmp_path: Path) -> None:
    """An available ZFS key maps to the client status word `unlocked`."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="available\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "unlocked\n"


def test_receiver_status_reports_unrecognized_keystatus_as_unknown(tmp_path: Path) -> None:
    """A keystatus outside available/unavailable maps to `unknown`, not a guess."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="none\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "unknown\n"


def test_receiver_status_propagates_keystatus_failure(tmp_path: Path) -> None:
    """A failing zfs get surfaces as a nonzero exit with its stderr."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=1, stdout="", stderr="cannot open 'tank/photos': dataset does not exist\n"),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 1
    assert response.stdout == ""
    assert "dataset does not exist" in response.stderr


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
        CommandResult(returncode=0, stdout="/mnt/tank/photos\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/raw\n", stderr=""),
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
        (["zfs", "get", "-H", "-o", "value", "mountpoint", "tank/photos"], None),
        (["zfs", "get", "-H", "-o", "value", "mountpoint", "tank/photos/raw"], None),
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
        CommandResult(returncode=0, stdout="/mnt/tank/photos/raw\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert [call[0] for call in runner.calls[3:]] == [
        ["zfs", "get", "-H", "-o", "value", "mountpoint", "tank/photos/raw"],
        ["zfs", "mount", "tank/photos/raw"],
    ]


def test_receiver_unlock_fails_when_mount_fails(tmp_path: Path) -> None:
    """Receiver does not report unlock success if mounting fails afterwards."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos\n", stderr=""),
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
        (["zfs", "get", "-H", "-o", "value", "mountpoint", "tank/photos"], None),
        (["zfs", "mount", "tank/photos"], None),
    ]


def test_receiver_unlock_mounts_past_a_failed_mount(tmp_path: Path) -> None:
    """A failed mount skips only datasets mounted at or below its path; the others still mount."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(
            returncode=0,
            stdout=(
                "tank/photos\ton\tyes\tavailable\n"
                "tank/photos/a\ton\tno\tavailable\n"
                "tank/photos/a/child\ton\tno\tavailable\n"
                "tank/photos/a/child/grand\ton\tno\tavailable\n"
                "tank/photos/a/elsewhere\ton\tno\tavailable\n"
                "tank/photos/ab\ton\tno\tavailable\n"
                "tank/photos/b\ton\tno\tavailable\n"
                "tank/photos/c\ton\tno\tavailable\n"
                "tank/photos/d\ton\tno\tavailable\n"
            ),
            stderr="",
        ),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/a\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/a/child\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/a/child/grand\n", stderr=""),
        CommandResult(returncode=0, stdout="/srv/elsewhere\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/ab\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/a/b\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/c\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/d\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="cannot mount a\n"),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="cannot mount c\n"),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert response.stdout == ""
    assert response.stderr == "cannot mount a\ncannot mount c\n"
    assert [call[0] for call in runner.calls if call[0][1] == "mount"] == [
        ["zfs", "mount", "tank/photos/a/elsewhere"],
        ["zfs", "mount", "tank/photos/a"],
        ["zfs", "mount", "tank/photos/ab"],
        ["zfs", "mount", "tank/photos/c"],
        ["zfs", "mount", "tank/photos/d"],
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


def test_receiver_unlock_rejects_empty_passphrase(tmp_path: Path) -> None:
    """Receiver refuses to run load-key when stdin carried no passphrase."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(CommandResult(returncode=0, stdout="unavailable\n", stderr=""))
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="")

    assert response.returncode == 1
    assert "missing passphrase on stdin" in response.stderr
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
    ]


def test_receiver_unlock_skips_already_available_key(tmp_path: Path) -> None:
    """Receiver does not re-load an already available key, but reconciles mounts."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="available\n", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert response.stdout == "already unlocked tank/photos\n"
    assert runner.calls == [
        (["zfs", "get", "-H", "-o", "value", "keystatus", "tank/photos"], None),
        (["zfs", "list", "-H", "-o", "name,canmount,mounted,keystatus", "-r", "tank/photos"], None),
        (["zfs", "get", "-H", "-o", "value", "mountpoint", "tank/photos"], None),
        (["zfs", "mount", "tank/photos"], None),
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


def test_receiver_status_reports_unlocked_unmounted(tmp_path: Path) -> None:
    """Status distinguishes unlocked-but-unmounted so the client can remount."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="available\n", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos\n", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "unlocked-unmounted\n"


def test_receiver_status_reports_unlocked_when_fully_mounted(tmp_path: Path) -> None:
    """A fully mounted unlocked subtree reports plain unlocked."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="available\n", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tyes\tavailable\n", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "unlocked\n"


def test_receiver_unlock_does_not_forward_load_key_stderr(tmp_path: Path) -> None:
    """load-key stderr never reaches the SSH client; it could echo key material."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="Incorrect key for secret-pass\n"),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 1
    assert "secret-pass" not in response.stderr
    assert response.stderr == "load-key failed for tank/photos (exit 1)\n"


def test_receiver_allowlist_with_only_comments_rejects_all(tmp_path: Path) -> None:
    """An allowlist holding only comments and blanks allows nothing."""
    allow_file = tmp_path / "allowed-datasets"
    allow_file.write_text("# nothing enabled yet\n\n   \n# tank/photos\n")
    receiver = Receiver(allow_file=allow_file, runner=RecordingLocalRunner())

    response = receiver.parse(["status", "tank/photos"])

    assert isinstance(response, CommandResult)
    assert response.returncode == 1
    assert "dataset not allowed" in response.stderr


def test_receiver_unlock_mount_skips_none_and_legacy_mountpoints(tmp_path: Path) -> None:
    """Datasets without a ZFS-managed mountpoint are never mounted by the receiver.

    `zfs mount` rejects mountpoint=none and mountpoint=legacy, so each would
    fail every unlock.
    """
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(
            returncode=0,
            stdout=(
                "tank/photos\ton\tno\tavailable\n"
                "tank/photos/incus\ton\tno\tavailable\n"
                "tank/photos/mirror\ton\tno\tavailable\n"
                "tank/photos/raw\ton\tno\tavailable\n"
            ),
            stderr="",
        ),
        CommandResult(returncode=0, stdout="/mnt/tank/photos\n", stderr=""),
        CommandResult(returncode=0, stdout="legacy\n", stderr=""),
        CommandResult(returncode=0, stdout="none\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/tank/photos/raw\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert [call[0] for call in runner.calls if call[0][1] == "mount"] == [
        ["zfs", "mount", "tank/photos"],
        ["zfs", "mount", "tank/photos/raw"],
    ]


def test_receiver_status_ignores_none_and_legacy_mountpoints(tmp_path: Path) -> None:
    """Unmounted none/legacy datasets do not make an unlocked subtree look unmounted."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="available\n", stderr=""),
        CommandResult(
            returncode=0,
            stdout=(
                "tank/photos\ton\tyes\tavailable\n"
                "tank/photos/incus\ton\tno\tavailable\n"
                "tank/photos/mirror\ton\tno\tavailable\n"
            ),
            stderr="",
        ),
        CommandResult(returncode=0, stdout="legacy\n", stderr=""),
        CommandResult(returncode=0, stdout="none\n", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 0
    assert response.stdout == "unlocked\n"


def test_receiver_unlock_mounts_dataset_whose_mountpoint_has_tab_and_newline(tmp_path: Path) -> None:
    """A mountpoint with a tab or newline neither breaks the listing nor hides the dataset."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="unavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=0, stdout="/mnt/raw\tphotos\nnone\n", stderr=""),
        CommandResult(returncode=0, stdout="", stderr=""),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "unlock", "tank/photos", stdin_text="secret-pass\n")

    assert response.returncode == 0
    assert runner.calls[-1] == (["zfs", "mount", "tank/photos"], None)


def test_receiver_status_propagates_mountpoint_failure(tmp_path: Path) -> None:
    """A failing mountpoint lookup surfaces as an error instead of a guess."""
    allow_file = write_allowlist(tmp_path, "tank/photos")
    runner = RecordingLocalRunner(
        CommandResult(returncode=0, stdout="available\n", stderr=""),
        CommandResult(returncode=0, stdout="tank/photos\ton\tno\tavailable\n", stderr=""),
        CommandResult(returncode=1, stdout="", stderr="get failed\n"),
    )
    receiver = Receiver(allow_file=allow_file, runner=runner)

    response = handle_request(receiver, "status", "tank/photos")

    assert response.returncode == 1
    assert response.stderr == "get failed\n"
