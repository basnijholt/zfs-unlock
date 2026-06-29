{ config, lib, pkgs, ... }:

let
  cfg = config.services.zfsUnlock.receiver;

  fromOption = lib.optionalString (cfg.allowedFrom != [ ])
    ''from="${lib.concatStringsSep "," cfg.allowedFrom}",'';

  receiver = pkgs.writeShellScript "zfs-unlock-receiver" ''
    exec ${lib.getExe cfg.package} receiver \
      --allow-file /etc/zfs-unlock/allowed-datasets \
      --zfs-path ${lib.getExe' cfg.zfsPackage "zfs"} \
      "$@"
  '';

  sshWrapper = pkgs.writeShellScript "zfs-unlock-ssh-wrapper" ''
    set -eu
    exec ${config.security.wrapperDir}/sudo -n ${receiver} "''${SSH_ORIGINAL_COMMAND-}"
  '';

  forcedCommandKey = key: ''restrict,${fromOption}command="${sshWrapper}" ${key}'';
in
{
  options.services.zfsUnlock.receiver = {
    enable = lib.mkEnableOption "the restricted zfs-unlock SSH receiver";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.writeShellScriptBin "zfs-unlock" ''
        exec ${pkgs.uv}/bin/uv tool run zfs-unlock "$@"
      '';
      defaultText = lib.literalExpression ''pkgs.writeShellScriptBin "zfs-unlock" "exec \${pkgs.uv}/bin/uv tool run zfs-unlock \"$@\""'';
      description = "Package providing the zfs-unlock executable.";
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
        assertion = cfg.allowedFrom != [ ];
        message = "services.zfsUnlock.receiver.allowedFrom must include at least one source pattern.";
      }
      {
        assertion = cfg.authorizedKeys != [ ];
        message = "services.zfsUnlock.receiver.authorizedKeys must include at least one public key.";
      }
      {
        assertion = cfg.datasets != [ ];
        message = "services.zfsUnlock.receiver.datasets must include at least one dataset.";
      }
    ];

    users.groups.${cfg.group} = { };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.home;
      createHome = true;
      shell = cfg.shell;
      openssh.authorizedKeys.keys = map forcedCommandKey cfg.authorizedKeys;
    };

    security.sudo.extraRules = [
      {
        users = [ cfg.user ];
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
