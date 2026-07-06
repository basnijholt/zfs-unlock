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
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonPackages = pkgs.python3Packages;
          version = builtins.replaceStrings [ "\n" "\r" ] [ "" "" ] (builtins.readFile ./VERSION);
        in
        {
          default = pythonPackages.buildPythonApplication {
            pname = "zfs-unlock";
            inherit version;

            pyproject = true;
            src = self;

            build-system = with pythonPackages; [
              hatch-vcs
              hatchling
            ];

            dependencies = with pythonPackages; [
              pydantic
              pyyaml
              rich
              typer
            ];

            SETUPTOOLS_SCM_PRETEND_VERSION = version;

            pythonImportsCheck = [ "zfs_unlock" ];
            doCheck = false;

            passthru.isZfsUnlockPackage = true;

            meta = {
              description = "Unlock encrypted OpenZFS datasets over a restricted SSH receiver";
              homepage = "https://github.com/basnijholt/zfs-unlock";
              license = pkgs.lib.licenses.mit;
              mainProgram = "zfs-unlock";
            };
          };
        }
      );

      nixosModules = {
        default = self.nixosModules.receiver;
        receiver =
          { lib, pkgs, ... }:
          {
            imports = [ ./nix/nixos-module.nix ];
            services.zfs-unlock.receiver.package =
              lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          };
        client =
          { lib, pkgs, ... }:
          {
            imports = [ ./nix/client-module.nix ];
            services.zfs-unlock.client.package =
              lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          };
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
                system.stateVersion = "26.05";
                # Minimal bootable-ish config so forcing `config.assertions`
                # below only surfaces real receiver-module failures.
                boot.loader.grub.enable = false;
                fileSystems."/" = {
                  device = "none";
                  fsType = "tmpfs";
                };
                services.openssh.enable = true;
                services.zfs-unlock.receiver = {
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
          # Reading environment.etc directly would skip module assertions
          # (they only fire when building the system toplevel), so check
          # them explicitly here.
          failedAssertions = builtins.filter (a: !a.assertion) eval.config.assertions;
          allowedDatasetsText =
            if failedAssertions == [ ] then
              eval.config.environment.etc."zfs-unlock/allowed-datasets".text
            else
              throw (nixpkgs.lib.concatMapStringsSep "\n" (a: a.message) failedAssertions);
          allowedDatasets = pkgs.writeText "allowed-datasets" allowedDatasetsText;
          sudoers = pkgs.writeText "sudoers" eval.config.security.sudo.configFile;
        in
        {
          receiverModule = pkgs.runCommand "zfs-unlock-receiver-module-check" { } ''
            grep -qx "tank/photos" ${allowedDatasets}
            grep -qx "tank/syncthing" ${allowedDatasets}
            grep -qE '^zfs-unlock[[:space:]]+ALL=\(root:root\)[[:space:]]+NOPASSWD:' ${sudoers}
            touch "$out"
          '';
        }
        # End-to-end VM test against real OpenZFS. Only x86_64-linux runs it in
        # CI (KVM there); on other systems it would need cross-emulation.
        // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          integration = import ./nix/integration-test.nix {
            inherit pkgs;
            receiverModule = self.nixosModules.receiver;
            zfsUnlockPackage = self.packages.${system}.default;
          };
        }
      );
    };
}
