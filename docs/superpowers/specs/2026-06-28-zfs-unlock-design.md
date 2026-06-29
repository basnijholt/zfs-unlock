# ZFS Unlock Design

## Goal

Build `zfs-unlock`, a NixOS/OpenZFS replacement for `truenas-unlock` that keeps the old off-box secret workflow while avoiding a general-purpose root SSH key.

## Context

`../truenas-unlock` unlocks TrueNAS encrypted datasets through the TrueNAS API. The NixOS NAS config in `~/dotfiles/configs/nixos/hosts/nas/storage.nix` currently imports `tank` and `ssd`, disables boot-time encryption prompts, and provides only an interactive `zfs-unlock-encrypted-datasets` helper. The latest `~/Work/nijho.lt` post says the off-box unlock path must survive the TrueNAS to NixOS cutover.

## Architecture

The package stays close to `truenas-unlock`: a single Typer CLI, YAML config, `--daemon` smart polling, `status`, `lock`, `service`, and a README/config example with the same shape.

The TrueNAS API client is replaced by an SSH client. The off-box device runs `zfs-unlock` and stores passphrases. For each configured dataset it connects to the NAS and requests `status`, `unlock`, or `lock`. Unlock sends the passphrase over SSH stdin only when the NAS reports the dataset key is unavailable.

The NAS runs a restricted receiver command. The receiver parses only a small command language, validates dataset names, checks an allowlist file, and then runs local ZFS commands. It is intended to be exposed through an SSH key restricted with `restrict`, `from=...`, and `command="sudo -n <receiver-wrapper>"`. The SSH user should have sudo permission only for that immutable receiver wrapper, not for a shell or arbitrary root commands.

## Components

- `zfs_unlock.py`: CLI, config parsing, local secret resolution, SSH client, receiver command, service install helpers.
- `tests/`: unit tests for config parsing, SSH command construction, client behavior, receiver parsing/safety, CLI behavior, and integration-style mocked flows.
- `config.example.yaml`: off-box client config.
- `README.md`: setup, usage, restricted SSH/NixOS receiver instructions.

## Data Flow

1. Off-box `zfs-unlock --daemon` loads `~/.config/zfs-unlock/config.yaml`.
2. For each dataset, the client runs `ssh ... status <dataset>`.
3. If status is `locked`, the client resolves the local secret and runs `ssh ... unlock <dataset>` with the passphrase on stdin.
4. The receiver validates the requested dataset against its allowlist.
5. The receiver runs `zfs load-key -L prompt <dataset>` and then best-effort `zfs mount -a`.

## Error Handling

Connection or command failures return `False` from one daemon tick, which keeps the old smart polling behavior: normal interval when reachable, one-second panic interval when unreachable or unstable. Receiver failures are explicit and nonzero. Secret resolution follows the existing `auto`, `files`, and `inline` modes.

## Testing

Tests should not require real SSH or ZFS. The SSH runner and local subprocess runner are injectable and mocked. Receiver tests assert allowlist enforcement and exact ZFS command arguments.
