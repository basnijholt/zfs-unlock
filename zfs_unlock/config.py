"""Configuration loading and validation."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, cast

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .constants import CONFIG_SEARCH_PATHS, DATASET_NAME_RE, EXAMPLE_CONFIG
from .output import err_console


class SecretsMode(StrEnum):
    """How to interpret secret values."""

    AUTO = "auto"
    FILES = "files"
    INLINE = "inline"


def _read_secret_file(path: Path) -> str:
    """Read a text secret file while preserving intentional spaces."""
    return path.read_text().rstrip("\r\n")


def resolve_secret(value: str, mode: SecretsMode) -> str:
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

    path: str
    secret: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Validate dataset paths before sending them to the receiver."""
        if not is_safe_dataset_name(value):
            msg = f"unsafe dataset name: {value}"
            raise ValueError(msg)
        return value

    @property
    def pool(self) -> str:  # noqa: D102
        return self.path.split("/")[0]

    @property
    def name(self) -> str:  # noqa: D102
        return "/".join(self.path.split("/")[1:])

    def get_passphrase(self, mode: SecretsMode) -> str:  # noqa: D102
        return resolve_secret(self.secret, mode)


class Config(BaseModel):
    """Application configuration for the off-box unlock client."""

    model_config = ConfigDict(extra="forbid")

    host: str
    user: str = "zfs-unlock"
    port: Annotated[int, Field(ge=1, le=65535)] = 22
    identity_file: Path | None = None
    connect_timeout: Annotated[int, Field(gt=0)] = 5
    command_timeout: Annotated[float, Field(gt=0)] = 30
    secrets: SecretsMode = SecretsMode.AUTO
    datasets: list[Dataset]

    @classmethod
    def from_yaml(cls, path: Path) -> Config:  # noqa: D102
        raw_data = yaml.safe_load(path.read_text())
        if raw_data is None:
            raw_data = {}
        if not isinstance(raw_data, dict):
            msg = "config file must contain a YAML mapping"
            raise TypeError(msg)
        if not all(isinstance(key, str) for key in raw_data):
            msg = "config keys must be strings"
            raise TypeError(msg)

        data = cast("dict[str, Any]", dict(raw_data))
        datasets_raw = data.pop("datasets", {})
        if not isinstance(datasets_raw, dict):
            msg = "config field 'datasets' must be a mapping"
            raise TypeError(msg)
        if not all(isinstance(ds_path, str) and isinstance(secret, str) for ds_path, secret in datasets_raw.items()):
            msg = "config field 'datasets' must map dataset names to string secrets"
            raise TypeError(msg)

        datasets = [Dataset(path=ds_path, secret=secret) for ds_path, secret in datasets_raw.items()]
        return cls(datasets=datasets, **data)


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
