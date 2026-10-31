"""What a source of progress updates hands to sync.

A source reads its items and gives each one its idempotency key, its
`source_item_id` and `item_version`, which the source checks itself. It
hands the item's decoded content on untouched; the guard judges that. An
item the source can't decode, or one without a usable key, still comes
through: its key is made from its bytes (UNKEYED plus their SHA-256) and it
carries a `problem`, so sync records one `malformed` conflict for it and a
later run that reads the same bytes sees a duplicate.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

UNKEYED = "unkeyed/"  # "/" can't appear in a source_item_id a source accepts


class SourceError(Exception):
    """The source can't be read at all, so the run fails before capturing anything."""


@dataclass(frozen=True, slots=True)
class SourceItem:
    source_item_id: str
    item_version: str
    raw: str  # the item's text, stored as source_update.raw_json
    payload: object = None  # the decoded object, untouched; None when `problem` is set
    problem: str | None = None  # why sync can't hand it to the guard


class SourceAdapter(Protocol):
    name: str  # the source in idempotency keys and in notes entries, such as "file"
    spec: str  # what a sync run records as its source, such as "file:updates.jsonl"

    def read(self) -> list[SourceItem]: ...


def unkeyed(raw: bytes, text: str, problem: str) -> SourceItem:
    """An item that can't be decided, keyed by its bytes."""
    return SourceItem(f"{UNKEYED}{hashlib.sha256(raw).hexdigest()}", "", text, problem=problem)


def decode_json(text: str) -> object:
    """Strict JSON: no NaN or Infinity, and no key twice in one object."""

    def refuse_constant(name: str) -> object:
        raise ValueError(f"{name} is not JSON")

    def one_value_per_key(pairs: list[tuple[str, object]]) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"the key {key!r} appears twice")
            obj[key] = value
        return obj

    try:
        return json.loads(text, parse_constant=refuse_constant, object_pairs_hook=one_value_per_key)
    except RecursionError as exc:
        raise ValueError("nested too deeply") from exc
