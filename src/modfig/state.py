from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K", bound=object)


@dataclass
class CollisionError(ValueError):
    key: object

    def __str__(self) -> str:
        return f"generated key {self.key!r} conflicts with an unowned entry"


def reconcile(
    existing: Sequence[T],
    generated: Sequence[T],
    owned_keys: set[K] | frozenset[K],
    key: Callable[[T], K],
) -> tuple[T, ...]:
    """Reconcile generated target records without replacing foreign records."""
    generated_by_key: dict[K, T] = {}
    for item in generated:
        item_key = key(item)
        if item_key in generated_by_key:
            raise ValueError(f"generated entries contain duplicate key {item_key!r}")
        generated_by_key[item_key] = item

    result: list[T] = []
    emitted: set[K] = set()
    for current in existing:
        current_key = key(current)
        replacement = generated_by_key.get(current_key)
        if replacement is None:
            if current_key not in owned_keys:
                result.append(current)
            continue
        if current_key not in owned_keys and current != replacement:
            raise CollisionError(current_key)
        result.append(replacement)
        emitted.add(current_key)

    result.extend(item for item in generated if key(item) not in emitted)
    return tuple(result)
