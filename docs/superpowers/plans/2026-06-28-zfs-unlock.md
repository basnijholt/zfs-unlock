# ZFS Unlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python package that unlocks NixOS/OpenZFS encrypted datasets from an off-box client through a restricted SSH receiver.

**Architecture:** Preserve the `truenas-unlock` CLI and config shape, but replace the TrueNAS API client with an SSH transport and add a NAS-side receiver mode. The receiver owns the privilege boundary by validating commands and allowlisting datasets before invoking ZFS.

**Tech Stack:** Python 3.11+, Typer, Pydantic, PyYAML, Rich, pytest, ruff, mypy, OpenSSH, OpenZFS.

---

### Task 1: Project Scaffold And Config Tests

**Files:**
- Create: `pyproject.toml`
- Create: `zfs_unlock.py`
- Create: `tests/test_config.py`

- [ ] Write failing tests for `resolve_secret`, `Dataset`, and `Config.from_yaml`.
- [ ] Run `uv run pytest tests/test_config.py -v` and confirm import/config failures.
- [ ] Implement the minimal config models and secret resolution.
- [ ] Run `uv run pytest tests/test_config.py -v` and confirm passing.

### Task 2: SSH Client

**Files:**
- Modify: `zfs_unlock.py`
- Create: `tests/test_client.py`
- Create: `tests/test_integration.py`

- [ ] Write failing tests for SSH command construction, status parsing, unlock stdin handling, lock command handling, and run loop behavior.
- [ ] Run targeted tests and confirm failures.
- [ ] Implement an injectable async SSH runner and `ZfsUnlockClient`.
- [ ] Run targeted tests and confirm passing.

### Task 3: Restricted Receiver

**Files:**
- Modify: `zfs_unlock.py`
- Create: `tests/test_receiver.py`

- [ ] Write failing tests for receiver command parsing, dataset name validation, allowlist rejection, status mapping, unlock, and lock.
- [ ] Run targeted tests and confirm failures.
- [ ] Implement receiver helpers and CLI command.
- [ ] Run targeted tests and confirm passing.

### Task 4: CLI, Service Helpers, And Docs

**Files:**
- Modify: `zfs_unlock.py`
- Create: `tests/test_cli.py`
- Create: `README.md`
- Create: `config.example.yaml`
- Create: `LICENSE`
- Create: `.github/workflows/pytest.yml`

- [ ] Write failing CLI tests for help, missing config, `status`, `lock`, daemon polling, and service status.
- [ ] Run targeted tests and confirm failures.
- [ ] Implement Typer commands and service helpers.
- [ ] Write README and example config with restricted SSH/NixOS setup.
- [ ] Run the full verification set: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy zfs_unlock.py`.
