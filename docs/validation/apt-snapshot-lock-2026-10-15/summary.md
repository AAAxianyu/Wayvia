# Ubuntu APT snapshot lock for lab image builds

Date: 2026-10-15 (Asia/Shanghai)
Branch: `QOKIK`
Scope: local working tree; not committed

## Finding

The latest GitHub Actions run failed only in `isolated-linux` at `make test-lab`. Its clean build selected `libsystemd-shared 255.4-1ubuntu8.18` while `Dockerfile.systemd` pins `systemd` and `systemd-sysv` to `255.4-1ubuntu8.17`. The `software` job passed. This matches the local clean-build failure.

## Change

The lab base Dockerfile now writes an APT snapshot ID to `/etc/apt/apt.conf.d/50snapshot` before updating package indexes. The ID is `20261014T064100Z`, chosen from the window of the preceding successful CI lab build. Derived systemd and transaction images inherit the same APT configuration. Existing exact package pins remain in place. `ca-certificates` is installed first from Ubuntu's normal signed archive so APT can validate the snapshot service's HTTPS certificate; this bootstrap package remains outside the snapshot lock.

The package snapshot policy is recorded in [ADR 005](../../adr/005-ubuntu-apt-snapshot-for-lab-images.md).

## Validation status

The first clean build attempt reached the new snapshot source but failed TLS validation because the base image had no system CA certificates. The Dockerfile now bootstraps `ca-certificates` from the regular signed Ubuntu archive before enabling the snapshot.

The next clean base-image build succeeded. The pinned OpenVPN package was fetched from snapshot `20261014T064100Z`; the derived systemd image installed matching pinned `systemd`, `systemd-sysv`, and `libsystemd-shared` version `255.4-1ubuntu8.17`. Core path, protocol, DNS, executor, real nftables counter regression, and systemd recovery tests passed.

The first two `make test-lab` invocations exited nonzero at the final transaction-image build: Python's `build` tool could not call `os.getcwd()` because the subprocess inherited an unavailable WSL working directory. A cwd trace narrowed the trigger to the lab container writing evidence through a writable Docker bind mount sourced from `/mnt/d`: afterward, `/proc/self/cwd` omitted the `/mnt/d` mount prefix and `os.getcwd()` failed. Plain and privileged containers, a non-writing D: bind mount, host-side D: writes, and a container writing to `/tmp` did not reproduce it; explicitly changing directory back to the repository restored `getcwd()`. The trace is in `preflight-cwdtrace.txt` and `test-lab-cwdtrace.txt`.

Running the transaction lab from `/tmp` built the locked transaction image and passed all 23 transaction cases. A complete `scripts/lab.py --no-build` run from `/tmp`, using the newly rebuilt base/systemd images and that transaction image, exited 0: 23 core checks, 7 protocol checks, 12 preflight conflict checks, 6 DNS checks, systemd timer/restart recovery, and all 23 transaction cases passed. Its test artifacts are preserved in `rebuilt-latest-lab/`; the prior `docs/validation/latest-lab/` artifacts were restored and SHA-256 verified.

This validates the local x86_64 WSL2/Docker path. It does not replace a new GitHub Actions run. Keep the WSL process cwd on the Linux filesystem (for example `/tmp`) while Docker writes to the D: bind mount; invoking the complete script from `/tmp` passed. Logs: `test-lab.txt` (initial attempt), `test-lab-rerun.txt` (repeat), `transaction-rerun.txt` (standalone transaction), `test-lab-from-tmp.txt` (complete passing run), and the two cwd traces above.
