"""Private, bounded files and explicit policy changes."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from wayvia.observability.models import LogError, Policy, canonical
from wayvia.observation_run import private
from wayvia.telemetry.store import secure_path


def directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise LogError("LOG_UNSAFE_PATH")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    private(path, directory=True)
    return path


def read_json(path, limit=65536):
    path = secure_path(path)
    if path.stat().st_size > limit:
        raise LogError("LOG_FILE_TOO_LARGE")
    return json.loads(path.read_bytes())


def write_json(path, value):
    path = secure_path(path)
    fd, name = tempfile.mkstemp(prefix=".log-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(canonical(value) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        with open_directory(path.parent) as parent:
            os.fsync(parent)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def open_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def locked(path, *, nonblocking=False):
    path = secure_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def policy(root):
    return Policy.model_validate(read_json(Path(root) / "policy.json"))


def initialize(root):
    root = directory(root)
    with locked(root / "policy.lock"):
        if not (root / "policy.json").exists():
            write_json(root / "policy.json", Policy().model_dump())
    return policy(root)


def update_policy(root, changes, expected_revision):
    with locked(Path(root) / "policy.lock"):
        before = policy(root)
        if before.revision != expected_revision or set(changes) & {"version", "revision"}:
            raise LogError("LOG_POLICY_CONFLICT", status=409)
        after = Policy.model_validate(
            before.model_dump() | changes | {"revision": before.revision + 1}
        )
        write_json(Path(root) / "policy.json", after.model_dump())
        return after
