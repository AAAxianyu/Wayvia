import contextlib, json, subprocess, time, pathlib, collections
from wayvia.agent import probe_runtime
from wayvia.agent.http_probe import matches_body

root = pathlib.Path("/var/lib/wayvia/agent")
gen = json.loads((root / "ingress-runtime.json").read_text())["transaction_id"]
journal = json.loads((root / (gen + ".json")).read_text())
payload = next(p for p in journal["plan"]["probes"] if p["kind"] == "trojan_http")
payload.update(config_dir="/etc/wayvia", timeout=11.5)
original_run = subprocess.run
original_relay = probe_runtime.trojan_relay
current = []


def traced_run(argv, **kwargs):
    result = original_run(argv, **kwargs)
    if argv[0] == "curl":
        body, code = result.stdout.decode().rsplit("\n", 1)
        current.append(
            {
                "curl_code": result.returncode,
                "http_status": int(code),
                "valid_identity": matches_body(
                    body, payload["expected"], payload["response_format"]
                ),
            }
        )
    return result


@contextlib.contextmanager
def relay(*a, **kw):
    try:
        with original_relay(*a, **kw) as port:
            yield port
    except Exception as e:
        current.append(
            {"relay_exception_type": type(e).__name__, "errno": getattr(e, "errno", None)}
        )
        raise


subprocess.run = traced_run
probe_runtime.trojan_relay = relay
rows = []
start = time.monotonic()
for i in range(100):
    current = []
    result = probe_runtime.run(payload)
    if (
        len(result.get("attempts", [])) > 1
        or not result["healthy"]
        or any("relay_exception_type" in x for x in current)
    ):
        rows.append({"sample": i + 1, "result": result, "sequence": current})
    time.sleep(0.03)
print(
    json.dumps(
        {
            "scope": "100_read_only_business_probes_no_monitor_mutation_or_fault_injection",
            "samples": 100,
            "elapsed_seconds": time.monotonic() - start,
            "exception_or_fallback_samples": rows,
        },
        indent=2,
    )
)
