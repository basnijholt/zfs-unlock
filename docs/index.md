---
icon: lucide/key-round
---

# zfs-unlock

**Unlock encrypted OpenZFS datasets over a restricted SSH receiver.**

<div style="text-align: center; margin: 2rem 0;">
  <img src="assets/logo.svg" alt="zfs-unlock Logo" width="200" />
</div>

`zfs-unlock` keeps OpenZFS encryption passphrases on a separate trusted machine and sends them only when it can reach a restricted receiver on the ZFS host.

There are two roles:

- the **unlock device** stores passphrases and runs `zfs-unlock unlock`, either once or as a daemon
- the **ZFS host** runs a forced-command receiver that can only operate on explicitly allowed datasets

## Why zfs-unlock?

OpenZFS native encryption protects data at rest, but encrypted datasets still need their keys loaded after every reboot.
The easy automation path is to store key files on the storage host, but that weakens the model: if the host is stolen, the attacker has both the encrypted data and the keys.

`zfs-unlock` gives you a practical second factor for storage unlocks:

1. Run `zfs-unlock` on a separate device.
2. Store encryption passphrases only on that device.
3. Let datasets unlock automatically when both devices are on the network.
4. Keep data encrypted if the storage host is stolen without the unlock device.

Unlike a plain root SSH key, the receiver path is intentionally narrow:

- a dedicated `zfs-unlock` SSH user
- an SSH key restricted with `restrict`, `from=...`, and `command=...`
- sudo permission only for a root-owned receiver wrapper
- a receiver-side dataset allowlist
- a receiver parser that only accepts `status`, `unlock`, and `lock`

<div style="text-align: center; margin: 2rem 0;">
  <a href="https://pypi.org/project/zfs-unlock/" class="md-button md-button--primary">
    Install from PyPI
  </a>
  <a href="https://github.com/basnijholt/zfs-unlock" class="md-button">
    View on GitHub
  </a>
</div>

## Quick Start

```bash
# Install on the unlock device
uv tool install zfs-unlock

# Generate a dedicated SSH key
zfs-unlock keygen --identity-file ~/.ssh/zfs-unlock-receiver --comment pi4-zfs-unlock

# Check config, key, network, and receiver status
zfs-unlock doctor

# Unlock configured datasets
zfs-unlock unlock
```

[Get Started](getting-started.md){ .md-button .md-button--primary }
[View Usage](usage.md){ .md-button }

## NixOS Support

Nix is optional.
The Python CLI and restricted SSH receiver work without Nix, while the included NixOS modules provide declarative setup when you want it.

The receiver module creates the restricted SSH account, forced command, sudo rule, allowlist, and receiver wrapper.
The client module creates a daemon service that runs `zfs-unlock unlock --daemon` for a normal user.

## Background

This project came from my own migration path: I happily used [`truenas-unlock`](https://github.com/basnijholt/truenas-unlock) on TrueNAS, then built this generic OpenZFS version after [switching my storage host from TrueNAS to NixOS](https://www.nijho.lt/post/truenas-to-nixos/).
