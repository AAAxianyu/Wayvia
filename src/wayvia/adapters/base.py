"""Bounded, shell-free component commands. Never expose command output as logs."""

import subprocess
from dataclasses import dataclass

from wayvia.errors import ExitCode, WayviaError


class AdapterError(WayviaError):
    def __init__(self, code, message, *, details=None):
        super().__init__(code, message, details=details, exit_code=ExitCode.RUNTIME)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    def observe(self, argv, *, marker, timeout=8):
        """Observe a long-running validator, then reap it without leaving a daemon."""
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=False
            )
        except FileNotFoundError as exc:
            raise AdapterError(
                "COMPONENT_MISSING",
                "Required component is not installed",
                details={"component": argv[0]},
            ) from exc
        try:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=2)
            if marker not in output:
                raise AdapterError(
                    "VALIDATION_INCOMPLETE",
                    "Required handshake evidence was not observed",
                    details={"component": argv[0]},
                )
            return CommandResult(0, marker)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def run(self, argv: list[str], *, timeout=20, input_text=None, check=True):
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise AdapterError(
                "COMPONENT_MISSING",
                "Required component is not installed",
                details={"component": argv[0]},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(
                "COMMAND_TIMEOUT",
                "Component command exceeded its deadline",
                details={"component": argv[0], "timeout": timeout},
            ) from exc
        if check and result.returncode:
            raise AdapterError(
                "COMMAND_FAILED",
                "Component command failed",
                details={"component": argv[0], "returncode": result.returncode},
            )
        return CommandResult(result.returncode, result.stdout, result.stderr)


def require_real(adapter, mode="production"):
    if getattr(adapter, "simulated", False) and mode not in {"test", "demo"}:
        raise AdapterError("FAKE_FORBIDDEN", "Production execution refuses simulated adapters")
