---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/basnijholt/zfs-unlock
cd zfs-unlock
```

Install development dependencies:

```bash
uv sync --dev
```

## Running Tests

```bash
uv run pytest
```

## Code Quality

Run linting:

```bash
uv run ruff check .
uv run ruff format --check .
```

Run type checking:

```bash
uv run mypy zfs_unlock
```

Run Nix checks:

```bash
nix flake check --all-systems
```

## Documentation

Install docs dependencies and build the site:

```bash
uv sync --group docs
uv run zensical build
```

The generated site is written to `site/`.
GitHub Pages deploys that directory from the `Documentation` workflow on `main`.

## Project Structure

```text
zfs_unlock/              # Python package
zfs_unlock/cli.py        # Typer command registration and CLI entrypoints
zfs_unlock/client.py     # SSH client operations and unlock/lock/status workflows
zfs_unlock/receiver.py   # Restricted receiver and OpenZFS command allowlist enforcement
zfs_unlock/config.py     # YAML config parsing, dataset validation, and secret resolution
zfs_unlock/diagnostics.py  # doctor checks
nix/nixos-module.nix     # NixOS receiver module
nix/client-module.nix    # NixOS client daemon module
tests/                   # Python, CLI, integration, packaging, and NixOS module tests
docs/                    # Zensical documentation site
```

## Release Checklist

1. Update `VERSION`.
2. Open and merge a PR.
3. Publish a GitHub release tagged `vX.Y.Z`, where `X.Y.Z` exactly matches `VERSION`.
4. Confirm the `Upload Python Package` workflow publishes to PyPI.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
