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


class DuplicateKeyError(yaml.YAMLError):
    """A YAML mapping in the config repeats a key.

    PyYAML's default is silent last-wins, which would drop the first
    passphrase of a dataset listed twice without any warning. The message
    only ever names mapping keys (config field names, dataset paths) — never
    values, which may be inline passphrases.
    """


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:  # noqa: FBT001
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                msg = f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
                raise DuplicateKeyError(msg)
            seen.add(key)
        return super().construct_mapping(node, deep)


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

    Strip exactly one trailing line terminator (the one an editor or
    `echo` appends), never more: a passphrase that intentionally ends in a
    newline must survive the round trip. newline="" disables universal-newline
    translation, which would otherwise silently rewrite any CR or CRLF
    inside the passphrase itself.
    """
    with path.open(encoding="utf-8", newline="") as fh:
        text = fh.read()
    for suffix in ("\r\n", "\n", "\r"):
        if text.endswith(suffix):
            return text.removesuffix(suffix)
    return text


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

    @field_validator("host", "user")
    @classmethod
    def _validate_ssh_word(cls, value: str) -> str:
        """Reject values ssh would parse as options instead of a destination.

        `host: "-oProxyCommand=..."` would otherwise become an ssh option on
        the client command line — local command execution from a config typo.
        """
        if not value or value.startswith("-"):
            msg = "must not be empty or start with '-'"
            raise ValueError(msg)
        return value

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
        raw_data = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
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
    except DuplicateKeyError as exc:
        # Safe to print: the message names mapping keys, never values.
        err_console.print(f"[red]Invalid config: {exc}[/red]")
        raise typer.Exit(1) from exc
    except yaml.YAMLError as exc:
        # Never print the YAML error itself: PyYAML embeds the offending source
        # line verbatim, which may contain an inline passphrase.
        mark = exc.problem_mark if isinstance(exc, yaml.MarkedYAMLError) else None
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        err_console.print(f"[red]Invalid config: YAML syntax error{location}.[/red]")
        raise typer.Exit(1) from exc
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        err_console.print(f"[red]Invalid config: {exc}[/red]")
        raise typer.Exit(1) from exc

    return config_path, config
