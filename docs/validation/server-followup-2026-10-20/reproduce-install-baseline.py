import pathlib, subprocess, os, json, importlib.util, platform

p = pathlib.Path("/repo/deploy/install.py")
spec = importlib.util.spec_from_file_location("install_baseline", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
root = pathlib.Path("/tmp/wayvia-permission-baseline")
(root / "opt").mkdir(parents=True, mode=0o755)
os.chmod(root, 0o755)
old = os.umask(0o077)
try:
    result = mod.prepare(
        root,
        component_dir=pathlib.Path("/components"),
        arch="arm64",
        wheel=pathlib.Path("/repo/dist/wayvia-0.1.0-py3-none-any.whl"),
        api_uid=65534,
        source_root=pathlib.Path("/repo"),
    )
    env = os.environ.copy()
    env.update(
        UV_PROJECT_ENVIRONMENT=str(root / "opt/wayvia/.venv"),
        UV_PYTHON_DOWNLOADS="never",
        UV_OFFLINE="true",
    )
    for argv in [
        [
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--no-install-project",
            "--project",
            "/repo",
            "--python",
            "python3.12",
        ],
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--python",
            str(root / "opt/wayvia/.venv/bin/python"),
            "/repo/dist/wayvia-0.1.0-py3-none-any.whl",
        ],
    ]:
        subprocess.run(argv, env=env, capture_output=True, text=True, check=True)
    tested = subprocess.run(
        ["runuser", "-u", "nobody", "--", str(root / "opt/wayvia/.venv/bin/wayvia"), "--version"],
        capture_output=True,
        text=True,
    )
    result = {
        "scope": "disposable_Linux_no_network_no_services_no_fault_injection",
        "umask": "077",
        "before_fix_service_identity_exit": tested.returncode,
        "before_fix_permission_denied": "Permission denied" in tested.stderr,
        "modes": {
            str(p.relative_to(root)): oct(p.stat().st_mode & 0o777)
            for p in [
                root / "opt/wayvia",
                root / "opt/wayvia/.venv",
                root / "opt/wayvia/.venv/lib/python3.12/site-packages/wayvia",
            ]
        },
        "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
        "python": platform.python_version(),
    }
    pathlib.Path("/evidence/install-permission-baseline.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
finally:
    os.umask(old)
