# ZFS Unlock

[![PyPI](https://img.shields.io/pypi/v/zfs-unlock)](https://pypi.org/project/zfs-unlock/)
[![Python](https://img.shields.io/pypi/pyversions/zfs-unlock)](https://pypi.org/project/zfs-unlock/)
[![Tests](https://github.com/basnijholt/zfs-unlock/actions/workflows/pytest.yml/badge.svg)](https://github.com/basnijholt/zfs-unlock/actions/workflows/pytest.yml)
[![License](https://img.shields.io/github/license/basnijholt/zfs-unlock)](LICENSE)

Unlock encrypted OpenZFS datasets over SSH without putting the passphrases on
the NAS.

This is the NixOS/OpenZFS version of
[`truenas-unlock`](https://github.com/basnijholt/truenas-unlock). The client
side is intentionally similar: same YAML config style, same daemon smart
polling, same `status`, `lock`, and `service` commands. The TrueNAS API call is
replaced by a restricted SSH receiver on the NAS.

## Why?

The goal is the same "poor-man's second factor" as `truenas-unlock`:

1. Keep ZFS dataset passphrases on a separate device.
2. Let the NAS boot without storing the unlock material locally.
3. Unlock datasets only when the separate unlock device is on the network.

A plain root SSH key would work, but it is too broad. `zfs-unlock` is designed
for a narrower setup:

- SSH key restricted with `restrict`, `from=...`, and `command=...`
- dedicated `zfs-unlock` SSH user
- sudo permission only for a root-owned receiver wrapper
- NAS-side dataset allowlist
- receiver command parser that only accepts `status`, `unlock`, and `lock`

## Install

```bash
# With uv (recommended)
uv tool install zfs-unlock

# With pip
pip install zfs-unlock
```

Install it on the off-box unlock device. The NAS also needs access to the
`zfs-unlock receiver` command, usually through a NixOS package, `uv tool run`, or
another immutable wrapper.

## Client Setup

Create `~/.config/zfs-unlock/config.yaml` on the off-box unlock device:

```yaml
host: nas.local
user: zfs-unlock
identity_file: ~/.ssh/zfs-unlock-nas

# secrets: auto  # auto (default) | files | inline

datasets:
  tank/syncthing: ~/.secrets/syncthing-key
  tank/photos: my-literal-passphrase
```

The `secrets` mode controls how values are interpreted:

- `auto` (default): if a value is an existing file, read it; otherwise use it as
  a literal passphrase
- `files`: always treat values as file paths
- `inline`: always treat values as literal passphrases

## NAS Setup

The receiver must run with enough privilege to call `zfs get`, `zfs load-key`,
`zfs unload-key`, `zfs unmount`, and `zfs mount`. Do not give the unlock SSH key
a general root shell. Use a forced command and a constrained sudo rule.

Example NixOS shape:

```nix
{ pkgs, ... }:

let
  zfsUnlock = pkgs.writeShellScriptBin "zfs-unlock" ''
    exec ${pkgs.uv}/bin/uv tool run zfs-unlock "$@"
  '';

  receiver = pkgs.writeShellScript "zfs-unlock-receiver" ''
    exec ${zfsUnlock}/bin/zfs-unlock receiver \
      --allow-file /etc/zfs-unlock/allowed-datasets "$@"
  '';

  sshWrapper = pkgs.writeShellScript "zfs-unlock-ssh-wrapper" ''
    set -eu
    exec ${pkgs.sudo}/bin/sudo -n ${receiver} "$SSH_ORIGINAL_COMMAND"
  '';
in
{
  users.groups.zfs-unlock = {};

  users.users.zfs-unlock = {
    isSystemUser = true;
    group = "zfs-unlock";
    home = "/var/lib/zfs-unlock";
    createHome = true;
    openssh.authorizedKeys.keys = [
      ''restrict,from="192.168.1.50",command="${sshWrapper}" ssh-ed25519 AAAA... unlock-device''
    ];
  };

  security.sudo.extraRules = [
    {
      users = [ "zfs-unlock" ];
      commands = [
        {
          command = "${receiver}";
          options = [ "NOPASSWD" ];
        }
      ];
    }
  ];

  environment.etc."zfs-unlock/allowed-datasets".text = ''
    tank/syncthing
    tank/photos
  '';
}
```

The key point is that the SSH key can only execute the receiver wrapper. The
receiver still checks the requested dataset against
`/etc/zfs-unlock/allowed-datasets`.

The extra `sshWrapper` avoids relying on `sudo` preserving
`SSH_ORIGINAL_COMMAND`. It captures the SSH command before sudo and passes it to
the root receiver as one argument.

## Usage

```bash
# Run once
zfs-unlock

# Run as daemon
# Checks every 1s if the NAS is unreachable, otherwise every 30s
zfs-unlock --daemon

# Custom interval for the relaxed state
zfs-unlock --daemon --interval 60

# Show configured datasets without connecting
zfs-unlock --dry-run

# Check status
zfs-unlock status

# Lock datasets
zfs-unlock lock --force

# Limit any command to matching dataset paths
zfs-unlock --dataset photos
zfs-unlock status --dataset tank/photos
```

## Running As A Service

Requires `uv` to be installed. Auto-detects Linux systemd user services or macOS
launchd:

```bash
zfs-unlock service install
zfs-unlock service status
zfs-unlock service logs
zfs-unlock service uninstall
```

On Linux, enable linger if the service should run before the user logs in:

```bash
sudo loginctl enable-linger "$USER"
```

## Receiver Command

The client sends one of these SSH commands:

```bash
status tank/photos
unlock tank/photos
lock tank/photos --force
```

When `unlock` is requested, the passphrase is sent on SSH stdin. The receiver
runs:

```bash
zfs load-key -L prompt tank/photos
zfs mount -a
```

`zfs mount -a` is best effort; unlock success does not depend on every dataset
mounting cleanly.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy zfs_unlock.py
```

## License

MIT
