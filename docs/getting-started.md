---
icon: lucide/rocket
---

# Getting Started

## Install

Install `zfs-unlock` on the unlock device.
If you configure the receiver manually, install it on the ZFS host too.

=== "uv tool (Recommended)"

    ```bash
    uv tool install zfs-unlock
    ```

=== "pip"

    ```bash
    pip install zfs-unlock
    ```

=== "From source"

    ```bash
    git clone https://github.com/basnijholt/zfs-unlock
    cd zfs-unlock
    uv sync --dev
    ```

## Generate a Receiver Key

Generate a dedicated SSH key on the unlock device:

```bash
zfs-unlock keygen --identity-file ~/.ssh/zfs-unlock-receiver --comment pi4-zfs-unlock
```

`keygen` refuses to replace an existing key; pass `--overwrite` to regenerate deliberately.

Add the printed public key to the receiver host's `authorizedKeys` list.

## Configure the Unlock Device

Create `~/.config/zfs-unlock/config.yaml` on the unlock device:

```yaml
host: zfs-host.example.lan
user: zfs-unlock
identity_file: ~/.ssh/zfs-unlock-receiver
# port: 22
# connect_timeout: 5
# command_timeout: 30

# secrets: auto  # auto (default) | files | inline

datasets:
  tank/syncthing: ~/.secrets/syncthing-key
  tank/photos: my-literal-passphrase
```

The `secrets` mode controls how dataset values are interpreted:

- **auto**: if the value is an existing file path, read from it; otherwise use it as a literal passphrase
- **files**: always treat values as file paths
- **inline**: always treat values as literal passphrases

## Configure a NixOS Receiver

Add the receiver module to the ZFS host flake:

```nix
{
  inputs.zfs-unlock.url = "github:basnijholt/zfs-unlock";

  outputs = { nixpkgs, zfs-unlock, ... }: {
    nixosConfigurations.storage = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        zfs-unlock.nixosModules.receiver
        ./hosts/storage/default.nix
      ];
    };
  };
}
```

Configure only the receiver policy on the ZFS host:

```nix
{
  services.zfs-unlock.receiver = {
    enable = true;
    allowedFrom = [ "192.168.1.50" ];
    authorizedKeys = [
      "ssh-ed25519 AAAA... unlock-device"
    ];
    datasets = [
      "tank/syncthing"
      "tank/photos"
    ];
  };
}
```

The module creates the `zfs-unlock` SSH user, forced command, sudo rule, receiver wrapper, login shell, and `/etc/zfs-unlock/allowed-datasets`.
The receiver still checks each requested dataset against that allowlist.
By default the module also enables systemd linger for the receiver user to avoid NixOS switch-time D-Bus races after short-lived forced-command SSH sessions.

## Configure a NixOS Client

On a NixOS unlock device, include the client module and enable the daemon:

```nix
{
  imports = [
    zfs-unlock.nixosModules.client
  ];

  services.zfs-unlock.client = {
    enable = true;
    user = "alice";
    group = "users";
  };
}
```

The client module creates a `zfs-unlock.service` system service, installs the CLI into the system profile, adds OpenSSH to the service `PATH`, and sets `HOME` and `XDG_CONFIG_HOME` so the normal user config is found.

## Configure a Receiver Manually (without Nix)

The NixOS module is only a convenience wrapper; the receiver runs on any Linux host with OpenZFS, sudo, and OpenSSH.
To reproduce what the module generates, on the ZFS host:

1. Install `zfs-unlock` (for example `uv tool install zfs-unlock`) and note the executable path (`command -v zfs-unlock`).

2. Create a dedicated system user:

    ```bash
    sudo useradd --system --create-home --home-dir /var/lib/zfs-unlock --shell /bin/sh zfs-unlock
    ```

3. Create the allowlist, one dataset per line (`#` comments allowed):

    ```bash
    sudo install -d /etc/zfs-unlock
    printf 'tank/syncthing\ntank/photos\n' | sudo tee /etc/zfs-unlock/allowed-datasets
    ```

4. Create a root-owned receiver wrapper, for example `/usr/local/sbin/zfs-unlock-receiver`:

    ```bash
    #!/bin/sh
    exec /path/to/zfs-unlock receiver \
      --allow-file /etc/zfs-unlock/allowed-datasets \
      --zfs-path /usr/sbin/zfs \
      "$@"
    ```

    Make it root-owned and not writable by others: `sudo chown root:root ... && sudo chmod 755 ...`.

5. Allow the receiver user to run exactly that wrapper as root (`sudo visudo -f /etc/sudoers.d/zfs-unlock`):

    ```text
    zfs-unlock ALL=(root:root) NOPASSWD: /usr/local/sbin/zfs-unlock-receiver
    ```

6. Create an SSH wrapper that forwards the forced command **as a single argument** —
   the receiver parses it itself, and passing it as one argument is what prevents
   option injection (for example `/usr/local/sbin/zfs-unlock-ssh-wrapper`):

    ```bash
    #!/bin/sh
    set -eu
    exec sudo -n /usr/local/sbin/zfs-unlock-receiver "${SSH_ORIGINAL_COMMAND-}"
    ```

7. Install the unlock device's public key for the `zfs-unlock` user, restricted and forced
   (`/var/lib/zfs-unlock/.ssh/authorized_keys`, mode `600`, owned by `zfs-unlock`):

    ```text
    restrict,from="192.168.1.50",command="/usr/local/sbin/zfs-unlock-ssh-wrapper" ssh-ed25519 AAAA... unlock-device
    ```

The receiver accepts only `status <dataset>`, `unlock <dataset>` (passphrase on stdin), and `lock <dataset> [--force]`, and only for datasets in the allowlist.

## Pin the Host Key

Passphrase secrecy in transit depends on SSH host-key verification, and the client refuses unknown or changed host keys.
Before the first unlock, add the receiver's host key to `known_hosts` on the unlock device and verify its fingerprint out of band (console access, or a fingerprint you recorded at install time):

```bash
ssh-keyscan -p 22 zfs-host.example.lan >> ~/.ssh/known_hosts
```

## Verify

After rebuilding the receiver host, run this on the unlock device:

```bash
zfs-unlock doctor
```

`doctor` checks the config, SSH identity, host reachability, receiver status, and permissions on the configured identity and file-backed dataset secrets.

## Troubleshooting

**`Host key verification failed`** — the receiver's host key is not in `known_hosts` (or changed).
Run the `ssh-keyscan` command above after verifying the fingerprint out of band.
The client pins `StrictHostKeyChecking=ask` and fails closed on purpose; never work around it by disabling host-key checking.

**`Permission denied (publickey)`** — the receiver host does not accept the configured identity.
Check that `identity_file` points at the key you generated, and that its public half is in the receiver user's `authorized_keys` with the `restrict,from=...,command=...` prefix intact.
`from=` must match the unlock device's address as the receiver host sees it.

**Receiver unreachable / daemon stuck in panic mode** — the host is down, or TCP to the SSH port is blocked.
`zfs-unlock doctor` separates DNS resolution, TCP connect, and receiver status so you can see which hop fails.
If `doctor` connects but status fails with a shell error like `command not found`, the receiver wrapper on the host is missing or not on the forced-command path.

**`dataset not allowed`** — the dataset is missing from `/etc/zfs-unlock/allowed-datasets` on the receiver host.
The allowlist is exact-match per line; globs are a client-side convenience only.

**`Key unload error: '<dataset>' is busy` when locking** — a service still has files open.
Stop it first, or use `lock --force` to unmount descendants deliberately.

**`unlocked, not fully mounted` in status** — a mount failed after the key loaded (busy mountpoint, for example).
Run `zfs-unlock unlock` again to remount the subtree; the daemon retries this automatically.
