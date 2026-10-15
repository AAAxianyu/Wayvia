# nftables baseline fail-closed follow-up

Date: 2026-10-15 (Asia/Shanghai)  
Branch: `QOKIK`  
Scope: local working tree; this follow-up is not committed

## Change

The nftables JSON reader now accepts only `metainfo`, `table`, `chain`, and `rule` entries in the relevant command outputs. Within the target table, unknown object kinds raise `AdapterError("BASELINE_INVALID")` instead of being included as if they were a understood baseline shape. The current Wayvia guard and NAT table generators create only tables, chains, and rules. Existing counter and handle normalization remains unchanged.

A new public-seam regression invokes `LinuxExecutor.baseline()` with an unknown object whose family and table otherwise match. The test failed before the change and passes after it. The complete focused suite passed 18 tests under Python 3.12.3.

## Validation

| Check | Result |
| --- | --- |
| `make check` | Passed: 316 tests, Ruff format/check, sdist/wheel build, CLI help; 3 deprecation warnings. See [check.txt](check.txt). |
| `make demo` | Passed. See [demo.txt](demo.txt). |
| `make test-configs` | Passed: all 10 artifacts. See [test-configs.txt](test-configs.txt). |
| `make test-lab` | The clean build path exited 2 because Ubuntu's package source now offers `systemd 1ubuntu8.18` while `Dockerfile.systemd` pins `1ubuntu8.17`. The full lab passed using existing locked images with `scripts/lab.py --no-build`; this covered 23 core and 23 transaction cases. See [test-lab.txt](test-lab.txt), [test-lab-reuse-images.txt](test-lab-reuse-images.txt), and [reused-image artifacts](test-lab-artifacts-reused/). |
| Live executor probe | Passed against real nftables in a disposable Linux container. Counter changed from 2 packets / 148 bytes to 8 / 552 with the baseline unchanged; an actual rule change changed the baseline and the stale plan was rejected. Cleanup confirmed in the probe result. See [executor-result.json](test-lab-executor-only/executor-result.json). |

The follow-up did not change package pins or Dockerfiles to work around the external package drift. Existing `latest-configs` and `latest-lab` evidence was restored after the runs. This remains isolated Linux validation, not real-server or mobile acceptance.
