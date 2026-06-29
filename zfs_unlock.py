"""OpenZFS dataset unlock over a restricted SSH receiver."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel


class SecretsMode(str, Enum):
    """How to interpret secret values."""

    AUTO = "auto"
    FILES = "files"
    INLINE = "inline"


def resolve_secret(value: str, mode: SecretsMode) -> str:
    """Resolve a secret value based on the configured mode."""
    if mode == SecretsMode.INLINE:
        return value

    path = Path(value).expanduser()

    if mode == SecretsMode.FILES:
        return path.read_text().strip()

    if path.exists() and path.is_file():
        return path.read_text().strip()
    return value


class Dataset(BaseModel):
    """A ZFS dataset to unlock."""

    path: str
    secret: str

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

    host: str
    user: str = "zfs-unlock"
    port: int = 22
    identity_file: Path | None = None
    connect_timeout: int = 5
    secrets: SecretsMode = SecretsMode.AUTO
    datasets: list[Dataset]

    @classmethod
    def from_yaml(cls, path: Path) -> Config:  # noqa: D102
        data = yaml.safe_load(path.read_text())

        datasets_raw = data.pop("datasets", {})
        datasets = [Dataset(path=ds_path, secret=secret) for ds_path, secret in datasets_raw.items()]

        return cls(datasets=datasets, **data)
