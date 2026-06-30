---
icon: lucide/terminal
---

# Usage

## Unlock

Run once:

```bash
zfs-unlock unlock
```

Run as a daemon:

```bash
zfs-unlock unlock --daemon
```

The daemon checks every 1 second while the receiver host is unreachable, then every 30 seconds after a reachable check.
Use `--interval` to change the relaxed interval:

```bash
zfs-unlock unlock --daemon --interval 60
```

Preview actions without sending passphrases:

```bash
zfs-unlock unlock --dry-run
```

## Status and Doctor

Show configured dataset status:

```bash
zfs-unlock status
```

Check config, key, network, permissions, and receiver status:

```bash
zfs-unlock doctor
```

## Lock

Lock a dataset after its services have stopped using it:

```bash
zfs-unlock lock -D tank/photos
```

Force-unmount mounted descendants before unloading the key:

```bash
zfs-unlock lock --force -D tank/photos
```

`zfs-unlock lock` can fail with `Key unload error: '<dataset>' is busy` when a service still has files open on that dataset.
Stop the service first, or use `--force` when you intentionally want to unmount the dataset and disrupt those processes.
Even with `--force`, OpenZFS can refuse to unmount a dataset that is still held by NFS, SMB, client mounts, or kernel users.
Unmount clients or stop exports first, then retry the lock.

## Config Reference

```yaml
host: zfs-host.example.lan
user: zfs-unlock
identity_file: ~/.ssh/zfs-unlock-receiver
command_timeout: 30
secrets: auto

datasets:
  tank/syncthing: ~/.secrets/syncthing-key
  tank/photos: my-literal-passphrase
```

| Field | Default | Description |
| --- | --- | --- |
| `host` | required | SSH hostname or IP address for the ZFS host. |
| `user` | `zfs-unlock` | SSH receiver user. |
| `identity_file` | unset | Dedicated SSH key for the receiver, commonly `~/.ssh/zfs-unlock-receiver`. |
| `command_timeout` | `30` | Per-command timeout in seconds. |
| `secrets` | `auto` | `auto`, `files`, or `inline`. |
| `datasets` | required | Mapping from dataset name to passphrase value or file path. |

## NixOS Receiver Options

```nix
services.zfsUnlock.receiver = {
  enable = true;
  allowedFrom = [ "192.168.1.50" ];
  authorizedKeys = [ "ssh-ed25519 AAAA... unlock-device" ];
  datasets = [ "tank/photos" ];
};
```

Common receiver options:

| Option | Default | Description |
| --- | --- | --- |
| `enable` | `false` | Enable the restricted receiver. |
| `package` | flake package | Package providing `zfs-unlock`. |
| `zfsPackage` | `config.boot.zfs.package` | Package providing `zfs`. |
| `user` | `zfs-unlock` | SSH receiver user. |
| `group` | `zfs-unlock` | Primary receiver user group. |
| `home` | `/var/lib/zfs-unlock` | Receiver user home directory. |
| `shell` | `pkgs.runtimeShell` | Login shell used by OpenSSH to run the forced command. |
| `enableLinger` | `true` | Keep the receiver user's systemd user manager stable across short-lived SSH sessions. |
| `allowedFrom` | `[]` | OpenSSH `from=` source patterns for the receiver key. |
| `authorizedKeys` | `[]` | Public keys allowed to invoke the forced command. |
| `datasets` | `[]` | OpenZFS datasets the receiver may inspect, unlock, or lock. |

## NixOS Client Options

```nix
services.zfsUnlock.client = {
  enable = true;
  user = "alice";
  group = "users";
};
```

Common client options:

| Option | Default | Description |
| --- | --- | --- |
| `enable` | `false` | Enable the unlock daemon. |
| `package` | flake package | Package providing `zfs-unlock`. |
| `user` | required | User that owns the config and key files. |
| `group` | user's group | Service group. |
| `interval` | `30` | Relaxed daemon interval in seconds. |

## Portable Service Commands

On NixOS, prefer the `services.zfsUnlock.client` module.
The portable CLI installer requires `uv` and auto-detects Linux systemd or macOS launchd:

```bash
zfs-unlock service install
zfs-unlock service status
zfs-unlock service logs
zfs-unlock service uninstall
```

## CLI Help

Bare `zfs-unlock` shows help and does not unlock anything.
Use the explicit `unlock` subcommand for state-changing unlock operations.

```bash
zfs-unlock --help
```
