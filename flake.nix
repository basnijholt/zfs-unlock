{
  description = "Unlock encrypted OpenZFS datasets over a restricted SSH receiver";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      nixosModules = {
        default = self.nixosModules.receiver;
        receiver = import ./nix/nixos-module.nix;
      };

      checks = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          eval = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.receiver
              {
                services.zfsUnlock.receiver = {
                  enable = true;
                  allowedFrom = [ "192.0.2.7" ];
                  authorizedKeys = [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock"
                  ];
                  datasets = [
                    "tank/photos"
                    "tank/syncthing"
                  ];
                  package = pkgs.writeShellScriptBin "zfs-unlock" "exit 0";
                };
              }
            ];
          };
          allowedDatasets = pkgs.writeText "allowed-datasets" eval.config.environment.etc."zfs-unlock/allowed-datasets".text;
        in
        {
          receiverModule = pkgs.runCommand "zfs-unlock-receiver-module-check" { } ''
            grep -qx "tank/photos" ${allowedDatasets}
            grep -qx "tank/syncthing" ${allowedDatasets}
            touch "$out"
          '';
        }
      );
    };
}
