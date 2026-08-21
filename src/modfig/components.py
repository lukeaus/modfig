from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

_LOGICAL_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ExtensionComponent:
    name: str

    def __post_init__(self) -> None:
        if self.name == "core" or not _LOGICAL_ID_RE.fullmatch(self.name):
            raise ValueError("extension name must be a logical ID other than 'core'")


Component: TypeAlias = Literal["core"] | ExtensionComponent
