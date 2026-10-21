"""One-off reviewed independent-module maintenance, not a general upgrade installer."""

import pathlib, hashlib, json, zipfile, subprocess, sys, os, fcntl, csv, io, base64, tempfile, datetime, time

os.umask(0o077)
sha = lambda b: hashlib.sha256(b).hexdigest()
root = pathlib.Path("/opt/wayvia")
site = root / ".venv/lib/python3.12/site-packages"
manifest_path = pathlib.Path("/var/lib/wayvia/install-manifest.json")
old_hash = "2d76abb1ccd6c9eff7302995d0f55bfe67a8c6c2b5b48643c9ccac3becba502f"
new_hash = "6289bb94661a4e008eac3f85488bce71e0d7e95811918ac3f9e83142f4f09b31"
wheel = root / "wayvia-0.1.0-py3-none-any.whl"
incoming = pathlib.Path("/root/wayvia-followup-20261020/wayvia-0.1.0-py3-none-any.whl")
changed = {"wayvia/agent/http_probe.py", "wayvia/render/__init__.py"}
role = sys.argv[1]
assert role in {"ingress", "exit"}
mode = sys.argv[2]
assert mode in {"prepare", "apply"}
locks = []
for name in ["system-operation.lock", "install.lock"]:
    fd = os.open("/var/lib/wayvia/" + name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    locks.append(fd)
assert not pathlib.Path("/var/lib/wayvia/install-transaction.json").exists()
manifest = json.loads(manifest_path.read_text())
assert sha(wheel.read_bytes()) == old_hash and sha(incoming.read_bytes()) == new_hash
with zipfile.ZipFile(wheel) as z:
    old = {n: z.read(n) for n in z.namelist()}
with zipfile.ZipFile(incoming) as z:
    new = {n: z.read(n) for n in z.namelist()}
assert old.keys() == new.keys()
assert {n for n in new if new[n] != old[n]} == changed | {"wayvia-0.1.0.dist-info/RECORD"}
for name, data in old.items():
    if name.startswith("wayvia/"):
        assert (site / name).read_bytes() == data, name
        assert (root / "source/src" / name).read_bytes() == data, name
for rel, meta in manifest["files"].items():
    path = pathlib.Path("/") / rel
    assert not path.is_symlink() and sha(path.read_bytes()) == meta["sha256"], rel
runtime = pathlib.Path("/var/lib/wayvia/agent") / (role + "-runtime.json")
gen = json.loads(runtime.read_text())["transaction_id"]
assert json.loads((runtime.parent / (gen + ".json")).read_text())["state"] == "ACTIVE"
units = (
    ["wayvia-ingress", "wayvia-bridge", "wayvia-carrier", "wayvia-vpn-client"]
    if role == "ingress"
    else ["wayvia-exit", "wayvia-vpn-server"]
)


def state():
    return {
        u: subprocess.check_output(
            [
                "systemctl",
                "show",
                u,
                "-p",
                "ActiveState",
                "-p",
                "MainPID",
                "-p",
                "ActiveEnterTimestampMonotonic",
            ],
            text=True,
        )
        .strip()
        .splitlines()
        for u in [*units, "wayvia-agent", "wayvia-monitor.timer", "nginx"]
    }


before = state()
assert all(
    "ActiveState=active" in before[u] for u in [*units, "wayvia-agent", "wayvia-monitor.timer"]
)
routes = [
    subprocess.check_output(["ip", version, "-j", "route", "show", "default"])
    for version in ["-4", "-6"]
]
changes = {site / n: new[n] for n in changed}
changes.update({root / "source/src" / n: new[n] for n in changed})
changes[wheel] = incoming.read_bytes()
record = site / "wayvia-0.1.0.dist-info/RECORD"
rows = list(csv.reader(io.StringIO(record.read_text())))
for row in rows:
    if row[0] in changed:
        row[1] = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(new[row[0]]).digest()
        ).decode().rstrip("=")
        row[2] = str(len(new[row[0]]))
assert sum(r[0] in changed for r in rows) == 2
out = io.StringIO()
csv.writer(out, lineterminator="\n").writerows(rows)
changes[record] = out.getvalue().encode()
for p, data in changes.items():
    rel = str(p).lstrip("/")
    if rel in manifest["files"]:
        manifest["files"][rel]["sha256"] = sha(data)
changes[manifest_path] = (json.dumps(manifest, indent=2) + "\n").encode()
report = {
    "role": role,
    "mode": mode,
    "old_wheel_sha256": old_hash,
    "new_wheel_sha256": new_hash,
    "independent_module_changes": sorted(changed),
    "generation_unchanged": gen,
    "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
if mode == "prepare":
    report["preflight"] = "all_source_package_manifest_and_dataplane_checks_passed"
    print(json.dumps(report, indent=2))
    sys.exit(0)
backup = pathlib.Path("/root/wayvia-followup-20261020/backup")
backup.mkdir(mode=0o700, exist_ok=False)
previous = {p: p.read_bytes() for p in changes}
for p, data in previous.items():
    dest = backup / str(p).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def write_atomic(path, data):
    st = path.stat()
    fd, tmp = tempfile.mkstemp(prefix=".wayvia-followup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), st.st_mode & 0o777)
            os.fchown(f.fileno(), st.st_uid, st.st_gid)
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


try:
    for p, data in changes.items():
        write_atomic(p, data)
    for name in changed:
        p = site / name
        for pyc in (p.parent / "__pycache__").glob(p.stem + ".*.pyc"):
            pyc.unlink()
    subprocess.run(
        ["runuser", "-u", "wayvia", "--", str(root / ".venv/bin/wayvia"), "--version"],
        check=True,
        capture_output=True,
    )
    # The API holds the old renderer in memory. Dataplane and monitor remain running.
    subprocess.run(["systemctl", "restart", "wayvia-api.service"], check=True, capture_output=True)
    for _ in range(40):
        r = subprocess.run(
            [
                "curl",
                "--disable",
                "--silent",
                "--fail",
                "--max-time",
                "1",
                "http://127.0.0.1:8787/healthz",
            ],
            capture_output=True,
        )
        if r.returncode == 0:
            break
        time.sleep(0.25)
    assert r.returncode == 0, "API health check failed"
    for name, data in new.items():
        if name.startswith("wayvia/"):
            assert (site / name).read_bytes() == data and (
                root / "source/src" / name
            ).read_bytes() == data
    after = state()
    assert before == after, "dataplane unit identity changed during maintenance"
    assert routes == [
        subprocess.check_output(["ip", version, "-j", "route", "show", "default"])
        for version in ["-4", "-6"]
    ]
    assert json.loads(runtime.read_text())["transaction_id"] == gen
    report.update(
        completed_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        API_health=200,
        dataplane_agent_monitor_and_nginx_unchanged=True,
        default_routes_unchanged=True,
        all_installed_sources_match_wheel=True,
        service_identity_CLI=True,
        only_API_reloaded=True,
    )
except BaseException:
    for p, data in previous.items():
        write_atomic(p, data)
    subprocess.run(["systemctl", "restart", "wayvia-api.service"], capture_output=True)
    raise
(backup.parent / "deployment-result.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
