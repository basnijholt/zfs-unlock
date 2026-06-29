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
uv run mypy zfs_unlock.py
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
zfs_unlock.py            # CLI, client, receiver, config, doctor, and service commands
nix/nixos-module.nix     # NixOS receiver module
nix/client-module.nix    # NixOS client daemon module
tests/                   # Python, CLI, integration, packaging, and NixOS module tests
docs/                    # Zensical documentation site
```

## Release Checklist

1. Update `VERSION`.
2. Open and merge a PR.
3. Publish a GitHub release tagged `vX.Y.Z`.
4. Confirm the `Upload Python Package` workflow publishes to PyPI.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
