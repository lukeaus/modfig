from __future__ import annotations

import pytest

from modfig.state import CollisionError, reconcile


def test_reconcile_preserves_foreign_entries_updates_owned_and_removes_stale() -> None:
    existing = [
        {"id": "foreign", "value": "preserve"},
        {"id": "owned-current", "value": "old"},
        {"id": "owned-stale", "value": "remove"},
    ]
    generated = [
        {"id": "owned-current", "value": "new"},
        {"id": "owned-new", "value": "add"},
    ]

    result = reconcile(
        existing, generated, {"owned-current", "owned-stale"}, lambda item: item["id"]
    )

    assert result == (
        {"id": "foreign", "value": "preserve"},
        {"id": "owned-current", "value": "new"},
        {"id": "owned-new", "value": "add"},
    )


def test_reconcile_adopts_exact_unowned_overlap() -> None:
    item = {"id": "same", "value": "same"}

    assert reconcile([item], [item], set(), lambda entry: entry["id"]) == (item,)


def test_reconcile_rejects_divergent_unowned_overlap() -> None:
    with pytest.raises(CollisionError, match="conflicts with an unowned entry"):
        reconcile(
            [{"id": "same", "value": "foreign"}],
            [{"id": "same", "value": "generated"}],
            set(),
            lambda entry: entry["id"],
        )
