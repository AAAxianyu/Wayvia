#!/usr/bin/env python3
"""Reproduce the bundled domestic domain rules from one pinned upstream commit."""

import argparse
import json
import os
import tempfile
import urllib.request
from pathlib import Path

from wayvia.config import RuleSetImporter
from wayvia.errors import WayviaError

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a66b6ffc158681a3be06c258893d58a4265b76e5"
SOURCE = (
    "https://raw.githubusercontent.com/felixonmars/dnsmasq-china-list/"
    + COMMIT
    + "/accelerated-domains.china.conf"
)
SOURCE_SHA256 = "c030567d770eb94e8fd56e75392ebb10ff5d4c3cf6e79d903c546f575a0145e3"
MAX_BYTES = 16 * 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="verify a local copy instead of downloading")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "src/wayvia/config/data/domestic-domains.json"
    )
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="wayvia-domestic-rules-") as temporary:
            source = args.source
            if source is None:
                request = urllib.request.Request(
                    SOURCE, headers={"User-Agent": "Wayvia-rule-source/1"}
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise WayviaError(
                        "rule_source_too_large", "Pinned rule source exceeded size limit"
                    )
                source = Path(temporary) / "accelerated-domains.china.conf"
                source.write_bytes(raw)
            rules = RuleSetImporter.from_dnsmasq(
                source, source=SOURCE, version=COMMIT, expected_sha256=SOURCE_SHA256
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.is_symlink():
                raise WayviaError("rule_output_symlink", "Rule output cannot be a symlink")
            fd, temporary_output = tempfile.mkstemp(prefix=".wayvia-rules-", dir=args.output.parent)
            try:
                with os.fdopen(fd, "w") as target:
                    json.dump(rules.model_dump(mode="json"), target, indent=2, ensure_ascii=False)
                    target.write("\n")
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(temporary_output, 0o644)
                os.replace(temporary_output, args.output)
            finally:
                Path(temporary_output).unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "status": "generated",
                    "source": SOURCE,
                    "version": COMMIT,
                    "source_sha256": rules.source_sha256,
                    "sha256": rules.sha256,
                    "domain_suffixes": len(rules.domains),
                    "output": str(args.output),
                    "dns_upstreams_inherited": False,
                }
            )
        )
        return 0
    except WayviaError as error:
        print(json.dumps(error.as_dict()))
        return int(error.exit_code)
    except (OSError, ValueError):
        print(
            json.dumps(
                {
                    "code": "rule_source_unavailable",
                    "message": "Pinned rule source could not be downloaded or written",
                }
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
