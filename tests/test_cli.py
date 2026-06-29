"""Tests for CLI functionality."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from zfs_unlock import Dataset, app, filter_datasets, find_config

runner = CliRunner()
DAEMON_RUN_CALLS = 3


class TestFilterDatasets:
    """Tests for filter_datasets function."""

    def test_no_filter_returns_all(self) -> None:
        """No filter returns all datasets."""
        datasets = [
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/syncthing", secret="pass2"),
        ]

        assert filter_datasets(datasets, None) == datasets

    def test_single_filter_partial_match(self) -> None:
        """Single filter matches partial path."""
        datasets = [
            Dataset(path="tank/photos", secret="pass1"),
            Dataset(path="tank/syncthing", secret="pass2"),
            Dataset(path="tank/frigate", secret="pass3"),
        ]

        result = filter_datasets(datasets, ["photos"])

        assert len(result) == 1
        assert result[0].path == "tank/photos"


class TestFindConfig:
    """Tests for find_config function."""

    def test_finds_config_yaml_in_cwd(self, tmp_path: Path, monkeypatch: object) -> None:
        """Find config.yaml in current directory."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("host: test")

        with patch("zfs_unlock.CONFIG_SEARCH_PATHS", [Path("config.yaml"), Path("config.yml")]):
            assert find_config() == Path("config.yaml")

    def test_returns_none_when_no_config(self, tmp_path: Path, monkeypatch: object) -> None:
        """Return None when no config file exists."""
        monkeypatch.chdir(tmp_path)

        with patch("zfs_unlock.CONFIG_SEARCH_PATHS", [Path("config.yaml"), Path("config.yml")]):
            assert find_config() is None


def test_cli_help() -> None:
    """Test CLI help command."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Unlock OpenZFS datasets" in result.stdout


def test_cli_missing_config() -> None:
    """Test CLI fails without config."""
    with patch("zfs_unlock.find_config", return_value=None):
        result = runner.invoke(app)

    assert result.exit_code == 1
    assert "Config not found" in result.stderr


def test_cli_with_config(tmp_path: Path) -> None:
    """Test CLI runs with config file."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_unlock(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        return True

    with patch("zfs_unlock.run_unlock", new=fake_run_unlock):
        result = runner.invoke(app, ["--config", str(config_file)])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_cli_daemon_mode(tmp_path: Path) -> None:
    """Test CLI daemon smart polling loop."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")

    fake_run_unlock = MagicMock(return_value=object())
    with (
        patch("zfs_unlock.run_unlock", new=fake_run_unlock),
        patch("asyncio.run") as mock_run,
        patch("time.sleep") as mock_sleep,
    ):
        mock_run.side_effect = [True, False, KeyboardInterrupt]
        result = runner.invoke(app, ["--config", str(config_file), "--daemon", "--interval", "10"])

    assert result.exit_code == 0
    assert mock_run.call_count == DAEMON_RUN_CALLS
    mock_sleep.assert_any_call(10)
    mock_sleep.assert_any_call(1)


def test_cli_lock_command(tmp_path: Path) -> None:
    """Test CLI lock command."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_lock(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    with patch("zfs_unlock.run_lock", new=fake_run_lock):
        result = runner.invoke(app, ["lock", "--config", str(config_file), "--force"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1]["force"] is True


def test_cli_status_command(tmp_path: Path) -> None:
    """Test CLI status command."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: test\ndatasets:\n  tank/ds: pass")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_run_status(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    with patch("zfs_unlock.run_status", new=fake_run_status):
        result = runner.invoke(app, ["status", "--config", str(config_file), "--dataset", "tank/ds"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1]["dataset_filters"] == ["tank/ds"]


def test_cli_receiver_uses_ssh_original_command(tmp_path: Path) -> None:
    """Receiver command reads SSH_ORIGINAL_COMMAND when no args are provided."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")

    with (
        patch.dict("os.environ", {"SSH_ORIGINAL_COMMAND": "status tank/photos"}),
        patch("zfs_unlock.Receiver") as receiver_cls,
    ):
        receiver_cls.return_value.handle.return_value = response
        result = runner.invoke(app, ["receiver", "--allow-file", str(allow_file)])

    assert result.exit_code == 0
    assert result.stdout == "locked\n"
    receiver_cls.return_value.handle.assert_called_once_with(["status", "tank/photos"], stdin_text="")


def test_cli_receiver_parses_single_wrapped_command_argument(tmp_path: Path) -> None:
    """Receiver command parses one shell command argument from a sudo wrapper."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")

    with patch("zfs_unlock.Receiver") as receiver_cls:
        receiver_cls.return_value.handle.return_value = response
        result = runner.invoke(app, ["receiver", "--allow-file", str(allow_file), "status tank/photos"])

    assert result.exit_code == 0
    assert result.stdout == "locked\n"
    receiver_cls.return_value.handle.assert_called_once_with(["status", "tank/photos"], stdin_text="")


def test_cli_receiver_passes_zfs_path(tmp_path: Path) -> None:
    """Receiver command can use an explicit zfs executable path."""
    allow_file = tmp_path / "allowed"
    allow_file.write_text("tank/photos\n")
    response = MagicMock(returncode=0, stdout="locked\n", stderr="")

    with patch("zfs_unlock.Receiver") as receiver_cls:
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
    receiver_cls.return_value.handle.assert_called_once_with(["status", "tank/photos"], stdin_text="")


def test_service_status_linux() -> None:
    """Service status checks systemd user unit on Linux."""
    with patch("platform.system", return_value="Linux"), patch("zfs_unlock._run") as mock_run:
        mock_run.return_value.stdout = "active\n"
        result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0
    assert "Service is running" in result.stdout
    mock_run.assert_called_once_with(["systemctl", "--user", "is-active", "zfs-unlock"], check=False)
