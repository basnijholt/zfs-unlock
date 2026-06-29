{ config, lib, pkgs, ... }:

let
  cfg = config.services.zfsUnlock.client;

  home = config.users.users.${cfg.user}.home or (
    if cfg.user == "root" then "/root" else "/home/${cfg.user}"
  );

  configArgs = lib.optionals (cfg.configFile != null) [
    "--config"
    (toString cfg.configFile)
  ];

  execArgs = [
    "--daemon"
    "--interval"
    (toString cfg.interval)
  ] ++ configArgs ++ cfg.extraArgs;
in
{
  options.services.zfsUnlock.client = {
    enable = lib.mkEnableOption "the zfs-unlock client daemon";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.writeShellScriptBin "zfs-unlock" ''
        exec ${pkgs.uv}/bin/uv tool run zfs-unlock "$@"
      '';
      defaultText = lib.literalExpression ''pkgs.writeShellScriptBin "zfs-unlock" "exec \${pkgs.uv}/bin/uv tool run zfs-unlock \"$@\""'';
      description = "Package providing the zfs-unlock executable.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "root";
      description = "User that runs the client daemon and owns the client config.";
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "users";
      description = "Optional group for the client daemon.";
    };

    interval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 30;
      description = "Seconds between healthy daemon polling passes.";
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/home/alice/.config/zfs-unlock/config.yaml";
      description = "Optional explicit client configuration file.";
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "--dataset" "tank/photos" ];
      description = "Additional arguments appended to the daemon command.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];

    systemd.services.zfs-unlock = {
      description = "ZFS Unlock Daemon";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      path = [ pkgs.openssh ];
      environment = {
        HOME = home;
        XDG_CONFIG_HOME = "${home}/.config";
      };

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        ExecStart = "${lib.getExe cfg.package} ${lib.escapeShellArgs execArgs}";
        Restart = "on-failure";
        RestartSec = "10s";
      } // lib.optionalAttrs (cfg.group != null) {
        Group = cfg.group;
      };
    };
  };
}
