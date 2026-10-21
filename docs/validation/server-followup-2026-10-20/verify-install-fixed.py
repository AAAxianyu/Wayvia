import pathlib, subprocess, os, json, importlib.util, platform, zipfile, csv, io, base64, hashlib, time, urllib.request

p = pathlib.Path("/repo/deploy/install.py")
spec = importlib.util.spec_from_file_location("install_fixed", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert pathlib.Path("/.dockerenv").exists()
old = os.umask(0o077)
try:
    user = mod.service_account()
    wheel = pathlib.Path("/repo/dist/wayvia-0.1.0-py3-none-any.whl")
    mod.install_layout(
        pathlib.Path("/"),
        component_dir=pathlib.Path("/components"),
        arch="arm64",
        wheel=wheel,
        api_uid=user.pw_uid,
        source_root=pathlib.Path("/repo"),
        system=True,
    )
    mod.install_runtime("uv", "/opt/wayvia/wayvia-0.1.0-py3-none-any.whl")
    result = {
        "scope": "disposable_Linux_normal_install_runtime_no_external_network_fresh_unprivileged_API_no_fault_injection",
        "umask": "077",
        "fresh_service_identity_version": subprocess.check_output(
            ["runuser", "-u", "wayvia", "--", "/opt/wayvia/.venv/bin/wayvia", "--version"],
            text=True,
        ).strip(),
    }
    with zipfile.ZipFile(wheel) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    entries["wayvia/__init__.py"] += b'\n_followup_same_version_marker = "rebuilt-wheel"\n'
    recname = "wayvia-0.1.0.dist-info/RECORD"
    records = list(csv.reader(io.StringIO(entries[recname].decode())))
    for r in records:
        if r[0] == "wayvia/__init__.py":
            r[1] = "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(entries[r[0]]).digest()
            ).decode().rstrip("=")
            r[2] = str(len(entries[r[0]]))
    stream = io.StringIO()
    csv.writer(stream, lineterminator="\n").writerows(records)
    entries[recname] = stream.getvalue().encode()
    rebuilt = pathlib.Path("/tmp/wayvia-0.1.0-py3-none-any.whl")
    with zipfile.ZipFile(rebuilt, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    mod.install_layout(
        pathlib.Path("/"),
        component_dir=pathlib.Path("/components"),
        arch="arm64",
        wheel=rebuilt,
        api_uid=user.pw_uid,
        source_root=pathlib.Path("/repo"),
        system=True,
    )
    mod.install_runtime("uv", "/opt/wayvia/wayvia-0.1.0-py3-none-any.whl")
    result["same_version_rebuilt_wheel_installed"] = (
        subprocess.check_output(
            [
                "runuser",
                "-u",
                "wayvia",
                "--",
                "/opt/wayvia/.venv/bin/python",
                "-c",
                "import wayvia;print(wayvia._followup_same_version_marker)",
            ],
            text=True,
        ).strip()
        == "rebuilt-wheel"
    )
    control = pathlib.Path("/var/lib/wayvia/control")
    command = [
        "runuser",
        "-u",
        "wayvia",
        "--",
        "/opt/wayvia/.venv/bin/wayvia",
        "--data-dir",
        str(control),
    ]
    subprocess.run(
        ["/opt/wayvia/.venv/bin/wayvia", "--data-dir", str(control), "init"],
        check=True,
        capture_output=True,
    )
    for path in (control, *control.rglob("*")):
        os.chown(path, user.pw_uid, user.pw_gid)
    for path in (pathlib.Path("/var/lib/wayvia"), pathlib.Path("/run/wayvia")):
        os.chown(path, 0, user.pw_gid)
    server = subprocess.Popen(
        [*command, "serve", "--port", "18787"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18787/healthz", timeout=1) as r:
                    result["fresh_service_identity_API_health"] = r.status
                break
            except OSError:
                time.sleep(0.1)
        assert result.get("fresh_service_identity_API_health") == 200
    finally:
        server.terminate()
        server.wait(timeout=10)
    paths = [
        "/opt/wayvia",
        "/opt/wayvia/.venv",
        "/opt/wayvia/.venv/lib/python3.12/site-packages/wayvia",
        "/etc/wayvia/secrets",
        "/etc/wayvia/agent.env",
        "/var/lib/wayvia/control",
    ]
    result["modes"] = {p: oct(pathlib.Path(p).stat().st_mode & 0o777) for p in paths}
    mask = os.umask(0o077)
    result["caller_umask_unchanged"] = mask == 0o077
    result.update(
        uv=subprocess.check_output(["uv", "--version"], text=True).strip(),
        python=platform.python_version(),
    )
    pathlib.Path("/evidence/install-permission-fixed.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
finally:
    os.umask(old)
