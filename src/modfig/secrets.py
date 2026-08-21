from __future__ import annotations

import os
import re
from collections.abc import Mapping

from .errors import AppError

_SECRET_REFERENCE = re.compile(r"^env\.([A-Za-z_][A-Za-z0-9_]*)$")


def secret_variable(reference: str) -> str:
    match = _SECRET_REFERENCE.fullmatch(reference)
    if match is None:
        raise AppError("apiKey must use env.VAR_NAME syntax")
    return match.group(1)


def resolve_secret(reference: str, environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    variable = secret_variable(reference)
    value = environment.get(variable)
    if not value:
        raise AppError(f"environment secret {variable!r} is missing or empty", exit_code=3)
    return value
