"""The Phase 1 source: a JSON Lines file of progress updates (PLAN.md section 6).

One JSON object per line, with `source_item_id`, `item_version`,
`task_uid`, `made_against_version`, `fields`, `submitted_at` and
`submitted_by`. Every run reads the whole file, so lines already decided
come back as duplicates. The file is untrusted: it is read up to
MAX_FILE_BYTES, and a line that isn't UTF-8 JSON, or has no usable key,
becomes an item with a problem instead of stopping the run. Blank lines are
skipped.

A usable key is a `source_item_id` of 1 to 200 letters, digits and `. _ :
-`, unique across every file one database reads, and an `item_version` that
is an integer from 0.
"""

import re
from pathlib import Path

from schedule_guard.sources.base import SourceError, SourceItem, decode_json, unkeyed

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ITEM_VERSION = 10**18 - 1
_ITEM_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_NO_KEY = (
    "the line has no usable key: source_item_id must be 1 to 200 letters, digits and . _ : -,"
    " and item_version an integer from 0"
)


class FileSource:
    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.spec = f"file:{path}"

    def read(self) -> list[SourceItem]:
        try:
            with open(self.path, "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)  # never more, whatever the file is
        except OSError as exc:
            raise SourceError(f"can't read the update file {self.path}: {exc.strerror}") from exc
        if len(data) > MAX_FILE_BYTES:
            raise SourceError(f"the update file is over the {MAX_FILE_BYTES // 2**20} MB limit")
        items = []
        for line in data.split(b"\n"):
            line = line.removesuffix(b"\r")
            if line.strip(b" \t"):
                items.append(_item(line))
        return items


def _item(line: bytes) -> SourceItem:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return unkeyed(line, line.decode("utf-8", "backslashreplace"), "the line is not UTF-8")
    try:
        payload = decode_json(text)
    except ValueError as exc:
        return unkeyed(line, text, f"the line is not JSON: {exc}")
    item_id = payload.get("source_item_id") if isinstance(payload, dict) else None
    version = payload.get("item_version") if isinstance(payload, dict) else None
    if not (isinstance(item_id, str) and _ITEM_ID.fullmatch(item_id)):
        return unkeyed(line, text, _NO_KEY)
    if not (isinstance(version, int) and not isinstance(version, bool) and 0 <= version <= MAX_ITEM_VERSION):
        return unkeyed(line, text, _NO_KEY)
    return SourceItem(item_id, str(version), text, payload)
