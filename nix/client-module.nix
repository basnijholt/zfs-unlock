{ config, lib, pkgs, ... }:

let
  cfg = config.services.zfs-unlock.client;

  home = config.users.users.${cfg.user}.home or (
    if cfg.user == "root" then "/root" else "/home/${cfg.user}"
  );

  configArgs = lib.optionals (cfg.configFile != null) [
    "--config"
    (toString cfg.configFile)
  ];

  execArgs = [
    "unlock"
    "--daemon"
    "--interval"
    (toString cfg.interval)
  ] ++ configArgs ++ cfg.extraArgs;
in
{
  options.services.zfs-unlock.client = {
    enable = lib.mkEnableOption "the zfs-unlock client daemon";

    package = lib.mkOption {
      type = lib.types.package;
      description = ''
        Package providing the zfs-unlock executable.

        The flake's nixosModules.client defaults this to the pinned zfs-unlock
        package. When importing nix/client-module.nix directly, set it explicitly:
        the daemon handles dataset passphrases and must not resolve or download
        its code at runtime.
      '';
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

        # The daemon reads dataset passphrases and pipes them to outbound ssh;
        # it never writes outside the journal. Sandbox it accordingly: the
        # filesystem stays read-only ($HOME config, secret files, and the SSH
        # identity remain readable), privileges cannot grow, and only the
        # address families ssh needs are available.
        #
        # Read-only $HOME also means ssh cannot persist new known_hosts
        # entries. Pin the receiver's host key before enabling the daemon
        # (`zfs-unlock doctor` prints the ssh-keyscan command), as the README
        # already requires for passphrase secrecy.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallArchitectures = "native";
        UMask = "0077";
      } // lib.optionalAttrs (cfg.group != null) {
        Group = cfg.group;
      };
    };
  };
}
