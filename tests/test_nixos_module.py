"""Tests for the exported NixOS receiver module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_nix_eval(expr: str, repo: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a Nix eval expression for module tests."""
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

    repo = repo or Path(__file__).resolve().parents[1]
    return subprocess.run(
        [
            nix,
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--json",
            "--expr",
            expr,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_nixos_receiver_module_generates_restricted_receiver_config() -> None:
    """The flake exports a NixOS module that generates the receiver policy."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        pkgs = import flake.inputs.nixpkgs {{ inherit system; }};
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.receiver
            ({{
              services.zfsUnlock.receiver = {{
                enable = true;
                allowedFrom = [ "192.0.2.7" ];
                authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock" ];
                datasets = [ "tank/photos" "tank/syncthing" ];
                package = pkgs.writeShellScriptBin "zfs-unlock" "exit 0";
              }};
            }})
          ];
        }};
      in {{
        allowedDatasets = eval.config.environment.etc."zfs-unlock/allowed-datasets".text;
        authorizedKeys = eval.config.users.users.zfs-unlock.openssh.authorizedKeys.keys;
        shell = toString eval.config.users.users.zfs-unlock.shell;
        sudoUsers = builtins.map (rule: rule.users) eval.config.security.sudo.extraRules;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["allowedDatasets"] == "tank/photos\ntank/syncthing\n"
    assert len(data["authorizedKeys"]) == 1
    assert data["authorizedKeys"][0].startswith('restrict,from="192.0.2.7",command="/nix/store/')
    assert "zfs-unlock-ssh-wrapper" in data["authorizedKeys"][0]
    assert data["authorizedKeys"][0].endswith(
        '" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock',
    )
    assert data["shell"].endswith("/bin/bash")
    assert ["zfs-unlock"] in data["sudoUsers"]


def test_nixos_receiver_module_defaults_to_flake_python_package() -> None:
    """The flake module defaults to the packaged app, not a uv tool wrapper."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.receiver
            ({{
              services.zfsUnlock.receiver = {{
                enable = true;
                allowedFrom = [ "192.0.2.7" ];
                authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock" ];
                datasets = [ "tank/photos" ];
              }};
            }})
          ];
        }};
      in {{
        hasPackage = builtins.hasAttr system flake.packages
          && builtins.hasAttr "default" flake.packages.${{system}};
        usesPackagedApp = eval.config.services.zfsUnlock.receiver.package.passthru.isZfsUnlockPackage or false;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {"hasPackage": True, "usesPackagedApp": True}


def test_nixos_receiver_module_enables_linger_by_default() -> None:
    """The receiver user keeps a stable user manager by default."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        pkgs = import flake.inputs.nixpkgs {{ inherit system; }};
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.receiver
            ({{
              services.zfsUnlock.receiver = {{
                enable = true;
                allowedFrom = [ "192.0.2.7" ];
                authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock" ];
                datasets = [ "tank/photos" ];
                package = pkgs.writeShellScriptBin "zfs-unlock" "exit 0";
              }};
            }})
          ];
        }};
      in {{
        linger = eval.config.users.users.zfs-unlock.linger or null;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["linger"] is True


def test_nixos_receiver_module_can_disable_linger() -> None:
    """Users can opt out of receiver user linger."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        pkgs = import flake.inputs.nixpkgs {{ inherit system; }};
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.receiver
            ({{
              services.zfsUnlock.receiver = {{
                enable = true;
                enableLinger = false;
                allowedFrom = [ "192.0.2.7" ];
                authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock" ];
                datasets = [ "tank/photos" ];
                package = pkgs.writeShellScriptBin "zfs-unlock" "exit 0";
              }};
            }})
          ];
        }};
      in {{
        linger = eval.config.users.users.zfs-unlock.linger or null;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["linger"] is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ('allowedFrom = [ "192.0.2.7\\"" ];', "allowedFrom entries must not contain quotes or newlines"),
        (
            'authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey\\nssh-ed25519 AAAAInjected" ];',
            "authorizedKeys entries must not contain newlines",
        ),
        ('datasets = [ "tank/photos bad" ];', "datasets entries must be safe OpenZFS dataset names"),
    ],
)
def test_nixos_receiver_module_rejects_unsafe_policy_strings(override: str, message: str) -> None:
    """Unsafe receiver option strings fail evaluation before policy files are generated."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        pkgs = import flake.inputs.nixpkgs {{ inherit system; }};
        receiverConfig = {{
          enable = true;
          allowedFrom = [ "192.0.2.7" ];
          authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyKey pi4-zfs-unlock" ];
          datasets = [ "tank/photos" ];
          package = pkgs.writeShellScriptBin "zfs-unlock" "exit 0";
        }} // {{
          {override}
        }};
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.receiver
            ({{
              system.stateVersion = "26.11";
              services.zfsUnlock.receiver = receiverConfig;
            }})
          ];
        }};
      in eval.config.system.build.toplevel.drvPath
    """

    result = run_nix_eval(expr, repo)

    assert result.returncode != 0
    assert message in result.stderr


def test_nixos_client_module_generates_packaged_daemon_service() -> None:
    """The flake exports a NixOS client module for a packaged daemon."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
        eval = flake.inputs.nixpkgs.lib.nixosSystem {{
          inherit system;
          modules = [
            flake.nixosModules.client
            ({{
              users.groups.users = {{}};
              users.users.alice = {{
                isNormalUser = true;
                group = "users";
                home = "/home/alice";
              }};
              services.zfsUnlock.client = {{
                enable = true;
                user = "alice";
                group = "users";
                interval = 45;
              }};
            }})
          ];
        }};
        service = eval.config.systemd.services.zfs-unlock;
      in {{
        usesPackagedApp = eval.config.services.zfsUnlock.client.package.passthru.isZfsUnlockPackage or false;
        systemPackages = builtins.map (pkg: pkg.pname or pkg.name) eval.config.environment.systemPackages;
        user = service.serviceConfig.User;
        group = service.serviceConfig.Group;
        execStart = service.serviceConfig.ExecStart;
        environment = service.environment;
        path = builtins.map toString service.path;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["usesPackagedApp"] is True
    assert "zfs-unlock" in data["systemPackages"]
    assert data["user"] == "alice"
    assert data["group"] == "users"
    assert " unlock --daemon --interval 45" in data["execStart"]
    assert "uvx" not in data["execStart"]
    assert data["environment"]["HOME"] == "/home/alice"
    assert data["environment"]["XDG_CONFIG_HOME"] == "/home/alice/.config"
    assert any(path.endswith("-openssh-10.3p1") or "openssh" in path for path in data["path"])


def test_nix_package_version_comes_from_committed_version_file() -> None:
    """The Nix package reports the release version instead of a 0.0.0 commit fallback."""
    repo = Path(__file__).resolve().parents[1]
    expr = f"""
      let
        flake = builtins.getFlake "path:{repo}";
        system = builtins.currentSystem;
      in {{
        expected = builtins.replaceStrings ["\\n"] [""] (builtins.readFile {repo / "VERSION"});
        actual = flake.packages.${{system}}.default.version;
      }}
    """
    result = run_nix_eval(expr, repo)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["actual"] == data["expected"]
    assert not data["actual"].startswith("0.0.0+")


def test_nixos_receiver_wrapper_uses_setuid_sudo_wrapper() -> None:
    """The forced SSH command must call NixOS's setuid sudo wrapper."""
    repo = Path(__file__).resolve().parents[1]
    module = (repo / "nix" / "nixos-module.nix").read_text()

    assert "${config.security.wrapperDir}/sudo" in module
    assert "${pkgs.sudo}/bin/sudo" not in module
