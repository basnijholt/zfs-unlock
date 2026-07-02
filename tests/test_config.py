"""Tests for configuration parsing."""

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from zfs_unlock.config import Config, Dataset, SecretsMode, _resolve_secret
from zfs_unlock.constants import DEFAULT_IDENTITY_FILE, EXAMPLE_CONFIG

DEFAULT_PORT = 22
DEFAULT_CONNECT_TIMEOUT = 5
CUSTOM_PORT = 2222
CUSTOM_CONNECT_TIMEOUT = 9


def test_public_defaults_use_receiver_terminology() -> None:
    """Public defaults avoid implying the receiver must be a storage appliance."""
    old_host_name = "".join(chr(codepoint) for codepoint in (110, 97, 115))
    old_host = f"{old_host_name}.local"
    old_identity_file = f"zfs-unlock-{old_host_name}"
    public_config_text = EXAMPLE_CONFIG + Path("config.example.yaml").read_text()

    assert Path("~/.ssh/zfs-unlock-receiver") == DEFAULT_IDENTITY_FILE
    assert "host: zfs-host.example.lan" in public_config_text
    assert "zfs-unlock-receiver" in public_config_text
    assert old_host not in public_config_text
    assert old_identity_file not in public_config_text


class TestResolveSecret:
    """Tests for secret resolution."""

    def test_inline_mode_returns_literal(self, tmp_path: Path) -> None:
        """Inline mode always returns the literal value."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content")

        assert _resolve_secret(str(secret_file), SecretsMode.INLINE) == str(secret_file)
        assert _resolve_secret("literal-value", SecretsMode.INLINE) == "literal-value"

    def test_files_mode_reads_file(self, tmp_path: Path) -> None:
        """Files mode always reads from file."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content\n")

        assert _resolve_secret(str(secret_file), SecretsMode.FILES) == "file-content"

    def test_files_mode_preserves_spaces_in_secret(self, tmp_path: Path) -> None:
        """File-backed secrets trim line endings but preserve passphrase spaces."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("  file-content  \n")

        assert _resolve_secret(str(secret_file), SecretsMode.FILES) == "  file-content  "

    def test_files_mode_raises_on_missing(self) -> None:
        """Files mode raises error if file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            _resolve_secret("/nonexistent/path", SecretsMode.FILES)

    def test_auto_mode_reads_existing_file(self, tmp_path: Path) -> None:
        """Auto mode reads from file if it exists."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-content\n")

        assert _resolve_secret(str(secret_file), SecretsMode.AUTO) == "file-content"

    def test_auto_mode_returns_literal_if_no_file(self) -> None:
        """Auto mode returns literal if file doesn't exist."""
        assert _resolve_secret("my-passphrase", SecretsMode.AUTO) == "my-passphrase"


class TestDataset:
    """Tests for Dataset model."""

    def test_rejects_unsafe_path(self) -> None:
        """Dataset paths use the same conservative names accepted by the receiver."""
        with pytest.raises(ValidationError):
            Dataset(path="tank/photos;reboot", secret="passphrase")

    def test_get_passphrase_from_file(self, tmp_path: Path) -> None:
        """Dataset resolves passphrase using configured secret mode."""
        key_file = tmp_path / "key"
        key_file.write_text("file-passphrase\n")

        ds = Dataset(path="tank/photos", secret=str(key_file))

        assert ds.get_passphrase(SecretsMode.FILES) == "file-passphrase"

    def test_dataset_repr_masks_secret(self) -> None:
        """Dataset objects never expose the raw secret in repr/str."""
        dataset = Dataset(path="tank/photos", secret="super-secret")

        assert "super-secret" not in repr(dataset)
        assert "super-secret" not in str(dataset)
        assert dataset.get_passphrase(SecretsMode.INLINE) == "super-secret"


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

    def test_from_yaml_rejects_non_mapping_root(self, tmp_path: Path) -> None:
        """Config files must be YAML mappings."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- host: zfs-host.example.lan\n")

        with pytest.raises(TypeError, match="YAML mapping"):
            Config.from_yaml(config_file)

    def test_from_yaml_rejects_non_mapping_datasets(self, tmp_path: Path) -> None:
        """Dataset config must be a mapping from dataset to secret."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("host: zfs-host.example.lan\ndatasets:\n  - tank/photos\n")

        with pytest.raises(ValidationError, match="datasets"):
            Config.from_yaml(config_file)

    def test_from_yaml_does_not_echo_secrets_in_validation_errors(self, tmp_path: Path) -> None:
        """Validation errors must never leak inline secret values."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            dedent("""\
            host: zfs-host.example.lan
            datasets:
              tank/photos: [hidden-passphrase-value]
            """),
        )

        with pytest.raises(ValidationError) as exc_info:
            Config.from_yaml(config_file)

        assert "hidden-passphrase-value" not in str(exc_info.value)

    def test_from_yaml_rejects_unknown_top_level_keys(self, tmp_path: Path) -> None:
        """Unknown config keys are rejected instead of silently ignored."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            dedent("""\
            host: zfs-host.example.lan
            identty_file: ~/.ssh/typo
            datasets:
              tank/photos: passphrase
            """),
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.from_yaml(config_file)

    def test_from_yaml_rejects_invalid_timeouts(self, tmp_path: Path) -> None:
        """Timeouts must be positive."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            dedent("""\
            host: zfs-host.example.lan
            connect_timeout: 0
            command_timeout: -1
            datasets:
              tank/photos: passphrase
            """),
        )

        with pytest.raises(ValidationError):
            Config.from_yaml(config_file)
