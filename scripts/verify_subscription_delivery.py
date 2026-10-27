#!/usr/bin/env python3
"""Check authorized existing subscriptions without printing tokens or configuration."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        source = json.loads(args.source_file.read_text())
        if set(source) != {"phone", "desktop"}:
            raise ValueError("Expected phone and desktop subscription records")
        origin = None
        results = {}
        with httpx.Client(
            verify=True, trust_env=False, follow_redirects=False, timeout=60
        ) as client:
            for label, record in source.items():
                url = record["url"]
                parsed = urlsplit(url)
                current_origin = (parsed.scheme, parsed.hostname, parsed.port)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or (origin is not None and origin != current_origin)
                ):
                    raise ValueError("Subscription origin is unexpected")
                origin = current_origin
                digest, size = hashlib.sha256(), 0
                with client.stream(
                    "GET", url, headers={"User-Agent": "Wayvia-health-check/1"}
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"{label}: subscription HTTP {response.status_code}")
                    for data in response.iter_bytes():
                        size += len(data)
                        if size > 16 * 1024 * 1024:
                            raise RuntimeError("Subscription exceeded safe size")
                        digest.update(data)
                    headers = response.headers
                    homepage = headers.get("profile-web-page-url")
                    if (
                        headers.get("profile-update-interval") != "24"
                        or "no-store" not in headers.get("cache-control", "")
                        or homepage != url.rsplit("/", 1)[0] + "/info"
                    ):
                        raise RuntimeError("Subscription metadata differs from expected")
                    disposition = headers.get("content-disposition", "")
                    if label == "phone" and 'filename="LTY.yaml"' not in disposition:
                        raise RuntimeError("Phone subscription title differs from expected")
                    if label == "desktop" and "JXY" not in disposition:
                        raise RuntimeError("Desktop subscription title differs from expected")
                info = client.get(homepage)
                if info.status_code != 200:
                    raise RuntimeError(f"{label}: info HTTP {info.status_code}")
                data = info.json()
                if data["download_link_expires_at"] != record["expires_at"]:
                    raise RuntimeError("Subscription expiry differs from expected")
                if label == "phone" and data["title"] != "LTY":
                    raise RuntimeError("Phone info title differs from expected")
                results[label] = {
                    "download_status": 200,
                    "info_status": 200,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "tls_verified": True,
                    "update_hours": 24,
                }
        if args.compare:
            baseline = json.loads(args.compare.read_text())
            if results != baseline:
                raise RuntimeError("Subscription content or metadata changed")
        with args.result_file.open("x") as result:
            json.dump(results, result, sort_keys=True)
        print(json.dumps({"status": "passed", "subscriptions": results}, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, httpx.HTTPError) as error:
        # Exceptions can include bearer URLs; only report a safe category.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
