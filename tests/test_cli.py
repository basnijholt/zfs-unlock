"""Tests for CLI functionality."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from zfs_unlock.cli import _receiver, app
from zfs_unlock.client import UnlockOutcome, _filter_datasets
from zfs_unlock.config import Dataset, find_config

runner = CliRunner()
DAEMON_RUN_CALLS = 3
CLICK_USAGE_ERROR = 2
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def write_private_file(path: Path, text: str = "secret") -> Path:
    """Write a test secret file with private permissions."""
    path.write_text(text)
    path.chmod(0o600)
    return path


class TestFilterDatasets:
    """Tests for dataset filtering."""

    def test_no_filter_returns_all(self) -> None:
        """No filter returns all datasets."""
        datasets = [
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/syncthing", secret="pass2"),
        ]

        assert _filter_datasets(datasets, None) == datasets

    def test_single_filter_partial_match(self) -> None:
        """Single filter matches partial path."""
        datasets = [
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/syncthing", secret="pass2"),
            Dataset(path="tank/frigate", secret="pass3"),
        ]

        result = _filter_datasets(datasets, ["photos"])

        assert len(result) == 1
        assert result[0].path == "tank/photos"


class TestFindConfig:
    """Tests for find_config function."""

    def test_finds_config_yaml_in_cwd(self, tmp_path: Path, monkeypatch: object) -> None:
        """Find config.yaml in current directory."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("host: test")

        with patch("zfs_unlock.config.CONFIG_SEARCH_PATHS", [Path("config.yaml"), Path("config.yml")]):
            assert find_config() == Path("config.yaml")

    def test_returns_none_when_no_config(self, tmp_path: Path, monkeypatch: object) -> None:
        """Return None when no config file exists."""
        monkeypatch.chdir(tmp_path)

        with patch("zfs_unlock.config.CONFIG_SEARCH_PATHS", [Path("config.yaml"), Path("config.yml")]):
            assert find_config() is None


def test_cli_help() -> None:
    """Test CLI help command."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Unlock OpenZFS datasets" in result.stdout


def test_cli_help_groups_commands() -> None:
    """Top-level help groups commands by operator workflow."""
    result = runner.invoke(app, ["--help"], color=True)
    stdout = ANSI_RE.sub("", result.stdout)

    assert result.exit_code == 0
    assert "Client Commands" in stdout
    assert "Setup Commands" in stdout
    assert "Receiver Commands" in stdout
    assert "Service Commands" in stdout
    assert stdout.index("Client Commands") < stdout.index("Setup Commands")
    assert stdout.index("Setup Commands") < stdout.index("Receiver Commands")
    assert stdout.index("Receiver Commands") < stdout.index("Service Commands")
    command_rows = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("│ "):
            continue

        command = stripped.removeprefix("│ ").split()[0]
        if command in {"unlock", "lock", "status", "doctor", "keygen", "receiver", "service"}:
            command_rows.append(command)

    assert command_rows == ["unlock", "lock", "status", "doctor", "keygen", "receiver", "service"]


def test_cli_without_subcommand_shows_help() -> None:
    """Bare invocation shows help and does not unlock datasets."""
    with patch("zfs_unlock.cli.run_unlock") as run_unlock:
        result = runner.invoke(app)

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "unlock" in result.stdout
    run_unlock.assert_not_called()


def test_cli_missing_config() -> None:
    """Test CLI fails without config."""
    with patch("zfs_unlock.config.find_config", return_value=None):
        result = runner.invoke(app, ["unlock"])

    assert result.exit_code == 1
    assert "Config not found" in result.stderr


def test_cli_invalid_config_reports_clean_error(tmp_path: Path) -> None:
    """Invalid config files fail with a concise message."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("- host: zfs-host.example.lan\n")

    result = runner.invoke(app, ["unlock", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "Invalid config" in result.stderr


def test_cli_malformed_yaml_reports_clean_error(tmp_path: Path) -> None:
    """YAML syntax errors fail with the same concise config message."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: [zfs-host.example.lan\n")

    result = runner.invoke(app, ["unlock", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "Invalid config" in result.stderr


def test_cli_with_config(tmp_path: Path) -> None:
    """Test CLI runs with config file."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_unlock(*args: object, **kwargs: object) -> UnlockOutcome:
        calls.append((args, kwargs))
        return UnlockOutcome.OK

    with patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock):
        result = runner.invoke(app, ["unlock", "--config", str(config_file)])

    assert result.exit_code == 0
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", [UnlockOutcome.FAILED, UnlockOutcome.UNREACHABLE])
def test_cli_unlock_exits_nonzero_when_run_fails(tmp_path: Path, outcome: UnlockOutcome) -> None:
    """The unlock command reflects failed unlock work in its exit code."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    async def fake_run_unlock(*_args: object, **_kwargs: object) -> UnlockOutcome:
        return outcome

    with patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock):
        result = runner.invoke(app, ["unlock", "--config", str(config_file)])

    assert result.exit_code == 1


def test_cli_daemon_mode(tmp_path: Path) -> None:
    """Daemon polls fast only while the receiver is unreachable."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    fake_run_unlock = MagicMock(return_value=object())
    with (
        patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock),
        patch("asyncio.run") as mock_run,
        patch("time.sleep") as mock_sleep,
    ):
        mock_run.side_effect = [UnlockOutcome.OK, UnlockOutcome.UNREACHABLE, KeyboardInterrupt]
        result = runner.invoke(app, ["unlock", "--config", str(config_file), "--daemon", "--interval", "10"])

    assert result.exit_code == 0
    assert mock_run.call_count == DAEMON_RUN_CALLS
    mock_sleep.assert_any_call(10)
    mock_sleep.assert_any_call(1)


def test_cli_daemon_does_not_claim_restored_on_failed_pass(tmp_path: Path) -> None:
    """Recovering reachability with a still-failing pass must not print success."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    fake_run_unlock = MagicMock(return_value=object())
    with (
        patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock),
        patch("asyncio.run") as mock_run,
        patch("time.sleep"),
    ):
        mock_run.side_effect = [UnlockOutcome.UNREACHABLE, UnlockOutcome.FAILED, KeyboardInterrupt]
        result = runner.invoke(app, ["unlock", "--config", str(config_file), "--daemon"])

    stdout = ANSI_RE.sub("", result.stdout)
    assert result.exit_code == 0
    assert "Connection restored" not in stdout
    assert "reachable again, but the unlock pass failed" in stdout


def test_cli_daemon_keeps_normal_interval_on_non_connection_failures(tmp_path: Path) -> None:
    """Persistent non-network failures must not hammer the receiver at 1s."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    fake_run_unlock = MagicMock(return_value=object())
    with (
        patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock),
        patch("asyncio.run") as mock_run,
        patch("time.sleep") as mock_sleep,
    ):
        mock_run.side_effect = [UnlockOutcome.FAILED, UnlockOutcome.FAILED, KeyboardInterrupt]
        result = runner.invoke(app, ["unlock", "--config", str(config_file), "--daemon", "--interval", "10"])

    assert result.exit_code == 0
    assert mock_sleep.call_count == 2  # noqa: PLR2004
    assert all(call.args == (10,) for call in mock_sleep.call_args_list)


@pytest.mark.parametrize("interval", ["0", "-1"])
def test_cli_daemon_rejects_non_positive_interval(tmp_path: Path, interval: str) -> None:
    """Daemon mode requires a positive polling interval."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls = 0

    async def fake_run_unlock(*_args: object, **_kwargs: object) -> UnlockOutcome:
        nonlocal calls
        calls += 1
        return UnlockOutcome.OK

    with (
        patch("zfs_unlock.cli.run_unlock", new=fake_run_unlock),
        patch("time.sleep", side_effect=KeyboardInterrupt),
    ):
        result = runner.invoke(app, ["unlock", "--config", str(config_file), "--daemon", "--interval", interval])

    assert result.exit_code == CLICK_USAGE_ERROR
    assert calls == 0


def test_cli_lock_command(tmp_path: Path) -> None:
    """Test CLI lock command."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_lock(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        return True

    with patch("zfs_unlock.cli.run_lock", new=fake_run_lock):
        result = runner.invoke(app, ["lock", "--config", str(config_file), "--force"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1]["force"] is True


def test_cli_lock_exits_nonzero_when_run_fails(tmp_path: Path) -> None:
    """The lock command reflects failed lock work in its exit code."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    async def fake_run_lock(*_args: object, **_kwargs: object) -> bool:
        return False

    with patch("zfs_unlock.cli.run_lock", new=fake_run_lock):
        result = runner.invoke(app, ["lock", "--config", str(config_file)])

    assert result.exit_code == 1


def test_cli_status_command(tmp_path: Path) -> None:
    """Test CLI status command."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_status(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        return True

    with patch("zfs_unlock.cli.run_status", new=fake_run_status):
        result = runner.invoke(app, ["status", "--config", str(config_file), "--dataset", "tank/ds"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1]["dataset_filters"] == ["tank/ds"]


def test_cli_status_exits_nonzero_when_run_fails(tmp_path: Path) -> None:
    """The status command reflects failed status checks in its exit code."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    async def fake_run_status(*_args: object, **_kwargs: object) -> bool:
        return False

    with patch("zfs_unlock.cli.run_status", new=fake_run_status):
        result = runner.invoke(app, ["status", "--config", str(config_file)])

    assert result.exit_code == 1


def test_cli_receiver_uses_ssh_original_command(tmp_path: Path) -> None:
    """Receiver command reads SSH_ORIGINAL_COMMAND when no args are provided."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")
    request = SimpleNamespace(requires_stdin=False)

    with (
        patch.dict("os.environ", {"SSH_ORIGINAL_COMMAND": "status tank/photos"}),
        patch("zfs_unlock.cli.Receiver") as receiver_cls,
    ):
        receiver_cls.return_value.parse.return_value = request
        receiver_cls.return_value.handle.return_value = response
        result = runner.invoke(app, ["receiver", "--allow-file", str(allow_file)])

    assert result.exit_code == 0
    assert result.stdout == "locked\n"
    receiver_cls.return_value.parse.assert_called_once_with(["status", "tank/photos"])
    receiver_cls.return_value.handle.assert_called_once_with(request, stdin_text="")


def test_cli_receiver_parses_single_wrapped_command_argument(tmp_path: Path) -> None:
    """Receiver command parses one shell command argument from a sudo wrapper."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")
    request = SimpleNamespace(requires_stdin=False)

    with patch("zfs_unlock.cli.Receiver") as receiver_cls:
        receiver_cls.return_value.parse.return_value = request
        receiver_cls.return_value.handle.return_value = response
        result = runner.invoke(app, ["receiver", "--allow-file", str(allow_file), "status tank/photos"])

    assert result.exit_code == 0
    assert result.stdout == "locked\n"
    receiver_cls.return_value.parse.assert_called_once_with(["status", "tank/photos"])
    receiver_cls.return_value.handle.assert_called_once_with(request, stdin_text="")


def test_cli_receiver_reports_malformed_wrapped_command(tmp_path: Path) -> None:
    """Malformed shell quoting in a wrapped receiver command is reported cleanly."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")

    result = runner.invoke(app, ["receiver", "--allow-file", str(allow_file), 'status "tank/photos'])

    assert result.exit_code == 1
    assert "invalid receiver command" in result.stderr


def test_cli_receiver_status_does_not_read_stdin(tmp_path: Path) -> None:
    """Receiver status must not block on stdin from an interactive SSH client."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")
    request = SimpleNamespace(requires_stdin=False)

    with (
        patch("zfs_unlock.cli.sys.stdin.read", side_effect=AssertionError("stdin should not be read")),
        patch("zfs_unlock.cli.Receiver") as receiver_cls,
    ):
        receiver_cls.return_value.parse.return_value = request
        receiver_cls.return_value.handle.return_value = response
        with pytest.raises(typer.Exit) as exc_info:
            _receiver(SimpleNamespace(args=["status tank/photos"]), allow_file=allow_file)

    assert exc_info.value.exit_code == 0
    receiver_cls.return_value.parse.assert_called_once_with(["status", "tank/photos"])
    receiver_cls.return_value.handle.assert_called_once_with(request, stdin_text="")


def test_cli_receiver_unlock_reads_stdin(tmp_path: Path) -> None:
    """Receiver unlock reads stdin so the passphrase reaches zfs load-key."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="unlocked\n", stderr="")
    request = SimpleNamespace(requires_stdin=True)

    with (
        patch("zfs_unlock.cli.sys.stdin.read", return_value="secret\n") as stdin_read,
        patch("zfs_unlock.cli.Receiver") as receiver_cls,
    ):
        receiver_cls.return_value.parse.return_value = request
        receiver_cls.return_value.handle.return_value = response
        with pytest.raises(typer.Exit) as exc_info:
            _receiver(SimpleNamespace(args=["unlock tank/photos"]), allow_file=allow_file)

    assert exc_info.value.exit_code == 0
    stdin_read.assert_called_once_with()
    receiver_cls.return_value.parse.assert_called_once_with(["unlock", "tank/photos"])
    receiver_cls.return_value.handle.assert_called_once_with(request, stdin_text="secret\n")


def test_cli_receiver_disallowed_unlock_does_not_read_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Receiver rejects disallowed unlocks before reading passphrase stdin."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")

    with (
        patch("zfs_unlock.cli.sys.stdin.read", side_effect=AssertionError("stdin should not be read")),
        pytest.raises(typer.Exit) as exc_info,
    ):
        _receiver(SimpleNamespace(args=["unlock tank/media"]), allow_file=allow_file)

    assert exc_info.value.exit_code == 1
    assert "not allowed" in capsys.readouterr().err


def test_cli_receiver_passes_zfs_path(tmp_path: Path) -> None:
    """Receiver command can use an explicit zfs executable path."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")
    request = SimpleNamespace(requires_stdin=False)

    with patch("zfs_unlock.cli.Receiver") as receiver_cls:
        receiver_cls.return_value.parse.return_value = request
        receiver_cls.return_value.handle.return_value = response
        result = runner.invoke(
            app,
            [
                "receiver",
                "--allow-file",
                str(allow_file),
                "--zfs-path",
                "/run/current-system/sw/bin/zfs",
                "status tank/photos",
            ],
        )

    assert result.exit_code == 0
    receiver_cls.assert_called_once_with(allow_file=allow_file, zfs_path="/run/current-system/sw/bin/zfs")
    receiver_cls.return_value.parse.assert_called_once_with(["status", "tank/photos"])
    receiver_cls.return_value.handle.assert_called_once_with(request, stdin_text="")


def test_doctor_reports_missing_identity_file(tmp_path: Path) -> None:
    """Doctor reports the configured identity file before trying SSH."""
    config_file = tmp_path / "config.yaml"
    missing_key = tmp_path / "missing-key"
    config_file.write_text(f"host: 192.0.2.1\nidentity_file: {missing_key}\ndatasets:\n  tank/ds: pass")

    result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "identity file missing" in result.stdout
    assert str(missing_key) in result.stdout


def test_doctor_checks_receiver_status(tmp_path: Path) -> None:
    """Doctor checks the receiver using a status command for one dataset."""
    config_file = tmp_path / "config.yaml"
    key = write_private_file(tmp_path / "zfs-unlock-receiver")
    config_file.write_text(f"host: 192.0.2.1\nidentity_file: {key}\ndatasets:\n  tank/ds: pass")

    with (
        patch("socket.getaddrinfo", return_value=[object()]),
        patch("socket.create_connection") as create_connection,
        patch("zfs_unlock.diagnostics.ZfsUnlockClient") as client_cls,
    ):
        create_connection.return_value.__enter__.return_value = object()
        client_cls.return_value.run_remote = AsyncMock(
            return_value=MagicMock(returncode=0, stdout="locked\n", stderr=""),
        )
        result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 0
    client_cls.return_value.run_remote.assert_called_once_with(["status", "tank/ds"])
    assert "checking receiver status: tank/ds" in result.stdout
    assert "receiver status ok" in result.stdout


def test_doctor_reports_missing_ssh_executable(tmp_path: Path) -> None:
    """Doctor reports when ssh is unavailable in the current environment."""
    config_file = tmp_path / "config.yaml"
    key = write_private_file(tmp_path / "zfs-unlock-receiver")
    config_file.write_text(f"host: 192.0.2.1\nidentity_file: {key}\ndatasets:\n  tank/ds: pass")

    with (
        patch("shutil.which", return_value=None),
        patch("socket.getaddrinfo", return_value=[object()]),
        patch("socket.create_connection") as create_connection,
        patch("zfs_unlock.diagnostics.ZfsUnlockClient") as client_cls,
    ):
        create_connection.return_value.__enter__.return_value = object()
        client_cls.return_value.run_remote = AsyncMock(
            return_value=MagicMock(returncode=0, stdout="locked\n", stderr=""),
        )
        result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "ssh executable missing" in result.stdout
    client_cls.return_value.run_remote.assert_not_called()


def test_doctor_checks_all_configured_datasets(tmp_path: Path) -> None:
    """Doctor checks every configured dataset by default."""
    config_file = tmp_path / "config.yaml"
    key = write_private_file(tmp_path / "zfs-unlock-receiver")
    config_file.write_text(
        f"host: 192.0.2.1\nidentity_file: {key}\ndatasets:\n  tank/one: pass\n  tank/two: pass",
    )

    with (
        patch("socket.getaddrinfo", return_value=[object()]),
        patch("socket.create_connection") as create_connection,
        patch("zfs_unlock.diagnostics.ZfsUnlockClient") as client_cls,
    ):
        create_connection.return_value.__enter__.return_value = object()
        client_cls.return_value.run_remote = AsyncMock(
            side_effect=[
                MagicMock(returncode=0, stdout="locked\n", stderr=""),
                MagicMock(returncode=0, stdout="unlocked\n", stderr=""),
            ],
        )
        result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 0
    assert client_cls.return_value.run_remote.call_args_list == [
        ((["status", "tank/one"],),),
        ((["status", "tank/two"],),),
    ]
    assert "checking receiver status: tank/one" in result.stdout
    assert "checking receiver status: tank/two" in result.stdout


def test_doctor_fails_unknown_receiver_status(tmp_path: Path) -> None:
    """Doctor fails when a receiver reports an unclassified dataset status."""
    config_file = tmp_path / "config.yaml"
    key = write_private_file(tmp_path / "zfs-unlock-receiver")
    config_file.write_text(f"host: 192.0.2.1\nidentity_file: {key}\ndatasets:\n  tank/plain: pass")

    with (
        patch("socket.getaddrinfo", return_value=[object()]),
        patch("socket.create_connection") as create_connection,
        patch("zfs_unlock.diagnostics.ZfsUnlockClient") as client_cls,
    ):
        create_connection.return_value.__enter__.return_value = object()
        client_cls.return_value.run_remote = AsyncMock(
            return_value=MagicMock(returncode=0, stdout="unknown\n", stderr=""),
        )
        result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "receiver status unexpected: tank/plain -> unknown" in result.stdout


def test_doctor_fails_world_readable_identity_file(tmp_path: Path) -> None:
    """Doctor rejects SSH identity files readable by group or others."""
    config_file = tmp_path / "config.yaml"
    key = tmp_path / "zfs-unlock-receiver"
    key.write_text("secret")
    key.chmod(0o644)
    config_file.write_text(f"host: 192.0.2.1\nidentity_file: {key}\ndatasets:\n  tank/ds: pass")

    result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "identity file permissions too open" in result.stdout
    assert str(key) in result.stdout


def test_doctor_fails_world_readable_file_secret(tmp_path: Path) -> None:
    """Doctor rejects file-backed dataset secrets readable by group or others."""
    config_file = tmp_path / "config.yaml"
    key = write_private_file(tmp_path / "zfs-unlock-receiver")
    secret = tmp_path / "tank-ds.key"
    secret.write_text("passphrase")
    secret.chmod(0o644)
    config_file.write_text(
        f"host: 192.0.2.1\nidentity_file: {key}\nsecrets: files\ndatasets:\n  tank/ds: {secret}",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "secret file permissions too open for tank/ds" in result.stdout
    assert str(secret) in result.stdout


def test_keygen_creates_unlock_key(tmp_path: Path) -> None:
    """Keygen creates an ed25519 key and prints the public key."""
    key_path = tmp_path / "zfs-unlock-receiver"

    def fake_run(cmd: list[str], *, check: bool = True) -> MagicMock:
        assert check is True
        assert "-t" in cmd
        assert "ed25519" in cmd
        assert "-f" in cmd
        assert cmd[cmd.index("-f") + 1] == str(key_path)
        key_path.write_text("private")
        Path(f"{key_path}.pub").write_text("ssh-ed25519 AAAATEST zfs-unlock\n")
        return MagicMock(stdout="", stderr="", returncode=0)

    with (
        patch("shutil.which", return_value="/usr/bin/ssh-keygen"),
        patch("zfs_unlock.keygen.run_process", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["keygen", "--identity-file", str(key_path), "--comment", "zfs-unlock"])

    assert result.exit_code == 0
    assert "ssh-ed25519 AAAATEST zfs-unlock" in result.stdout
    assert key_path.exists()
    assert Path(f"{key_path}.pub").exists()


def test_service_install_linux_pins_current_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service install writes a unit that runs the installed zfs-unlock binary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    which = {"zfs-unlock": "/usr/local/bin/zfs-unlock"}
    with (
        patch("zfs_unlock.service.platform.system", return_value="Linux"),
        patch("zfs_unlock.service.shutil.which", side_effect=which.get),
        patch("zfs_unlock.service.run_process") as mock_run,
    ):
        result = runner.invoke(app, ["service", "install"])

    assert result.exit_code == 0
    unit = (tmp_path / ".config" / "systemd" / "user" / "zfs-unlock.service").read_text()
    assert "ExecStart=/usr/local/bin/zfs-unlock unlock --daemon\n" in unit
    assert "tool run" not in unit
    assert mock_run.call_count == 2  # noqa: PLR2004


def test_service_install_macos_pins_current_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service install writes a launchd plist that runs the installed binary."""
    monkeypatch.setenv("HOME", str(tmp_path))
    which = {"zfs-unlock": "/usr/local/bin/zfs-unlock"}
    with (
        patch("zfs_unlock.service.platform.system", return_value="Darwin"),
        patch("zfs_unlock.service.shutil.which", side_effect=which.get),
        patch("zfs_unlock.service.run_process"),
    ):
        result = runner.invoke(app, ["service", "install"])

    assert result.exit_code == 0
    plist = (tmp_path / "Library" / "LaunchAgents" / "com.zfs_unlock.plist").read_text()
    assert "    <string>/usr/local/bin/zfs-unlock</string>\n    <string>unlock</string>" in plist
    assert "tool" not in plist


def test_service_install_falls_back_to_uv_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without zfs-unlock on PATH the service falls back to uv tool run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    which = {"uv": "/opt/uv/bin/uv"}
    with (
        patch("zfs_unlock.service.platform.system", return_value="Linux"),
        patch("zfs_unlock.service.shutil.which", side_effect=which.get),
        patch("zfs_unlock.service.run_process"),
    ):
        result = runner.invoke(app, ["service", "install"])

    assert result.exit_code == 0
    assert "zfs-unlock not found on PATH" in ANSI_RE.sub("", result.output)
    unit = (tmp_path / ".config" / "systemd" / "user" / "zfs-unlock.service").read_text()
    assert "ExecStart=/opt/uv/bin/uv tool run zfs-unlock unlock --daemon\n" in unit


def test_service_install_fails_without_executable_or_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Service install fails cleanly when nothing can run the daemon."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with (
        patch("zfs_unlock.service.platform.system", return_value="Linux"),
        patch("zfs_unlock.service.shutil.which", return_value=None),
    ):
        result = runner.invoke(app, ["service", "install"])

    assert result.exit_code == 1
    assert "neither zfs-unlock nor uv found" in ANSI_RE.sub("", result.output)


def test_service_status_linux() -> None:
    """Service status checks systemd user unit on Linux."""
    with patch("platform.system", return_value="Linux"), patch("zfs_unlock.service.run_process") as mock_run:
        mock_run.return_value.stdout = "active\n"
        result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0
    assert "Service is running" in result.stdout
    mock_run.assert_called_once_with(["systemctl", "--user", "is-active", "zfs-unlock"], check=False)
