# nftables baseline counter fix validation

Date: 2026-10-14 (Asia/Shanghai)  
Branch: `QOKIK`  
Scope: local working tree; no commit or push

## Change verified

For `guard` and `exit_nat`, `LinuxExecutor.baseline()` reads nftables JSON for the target table and compares table, chain, and rule structure in order. It omits only numeric `counter.packets` / `counter.bytes` values and nft-assigned `handle` fields on table, chain, and rule objects. Other fields, nested values, and list order remain in the comparison. Malformed JSON, unexpected structures, and read failures fail closed. `inspect_action()` retains the full text snapshot for rollback.

The focused regression set passed 17 tests. It covers counter-only changes, handles, firewall action/port/policy, rule order, routes, NAT parameters, complete rollback snapshots, malformed output, command failures, and absent tables. Ruff also passed; see [focused-tests.txt](focused-tests.txt).

## Requested gates

| Gate | Final result |
| --- | --- |
| `make check` | Passed on the final code tree: 315 tests, Ruff format/check, source distribution and wheel builds, and CLI help. Three dependency deprecation warnings. See [check-retest.txt](check-retest.txt). |
| `make demo` | Passed, exit 0; simulated control-plane demo. See [demo-retest.txt](demo-retest.txt). |
| `make test-configs` | Passed, exit 0; all 10 configuration artifacts validated. See [test-configs-retest.txt](test-configs-retest.txt) and [config-result.json](test-configs-artifacts/config-result.json). |
| `make test-lab` | Passed, exit 0; 23 core lab cases and 23 installed transaction cases. The real Linux executor probe also passed the live nftables baseline regression described below. See [test-lab-final.txt](test-lab-final.txt), [lab-result.json](test-lab-artifacts-baseline-final/lab-result.json), and [transaction-result.json](test-lab-artifacts-baseline-final/transaction-result.json). |

### Real-kernel baseline regression

The probe used the production `LinuxExecutor` and real Linux nftables in a disposable Docker Linux container. A blocked TCP connection increased the rule counter from 1 packet / 76 bytes to 7 packets / 476 bytes while the baseline stayed unchanged. Adding an actual structural nftables rule changed the baseline, and applying a stale plan was rejected with `BASELINE_CHANGED`. Probe evidence: [executor-result.json](test-lab-artifacts-baseline-final/executor-result.json). The probe removed its namespace, veth, and owned guard table; the host default route remained unchanged.

## Retry and environment notes

The Docker Engine was unavailable during the first attempts. After it started, the final lab run passed. One intermediate full-lab retry encountered a transient preflight snapshot conflict; the next complete run passed. A Windows host invocation initially lacked OpenSSL on PATH; the final Linux lab run used the available Git OpenSSL path and passed. The native Windows `make demo` launcher also had a path-enumeration error; the same project gate passed under WSL Linux. These failed attempts are retained as retry history and do not replace the final passing logs.

The existing `docs/validation/latest-configs/` and `docs/validation/latest-lab/` evidence was restored and its hashes verified after the runs. No production server or host firewall/network configuration was changed. This local isolated Linux acceptance does not establish real-server deployment acceptance; M6 remains open and OpenWrt M7 remains unimplemented / unaccepted.
