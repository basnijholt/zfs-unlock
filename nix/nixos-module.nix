{ config, lib, pkgs, ... }:

let
  cfg = config.services.zfs-unlock.receiver;

  hasLineBreak = value:
    lib.hasInfix "\n" value || lib.hasInfix "\r" value;

  safeFromPattern = pattern:
    !hasLineBreak pattern && !lib.hasInfix "\"" pattern;

  safeAuthorizedKey = key:
    !hasLineBreak key;

  safeDatasetName = dataset:
    builtins.match "[A-Za-z0-9_.:-]+(/[A-Za-z0-9_.:-]+)*" dataset != null
    && lib.all
      (segment: segment != "." && segment != ".." && !lib.hasPrefix "-" segment)
      (lib.splitString "/" dataset);

  fromOption = lib.optionalString (cfg.allowedFrom != [ ])
    ''from="${lib.concatStringsSep "," cfg.allowedFrom}",'';

  receiver = pkgs.writeShellScript "zfs-unlock-receiver" ''
    exec ${lib.getExe cfg.package} receiver \
      --allow-file /etc/zfs-unlock/allowed-datasets \
      --zfs-path ${lib.getExe' cfg.zfsPackage "zfs"} \
      "$@"
  '';

  # SSH_ORIGINAL_COMMAND must stay a SINGLE quoted argument: the receiver's
  # defense against pinned-flag override (see _single_receiver_option in
  # cli.py) assumes the untrusted command arrives as one argv element that it
  # shlex-splits itself. Never unquote or word-split it here.
  sshWrapper = pkgs.writeShellScript "zfs-unlock-ssh-wrapper" ''
    set -eu
    exec ${config.security.wrapperDir}/sudo -n ${receiver} "''${SSH_ORIGINAL_COMMAND-}"
  '';

  forcedCommandKey = key: ''restrict,${fromOption}command="${sshWrapper}" ${key}'';
in
{
  options.services.zfs-unlock.receiver = {
    enable = lib.mkEnableOption "the restricted zfs-unlock SSH receiver";

    package = lib.mkOption {
      type = lib.types.package;
      description = ''
        Package providing the zfs-unlock executable.

        The flake's nixosModules.receiver defaults this to the pinned zfs-unlock
        package. When importing nix/nixos-module.nix directly, set it explicitly:
        the receiver runs as root via sudo and must not resolve or download its
        code at runtime.
      '';
    };

    zfsPackage = lib.mkOption {
      type = lib.types.package;
      default = config.boot.zfs.package;
      defaultText = lib.literalExpression "config.boot.zfs.package";
      description = "Package providing the zfs executable used by the receiver.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "zfs-unlock";
      description = "System user that receives forced SSH commands.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "zfs-unlock";
      description = "Primary group for the receiver user.";
    };

    home = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/zfs-unlock";
      description = "Home directory for the receiver user.";
    };

    enableLinger = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Enable systemd linger for the receiver user.

        This keeps the receiver user's systemd user manager stable across short-lived forced-command SSH sessions and
        avoids NixOS switch-time D-Bus races after the receiver account has been used.
      '';
    };

    shell = lib.mkOption {
      type = lib.types.str;
      default = pkgs.runtimeShell;
      defaultText = lib.literalExpression "pkgs.runtimeShell";
      description = "Login shell used by OpenSSH to execute the forced receiver command.";
    };

    allowedFrom = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "192.168.1.7" ];
      description = "OpenSSH authorized_keys from= patterns allowed to use the receiver key.";
    };

    authorizedKeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... pi4-zfs-unlock"
      ];
      description = "Public SSH keys allowed to invoke the forced receiver command.";
    };

    datasets = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "tank/photos"
        "tank/syncthing"
      ];
      description = "OpenZFS dataset names the receiver may inspect, unlock, or lock.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        # Without sshd the forced-command key is never materialized and the
        # receiver silently never works; fail at build time instead.
        assertion = config.services.openssh.enable;
        message = "services.zfs-unlock.receiver requires services.openssh.enable = true.";
      }
      {
        # The SSH wrapper calls the sudo security wrapper unconditionally; with
        # sudo disabled the receiver would fail at runtime with an opaque
        # "command not found" over SSH instead of at build time.
        assertion = config.security.sudo.enable;
        message = "services.zfs-unlock.receiver requires security.sudo.enable = true.";
      }
      {
        assertion = cfg.allowedFrom != [ ];
        message = "services.zfs-unlock.receiver.allowedFrom must include at least one source pattern.";
      }
      {
        assertion = cfg.authorizedKeys != [ ];
        message = "services.zfs-unlock.receiver.authorizedKeys must include at least one public key.";
      }
      {
        assertion = cfg.datasets != [ ];
        message = "services.zfs-unlock.receiver.datasets must include at least one dataset.";
      }
      {
        assertion = lib.all safeFromPattern cfg.allowedFrom;
        message = "services.zfs-unlock.receiver.allowedFrom entries must not contain quotes or newlines.";
      }
      {
        assertion = lib.all safeAuthorizedKey cfg.authorizedKeys;
        message = "services.zfs-unlock.receiver.authorizedKeys entries must not contain newlines.";
      }
      {
        assertion = lib.all safeDatasetName cfg.datasets;
        message = "services.zfs-unlock.receiver.datasets entries must be safe OpenZFS dataset names.";
      }
    ];

    users.groups.${cfg.group} = { };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.home;
      createHome = true;
      linger = lib.mkDefault cfg.enableLinger;
      shell = cfg.shell;
      openssh.authorizedKeys.keys = map forcedCommandKey cfg.authorizedKeys;
    };

    security.sudo.extraRules = [
      {
        users = [ cfg.user ];
        runAs = "root:root";
        commands = [
          {
            command = "${receiver}";
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];

    environment.etc."zfs-unlock/allowed-datasets".text =
      lib.concatMapStringsSep "\n" (dataset: dataset) cfg.datasets + "\n";
  };
}
