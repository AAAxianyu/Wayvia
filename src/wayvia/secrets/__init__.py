"""Restricted, role-local file secrets with race-safe reads."""

import os
import re
import stat
from pathlib import Path

from wayvia.errors import ExitCode, WayviaError


class SecretStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WayviaError(
                "secret_permissions",
                "Secret directory must be owned and mode 0700",
                exit_code=ExitCode.AUTH,
            )

    def _name(self, ref: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", ref):
            raise WayviaError("secret_reference", "Invalid secret reference")
        return ref

    def read(self, ref: str) -> str:
        name = self._name(ref)
        dfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise WayviaError(
                        "secret_permissions",
                        "Secret must be owned and mode 0600",
                        exit_code=ExitCode.AUTH,
                    )
                with os.fdopen(fd, "r", closefd=False) as stream:
                    return stream.read(1024 * 1024)
            finally:
                os.close(fd)
        except OSError as exc:
            raise WayviaError(
                "secret_unavailable", "Secret reference unavailable", exit_code=ExitCode.AUTH
            ) from exc
        finally:
            os.close(dfd)

    def write(self, ref: str, value: str) -> None:
        name = self._name(ref)
        # Random, immutable references are created only once; no symlink overwrite.
        dfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd
            )
            try:
                with os.fdopen(fd, "w", closefd=False) as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(dfd)
        finally:
            os.close(dfd)
