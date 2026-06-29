"""Tests for the exported NixOS receiver module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_nixos_receiver_module_generates_restricted_receiver_config() -> None:
    """The flake exports a NixOS module that generates the receiver policy."""
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

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
    result = subprocess.run(
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
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

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
    result = subprocess.run(
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

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {"hasPackage": True, "usesPackagedApp": True}


def test_nixos_client_module_generates_packaged_daemon_service() -> None:
    """The flake exports a NixOS client module for a packaged daemon."""
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

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
        user = service.serviceConfig.User;
        group = service.serviceConfig.Group;
        execStart = service.serviceConfig.ExecStart;
        environment = service.environment;
        path = builtins.map toString service.path;
      }}
    """
    result = subprocess.run(
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

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["usesPackagedApp"] is True
    assert data["user"] == "alice"
    assert data["group"] == "users"
    assert "--daemon --interval 45" in data["execStart"]
    assert "uvx" not in data["execStart"]
    assert data["environment"]["HOME"] == "/home/alice"
    assert data["environment"]["XDG_CONFIG_HOME"] == "/home/alice/.config"
    assert any(path.endswith("-openssh-10.3p1") or "openssh" in path for path in data["path"])


def test_nix_package_version_comes_from_committed_version_file() -> None:
    """The Nix package reports the release version instead of a 0.0.0 commit fallback."""
    nix = shutil.which("nix")
    if nix is None:
        pytest.skip("nix is not installed")

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
    result = subprocess.run(
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
