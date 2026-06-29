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
