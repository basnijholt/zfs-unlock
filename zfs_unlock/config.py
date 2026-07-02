"""Configuration loading and validation."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from .constants import CONFIG_SEARCH_PATHS, DATASET_NAME_RE, EXAMPLE_CONFIG
from .output import err_console


class SecretsMode(StrEnum):
    """How to interpret secret values."""

    AUTO = "auto"
    FILES = "files"
    INLINE = "inline"


def _read_secret_file(path: Path) -> str:
    """Read a text secret file while preserving intentional spaces.

    Always UTF-8: the passphrase is encoded as UTF-8 on the wire and decoded as
    UTF-8 by the receiver, so a locale-dependent read (e.g. LANG=C) would
    corrupt or reject a correct secret.
    """
    return path.read_text(encoding="utf-8").rstrip("\r\n")


def _resolve_secret(value: str, mode: SecretsMode) -> str:
    """Resolve a secret value based on the configured mode."""
    if mode == SecretsMode.INLINE:
        return value

    path = Path(value).expanduser()

    if mode == SecretsMode.FILES:
        return _read_secret_file(path)

    if path.exists() and path.is_file():
        return _read_secret_file(path)
    return value


def is_safe_dataset_name(name: str) -> bool:
    """Return whether a string is a conservative ZFS dataset name."""
    if not DATASET_NAME_RE.fullmatch(name):
        return False
    return all(segment not in {".", ".."} and not segment.startswith("-") for segment in name.split("/"))


class Dataset(BaseModel):
    """A ZFS dataset to unlock."""

    # Never echo raw inputs in validation errors: `secret` may be a passphrase.
    model_config = ConfigDict(hide_input_in_errors=True)

    path: str
    secret: SecretStr

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Validate dataset paths before sending them to the receiver."""
        if not is_safe_dataset_name(value):
            msg = f"unsafe dataset name: {value}"
            raise ValueError(msg)
        return value

    def get_passphrase(self, mode: SecretsMode) -> str:  # noqa: D102
        return _resolve_secret(self.secret.get_secret_value(), mode)


class Config(BaseModel):
    """Application configuration for the off-box unlock client."""

    # hide_input_in_errors: the datasets mapping can hold inline passphrases,
    # and load_config prints ValidationError text to the terminal.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str
    user: str = "zfs-unlock"
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    identity_file: Path | None = None
    # Bounded above so YAML `.inf` (or an absurd value) cannot disable the
    # timeout and wedge the daemon poll loop on a hung receiver.
    connect_timeout: Annotated[int, Field(gt=0, le=3600)] = 5
    command_timeout: Annotated[float, Field(gt=0, le=3600)] = 30
    secrets: SecretsMode = SecretsMode.AUTO
    datasets: list[Dataset] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _datasets_mapping_to_list(cls, data: Any) -> Any:
        """Accept the config-file shape where datasets map paths to secrets."""
        if isinstance(data, dict) and isinstance(datasets := data.get("datasets"), dict):
            data = {**data, "datasets": [{"path": path, "secret": secret} for path, secret in datasets.items()]}
        return data

    @classmethod
    def from_yaml(cls, path: Path) -> Config:  # noqa: D102
        raw_data = yaml.safe_load(path.read_text())
        if raw_data is None:
            raw_data = {}
        if not isinstance(raw_data, dict):
            msg = "config file must contain a YAML mapping"
            raise TypeError(msg)
        return cls.model_validate(raw_data)


def find_config() -> Path | None:
    """Find config file in standard locations."""
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path
    return None


def load_config(config_path: Path | None) -> tuple[Path, Config]:
    """Load and validate a config file, exiting cleanly for CLI errors."""
    if config_path is None:
        config_path = find_config()

    if config_path is None or not config_path.exists():
        err_console.print("[red]Config not found.[/red]")
        err_console.print("\nCreate ~/.config/zfs-unlock/config.yaml:\n")
        err_console.print(EXAMPLE_CONFIG)
        raise typer.Exit(1)

    try:
        config = Config.from_yaml(config_path)
    except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Invalid config: {exc}[/red]")
        raise typer.Exit(1) from exc

    return config_path, config
