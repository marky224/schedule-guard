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
import re
from dataclasses import dataclass
from typing import Protocol

UNKEYED = "unkeyed/"  # "/" can't appear in a source_item_id a source accepts
_SURROGATE = re.compile(r"[\ud800-\udfff]")  # json.loads joins a valid pair into one character, so any left is lone


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
    """Strict JSON: no NaN or Infinity, no key twice in one object, and no lone surrogate in any string.

    A lone surrogate escape such as "\\ud800" decodes to a str that isn't
    Unicode text: SQLite can't store it as UTF-8, so a decoded item never
    holds one, in a value or a key at any depth.
    """

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
        value = json.loads(text, parse_constant=refuse_constant, object_pairs_hook=one_value_per_key)
    except RecursionError as exc:
        raise ValueError("nested too deeply") from exc
    pending = [value]  # a loop, not recursion: the value may be nested nearly as deep as json.loads goes
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and (surrogate := _SURROGATE.search(item)):
            raise ValueError(f"a string holds a lone surrogate, U+{ord(surrogate.group()):04X}")
    return value
