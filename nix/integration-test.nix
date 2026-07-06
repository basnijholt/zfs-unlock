# End-to-end integration test against *real* OpenZFS.
#
# The unit suite drives mocked runners, so it proves the receiver's logic given
# assumed `zfs` output. This test proves those assumptions: it boots a VM with a
# real kernel and real OpenZFS, imports the actual receiver NixOS module, builds
# a genuine encrypted dataset on a loopback pool, and exercises the full trust
# chain the way a client does — ssh (forced command) -> sudo -> receiver ->
# `zfs load-key`/`mount`/`unload-key` — including the security-critical refusals.
{
  pkgs,
  receiverModule,
  zfsUnlockPackage,
}:
let
  # Throwaway keypair committed under nix/integration/. It authenticates only
  # this ephemeral test VM and protects nothing real.
  publicKey = pkgs.lib.strings.trim (builtins.readFile ./integration/id_test.pub);
  privateKey = ./integration/id_test;

  passphrase = "correcthorsebatterystaple";
in
pkgs.testers.runNixOSTest {
  name = "zfs-unlock-integration";

  nodes.machine =
    { ... }:
    {
      imports = [ receiverModule ];

      # Real ZFS in the guest.
      boot.supportedFilesystems = [ "zfs" ];
      # ZFS refuses to import without a hostId; the value is arbitrary.
      networking.hostId = "deadbeef";
      # A spare virtio disk (/dev/vdb) to back the test pool.
      virtualisation.emptyDiskImages = [ 512 ];
      virtualisation.memorySize = 2048;

      services.openssh.enable = true;

      services.zfs-unlock.receiver = {
        enable = true;
        package = zfsUnlockPackage;
        # The client connects over loopback, so authorize that source.
        allowedFrom = [
          "127.0.0.1"
          "::1"
        ];
        authorizedKeys = [ publicKey ];
        datasets = [ "tank/enc" ];
      };
    };

  testScript = ''
    ssh = (
        "ssh -i /root/id_test -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o BatchMode=yes zfs-unlock@127.0.0.1"
    )

    start_all()
    machine.wait_for_unit("sshd.service")
    machine.succeed("install -m600 ${privateKey} /root/id_test")

    with subtest("a real encrypted dataset exists and starts unlocked"):
        machine.succeed("zpool create -f tank /dev/vdb")
        machine.succeed("printf '${passphrase}' > /root/pp")
        machine.succeed(
            "zfs create -o encryption=on -o keyformat=passphrase "
            "-o keylocation=file:///root/pp tank/enc"
        )
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = available")

    with subtest("locking it leaves the key unavailable"):
        # Switch to prompt so a later load-key must supply the passphrase.
        machine.succeed("zfs set keylocation=prompt tank/enc")
        # A mounted dataset is busy; unmount before the key can be unloaded.
        machine.succeed("zfs unmount tank/enc")
        machine.succeed("zfs unload-key tank/enc")
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = unavailable")

    with subtest("receiver reports the dataset as locked"):
        assert machine.succeed(f"{ssh} status tank/enc").strip() == "locked"

    with subtest("a wrong passphrase is refused and leaks nothing, dataset stays locked"):
        out = machine.fail(f"printf 'not-the-passphrase\\n' | {ssh} unlock tank/enc 2>&1")
        assert "load-key failed" in out, out
        assert "not-the-passphrase" not in out, "receiver must not echo attempted key material"
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = unavailable")

    with subtest("the correct passphrase unlocks and mounts the subtree"):
        out = machine.succeed(f"printf '${passphrase}\\n' | {ssh} unlock tank/enc")
        assert "unlocked tank/enc" in out, out
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = available")
        machine.succeed("test \"$(zfs get -H -o value mounted tank/enc)\" = yes")

    with subtest("receiver reports the dataset as unlocked"):
        assert machine.succeed(f"{ssh} status tank/enc").strip() == "unlocked"

    with subtest("plain lock is the safe path and refuses to disrupt a mounted dataset"):
        # `unload-key` fails on a busy (mounted) dataset; plain lock surfaces
        # that as a clean error rather than force-unmounting behind your back.
        # Assert on the message so a future failure for a *different* reason
        # (e.g. an allowlist regression) doesn't silently satisfy this subtest.
        out = machine.fail(f"{ssh} lock tank/enc 2>&1")
        assert "busy" in out, out
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = available")

    with subtest("lock --force unmounts the subtree and unloads the key"):
        out = machine.succeed(f"{ssh} lock tank/enc --force")
        assert "locked tank/enc" in out, out
        machine.succeed("test \"$(zfs get -H -o value keystatus tank/enc)\" = unavailable")
        machine.succeed("test \"$(zfs get -H -o value mounted tank/enc)\" = no")

    with subtest("a non-allowlisted dataset is refused"):
        out = machine.fail(f"{ssh} status tank/secret 2>&1")
        assert "not allowed" in out, out

    with subtest("an arbitrary command is refused and does NOT run on the host"):
        # If the forced-command parser leaked, `reboot` would run as root via
        # sudo and kill the VM. Refusal keeps the machine up.
        out = machine.fail(f"{ssh} reboot 2>&1")
        assert "unsupported command" in out, out
        machine.succeed("true")
  '';
}
