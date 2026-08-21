from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppError(Exception):
    """An expected, user-facing failure with a prescribed process exit code."""

    message: str
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message
