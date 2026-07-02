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
  services.zfsUnlock.receiver = {
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

  services.zfsUnlock.client = {
    enable = true;
    user = "alice";
    group = "users";
  };
}
```

The client module creates a `zfs-unlock.service` system service, installs the CLI into the system profile, adds OpenSSH to the service `PATH`, and sets `HOME` and `XDG_CONFIG_HOME` so the normal user config is found.

## Verify

After rebuilding the receiver host, run this on the unlock device:

```bash
zfs-unlock doctor
```

`doctor` checks the config, SSH identity, host reachability, receiver status, and permissions on the configured identity and file-backed dataset secrets.
