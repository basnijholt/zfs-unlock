"""Tests for configuration parsing."""

from pathlib import Path
from textwrap import dedent

import pytest

from zfs_unlock import DEFAULT_IDENTITY_FILE, EXAMPLE_CONFIG, Config, Dataset, SecretsMode, resolve_secret

DEFAULT_PORT = 22
DEFAULT_CONNECT_TIMEOUT = 5
CUSTOM_PORT = 2222
CUSTOM_CONNECT_TIMEOUT = 9


def test_public_defaults_use_receiver_terminology() -> None:
    """Public defaults avoid implying the receiver must be a storage appliance."""
    old_host_name = "".join(chr(codepoint) for codepoint in (110, 97, 115))
    old_host = f"{old_host_name}.local"
    old_identity_file = f"zfs-unlock-{old_host_name}"

    assert Path("~/.ssh/zfs-unlock-receiver") == DEFAULT_IDENTITY_FILE
    assert "host: zfs-host.example.lan" in EXAMPLE_CONFIG
    assert "zfs-unlock-receiver" in EXAMPLE_CONFIG
    assert old_host not in EXAMPLE_CONFIG
    assert old_identity_file not in EXAMPLE_CONFIG


class TestResolveSecret:
    """Tests for resolve_secret function."""

    def test_inline_mode_returns_literal(self, tmp_path: Path) -> None:
        """Inline mode always returns the literal value."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content")

        assert resolve_secret(str(secret_file), SecretsMode.INLINE) == str(secret_file)
        assert resolve_secret("literal-value", SecretsMode.INLINE) == "literal-value"

    def test_files_mode_reads_file(self, tmp_path: Path) -> None:
        """Files mode always reads from file."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content\n")

        assert resolve_secret(str(secret_file), SecretsMode.FILES) == "file-content"

    def test_files_mode_raises_on_missing(self) -> None:
        """Files mode raises error if file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            resolve_secret("/nonexistent/path", SecretsMode.FILES)

    def test_auto_mode_reads_existing_file(self, tmp_path: Path) -> None:
        """Auto mode reads from file if it exists."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content\n")

        assert resolve_secret(str(secret_file), SecretsMode.AUTO) == "file-content"

    def test_auto_mode_returns_literal_if_no_file(self) -> None:
        """Auto mode returns literal if file doesn't exist."""
        assert resolve_secret("my-passphrase", SecretsMode.AUTO) == "my-passphrase"


class TestDataset:
    """Tests for Dataset model."""

    def test_path_parsing(self) -> None:
        """Dataset exposes pool and child name."""
        ds = Dataset(path="tank/photos", secret="passphrase")

        assert ds.pool == "tank"
        assert ds.name == "photos"
        assert ds.path == "tank/photos"

    def test_nested_path(self) -> None:
        """Dataset child name preserves nested path."""
        ds = Dataset(path="tank/data/photos", secret="passphrase")

        assert ds.pool == "tank"
        assert ds.name == "data/photos"

    def test_get_passphrase_from_file(self, tmp_path: Path) -> None:
        """Dataset resolves passphrase using configured secret mode."""
        key_file = tmp_path / "key"
        key_file.write_text("file-passphrase\n")

        ds = Dataset(path="tank/photos", secret=str(key_file))

        assert ds.get_passphrase(SecretsMode.FILES) == "file-passphrase"


class TestConfig:
    """Tests for Config model."""

    def test_from_yaml_with_defaults(self, tmp_path: Path) -> None:
        """Config loads the off-box SSH defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            dedent("""\
            host: zfs-host.example.lan
            datasets:
              tank/photos: my-passphrase
            """),
        )

        config = Config.from_yaml(config_file)

        assert config.host == "zfs-host.example.lan"
        assert config.user == "zfs-unlock"
        assert config.port == DEFAULT_PORT
        assert config.identity_file is None
        assert config.connect_timeout == DEFAULT_CONNECT_TIMEOUT
        assert config.secrets == SecretsMode.AUTO
        assert len(config.datasets) == 1
        assert config.datasets[0].get_passphrase(config.secrets) == "my-passphrase"

    def test_from_yaml_with_identity_file_and_file_secrets(self, tmp_path: Path) -> None:
        """Config supports SSH identity files and file-backed secrets."""
        config_file = tmp_path / "config.yaml"
        ds_key_file = tmp_path / "ds-key"
        identity_file = tmp_path / "ssh-key"
        ds_key_file.write_text("test-passphrase\n")
        identity_file.write_text("not-a-real-key")

        config_file.write_text(
            dedent(f"""\
            host: zfs-host.example.lan
            user: unlocker
            port: {CUSTOM_PORT}
            identity_file: {identity_file}
            connect_timeout: {CUSTOM_CONNECT_TIMEOUT}
            secrets: files
            datasets:
              tank/photos: {ds_key_file}
            """),
        )

        config = Config.from_yaml(config_file)

        assert config.user == "unlocker"
        assert config.port == CUSTOM_PORT
        assert config.identity_file == identity_file
        assert config.connect_timeout == CUSTOM_CONNECT_TIMEOUT
        assert config.datasets[0].get_passphrase(config.secrets) == "test-passphrase"
