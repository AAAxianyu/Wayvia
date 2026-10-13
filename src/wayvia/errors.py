"""Stable public error codes; never include sensitive inputs in error details."""

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    INPUT = 2
    AUTH = 3
    ENVIRONMENT = 4
    RUNTIME = 5
    AWAITING_CONFIRM = 6


class WayviaError(Exception):
    def __init__(self, code: str, message: str, *, details=None, exit_code=ExitCode.INPUT):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self, request_id=None):
        return {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
            "details": self.details,
        }
