import hashlib
import json

import pytest

from schedule_guard.sources.base import UNKEYED, decode_json, unkeyed


@pytest.mark.parametrize(
    "text",
    ['{"a": 1, "b": [true, null, 2.5]}', '"text"', "[]", '{"nested": {"a": {"b": 1}}}'],
)
def test_decode_json_reads_strict_json_like_json_loads(text):
    assert decode_json(text) == json.loads(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"remaining_work": NaN}', "NaN is not JSON"),
        ('{"remaining_work": Infinity}', "Infinity is not JSON"),
        ('{"remaining_work": -Infinity}', "-Infinity is not JSON"),
        ('{"task_uid": 3, "task_uid": 4}', "the key 'task_uid' appears twice"),
        ('{"fields": {"notes": "a", "notes": "b"}}', "the key 'notes' appears twice"),
        ("[" * 100_000, "nested too deeply"),
        ("{'single': 'quotes'}", "Expecting property name"),
    ],
    ids=["nan", "infinity", "minus-infinity", "repeated-key", "repeated-nested-key", "too-deep", "not-json"],
)
def test_decode_json_refuses_what_json_loads_would_let_through_or_fail_on(text, message):
    with pytest.raises(ValueError, match=message):
        decode_json(text)


def test_an_unkeyed_item_is_keyed_by_the_sha256_of_its_bytes():
    item = unkeyed(b"not json \x9b", "not json \\x9b", "the line is not UTF-8")
    assert item.source_item_id == UNKEYED + hashlib.sha256(b"not json \x9b").hexdigest()
    assert (item.item_version, item.raw, item.payload, item.problem) == ("", "not json \\x9b", None, "the line is not UTF-8")
    assert unkeyed(b"not json \x9b", "", "").source_item_id == item.source_item_id  # same bytes, same key
