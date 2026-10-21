from pathlib import Path
import time, json, hashlib, yaml, cProfile, pstats, io
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ProfileRenderer
from wayvia.policy import PolicyCompiler

out = Path("docs/validation/server-followup-2026-10-20")
rows = {}


def measure(name, fn):
    start = time.perf_counter()
    v = fn()
    rows[name] = round(time.perf_counter() - start, 6)
    return v


config = measure("load_example_config", lambda: WayviaConfig.model_validate(example_config()))
measure(
    "published_snapshot_validation",
    lambda: WayviaConfig.model_validate_json(config.model_dump_json()),
)
measure("policy_compile", lambda: PolicyCompiler(config).compile())
profile = measure(
    "mihomo_build", lambda: ProfileRenderer(config)._mihomo("synthetic-benchmark-password")
)
text = measure("pure_safe_yaml_dump", lambda: yaml.safe_dump(profile, sort_keys=False))
fast = measure(
    "C_safe_yaml_dump", lambda: yaml.dump(profile, Dumper=yaml.CSafeDumper, sort_keys=False)
)
rows.update(
    profile_bytes=len(text.encode()),
    bytes_equal=text == fast,
    semantics_equal=yaml.load(text, Loader=yaml.CSafeLoader)
    == yaml.load(fast, Loader=yaml.CSafeLoader),
    yaml_version=yaml.__version__,
    libyaml=bool(yaml.__with_libyaml__),
    rules=len(profile["rules"]),
    nameserver_policy=len(profile["dns"]["nameserver-policy"]),
    scope="local_full_example_rules_synthetic_credential_no_network",
)
(out / "render-baseline.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows, indent=2), flush=True)
