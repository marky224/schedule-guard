import hashlib
import json

import pytest

from schedule_guard.sources.base import UNKEYED, SourceError, SourceItem, decode_json, unkeyed
from schedule_guard.sources.file import MAX_FILE_BYTES, FileSource


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


def line(**changes) -> dict:
    item = {
        "source_item_id": "u-001", "item_version": 1, "task_uid": 3, "made_against_version": 0,
        "fields": {"percent_complete": 25}, "submitted_at": "2026-10-26T16:00:00-05:00",
        "submitted_by": "Site lead (demo)",
    }
    item.update(changes)
    return item


def read(tmp_path, content: bytes) -> list[SourceItem]:
    path = tmp_path / "updates.jsonl"
    path.write_bytes(content)
    return FileSource(path).read()


def test_each_line_is_an_item_keyed_by_its_source_item_id_and_version(tmp_path):
    first, second = line(), line(source_item_id="u-002", item_version=0, fields={"notes": "Fence up"})
    content = (json.dumps(first) + "\n\n  \n" + json.dumps(second) + "\r\n").encode()
    items = read(tmp_path, content)
    assert items == [
        SourceItem("u-001", "1", json.dumps(first), first),
        SourceItem("u-002", "0", json.dumps(second), second),  # the CR of a CRLF line is not part of it
    ]


def test_the_payload_is_the_decoded_line_untouched(tmp_path):
    odd = {**line(), "task_uid": "3", "extra": [1, {"x": None}], "fields": {"planned_finish": "2027-01-01T17:00:00"}}
    (item,) = read(tmp_path, json.dumps(odd).encode())
    assert item.payload == odd
    assert item.problem is None


UNUSABLE = {
    "not-json": (b"this is not json", "the line is not JSON"),
    "not-utf-8": (b'{"source_item_id": "u-1\x9b"}', "the line is not UTF-8"),
    "nan": (json.dumps(line()).replace("25", "NaN").encode(), "NaN is not JSON"),
    "repeated-key": (b'{"source_item_id": "a", "source_item_id": "b", "item_version": 1}', "appears twice"),
    "not-an-object": (b"[1, 2]", "no usable key"),
    "no-item-id": (json.dumps({k: v for k, v in line().items() if k != "source_item_id"}).encode(), "no usable key"),
    "no-version": (json.dumps({k: v for k, v in line().items() if k != "item_version"}).encode(), "no usable key"),
    "id-with-slash": (json.dumps(line(source_item_id="unkeyed/abc")).encode(), "no usable key"),
    "id-with-space": (json.dumps(line(source_item_id="u 1")).encode(), "no usable key"),
    "id-empty": (json.dumps(line(source_item_id="")).encode(), "no usable key"),
    "id-too-long": (json.dumps(line(source_item_id="u" * 201)).encode(), "no usable key"),
    "id-number": (json.dumps(line(source_item_id=1)).encode(), "no usable key"),
    "version-text": (json.dumps(line(item_version="1")).encode(), "no usable key"),
    "version-bool": (json.dumps(line(item_version=True)).encode(), "no usable key"),
    "version-negative": (json.dumps(line(item_version=-1)).encode(), "no usable key"),
    "version-huge": (json.dumps(line(item_version=10**18)).encode(), "no usable key"),
}


@pytest.mark.parametrize(("content", "problem"), UNUSABLE.values(), ids=UNUSABLE.keys())
def test_a_line_without_a_usable_key_comes_through_unkeyed_with_its_problem(tmp_path, content, problem):
    (item,) = read(tmp_path, content)
    assert item.source_item_id == UNKEYED + hashlib.sha256(content).hexdigest()
    assert (item.item_version, item.payload) == ("", None)
    assert problem in item.problem


def test_the_widest_usable_key(tmp_path):
    widest = line(source_item_id="A.z_0:9-" + "x" * 192, item_version=10**18 - 1)
    (item,) = read(tmp_path, json.dumps(widest).encode())
    assert (item.source_item_id, item.item_version, item.problem) == (widest["source_item_id"], str(10**18 - 1), None)


def test_a_line_that_is_not_utf_8_keeps_its_bytes_readable(tmp_path):
    (item,) = read(tmp_path, b"crew \x9b note")
    assert item.raw == "crew \\x9b note"


def test_a_missing_file_is_a_source_error(tmp_path):
    with pytest.raises(SourceError, match="can't read the update file"):
        FileSource(tmp_path / "missing.jsonl").read()


def test_an_oversize_file_is_a_source_error_read_no_further(tmp_path):
    path = tmp_path / "huge.jsonl"
    with open(path, "wb") as f:
        f.truncate(10 * MAX_FILE_BYTES)  # sparse, so it takes no disk space
    with pytest.raises(SourceError, match="over the 20 MB limit"):
        FileSource(path).read()
