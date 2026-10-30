import sys
import time
from contextlib import contextmanager
from xml.etree import ElementTree as ET

import pytest

from schedule_guard.mspdi import parse as parse_module
from schedule_guard.mspdi.parse import (
    MAX_FILE_BYTES,
    MAX_TASKS,
    NS,
    MspdiError,
    PredecessorLink,
    parse_mspdi,
    read_mspdi_bytes,
)


def mspdi(tasks: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Project xmlns="{NS}"><Name>t.xml</Name><Title>T</Title><Tasks>{tasks}</Tasks></Project>'
    ).encode()


_recording: list[str] | None = None


def _audit(event, args):
    if _recording is not None and event == "open":
        _recording.append(str(args[0]))


sys.addaudithook(_audit)  # hooks can't be removed; this one records only inside files_opened()


@contextmanager
def files_opened():
    """Collect every path Python opens inside the block (the `open` audit event)."""
    global _recording
    _recording = paths = []
    try:
        yield paths
    finally:
        _recording = None


TASK = """
<Task>
  <UID>12</UID><ID>3</ID><Name>Pour slab &amp; cure</Name><WBS>2.4</WBS>
  <OutlineNumber>2.4</OutlineNumber><OutlineLevel>2</OutlineLevel>
  <Start>2026-07-16T08:00:00</Start><Finish>2026-07-22T17:00:00</Finish>
  <Duration>PT40H0M0S</Duration><DurationFormat>7</DurationFormat><Work>PT160H0M0S</Work>
  <Milestone>0</Milestone><Summary>0</Summary><Critical>1</Critical>
  <PercentComplete>40</PercentComplete>
  <ActualStart>2026-07-16T08:00:00</ActualStart><ActualFinish>2026-07-21T17:00:00</ActualFinish>
  <RemainingWork>PT96H0M0S</RemainingWork>
  <Notes>Pump truck booked</Notes>
  <PredecessorLink><PredecessorUID>11</PredecessorUID><Type>1</Type><LinkLag>4800</LinkLag></PredecessorLink>
  <PredecessorLink><PredecessorUID>7</PredecessorUID><Type>3</Type></PredecessorLink>
  <Baseline>
    <Number>0</Number><Start>2026-07-14T08:00:00</Start><Finish>2026-07-20T17:00:00</Finish>
    <Duration>PT40H0M0S</Duration><Work>PT160H0M0S</Work><Cost>12800.50</Cost>
  </Baseline>
  <Baseline><Number>1</Number><Start>2026-07-16T08:00:00</Start></Baseline>
</Task>
<Task><UID>13</UID></Task>
"""


def test_reads_the_task_subset():
    project = parse_mspdi(mspdi(TASK))
    assert (project.name, project.title) == ("t.xml", "T")
    task, bare = project.tasks
    assert task.uid == 12  # UID, not the row ID
    assert (task.name, task.wbs, task.outline_level) == ("Pour slab & cure", "2.4", 2)
    assert (task.is_summary, task.is_milestone) == (False, False)
    assert (task.planned_start, task.planned_finish) == ("2026-07-16T08:00:00", "2026-07-22T17:00:00")
    assert (task.planned_duration, task.planned_work) == ("PT40H0M0S", "PT160H0M0S")
    assert task.percent_complete == 40
    assert (task.actual_start, task.actual_finish) == ("2026-07-16T08:00:00", "2026-07-21T17:00:00")
    assert (task.remaining_work, task.notes) == ("PT96H0M0S", "Pump truck booked")
    assert task.predecessors == (PredecessorLink(11, 1, 4800), PredecessorLink(7, 3, None))
    b0, b1 = task.baselines
    assert (b0.number, b0.start, b0.finish) == (0, "2026-07-14T08:00:00", "2026-07-20T17:00:00")
    assert (b0.duration, b0.work, b0.cost) == ("PT40H0M0S", "PT160H0M0S", "12800.50")
    assert (b1.number, b1.start, b1.finish, b1.cost) == (1, "2026-07-16T08:00:00", None, None)

    assert bare.uid == 13
    assert bare.name is bare.planned_start is bare.percent_complete is bare.notes is None
    assert (bare.is_summary, bare.is_milestone, bare.predecessors, bare.baselines) == (False, False, (), ())


def test_baseline_bytes_are_c14n_of_the_subtree_whatever_the_prefix():
    expected = (
        f'<Baseline xmlns="{NS}"><Number>0</Number><Interim></Interim><Cost>5</Cost></Baseline>'
    ).encode()
    default_ns = mspdi("<Task><UID>1</UID><Baseline><Number>0</Number><Interim/><Cost>5</Cost></Baseline>\n</Task>")
    prefixed = (
        f'<p:Project xmlns:p="{NS}"><p:Tasks><p:Task><p:UID>1</p:UID>'
        "<p:Baseline ><p:Number>0</p:Number><p:Interim/><p:Cost>5</p:Cost></p:Baseline >"
        "</p:Task></p:Tasks></p:Project>"
    ).encode()
    for data in (default_ns, prefixed):
        (baseline,) = parse_mspdi(data).tasks[0].baselines
        assert baseline.xml_c14n == expected
        assert ET.canonicalize(xml_data=baseline.xml_c14n.decode()).encode() == expected


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE Project [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<Project xmlns="http://schemas.microsoft.com/project"><Name>&lol9;</Name></Project>
"""


def test_rejects_billion_laughs():
    with pytest.raises(MspdiError, match="DTD"):
        parse_mspdi(BILLION_LAUGHS)


def test_rejects_xxe_without_reading_the_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-MARKER")
    payload = (
        f'<?xml version="1.0"?><!DOCTYPE Project [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
        f'<Project xmlns="{NS}"><Tasks><Task><UID>1</UID><Name>&xxe;</Name></Task></Tasks></Project>'
    ).encode()
    with files_opened() as opened, pytest.raises(MspdiError, match="DTD") as excinfo:
        parse_mspdi(payload)
    assert "TOP-SECRET-MARKER" not in str(excinfo.value)
    assert str(secret) not in opened


def test_rejects_parameter_entity_xxe_without_reading_the_file(tmp_path):
    remote = tmp_path / "evil.dtd"
    remote.write_text('<!ENTITY leak "x">')
    payload = (
        f'<?xml version="1.0"?><!DOCTYPE Project [<!ENTITY % ext SYSTEM "{remote.as_uri()}"> %ext;]>'
        f'<Project xmlns="{NS}"/>'
    ).encode()
    with files_opened() as opened, pytest.raises(MspdiError, match="DTD"):
        parse_mspdi(payload)
    assert str(remote) not in opened


def test_rejects_external_dtd_without_reading_it(tmp_path):
    dtd = tmp_path / "project.dtd"
    dtd.write_text('<!ENTITY name "from-the-dtd">')
    payload = (
        f'<?xml version="1.0"?><!DOCTYPE Project SYSTEM "{dtd.as_uri()}">'
        f'<Project xmlns="{NS}"><Name>&name;</Name></Project>'
    ).encode()
    with files_opened() as opened, pytest.raises(MspdiError, match="DTD"):
        parse_mspdi(payload)
    assert str(dtd) not in opened


def test_rejects_bytes_over_the_size_limit_before_parsing():
    # Whitespace isn't XML, so this only passes if the size check comes first.
    with pytest.raises(MspdiError, match="20 MB"):
        parse_mspdi(b" " * (MAX_FILE_BYTES + 1))


class ReadRecorder:
    """A real file that records the size each read() asks for."""

    def __init__(self, f, sizes: list):
        self._f, self._sizes = f, sizes

    def read(self, size=-1):
        self._sizes.append(size)
        return self._f.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


def test_reads_a_file_up_to_the_size_limit_and_no_further(tmp_path, monkeypatch):
    at_limit, over = tmp_path / "at-limit.xml", tmp_path / "over.xml"
    with open(at_limit, "wb") as f:
        f.truncate(MAX_FILE_BYTES)
    with open(over, "wb") as f:
        f.truncate(MAX_FILE_BYTES + 1)
    sizes: list = []
    monkeypatch.setattr(parse_module, "open", lambda *a, **k: ReadRecorder(open(*a, **k), sizes), raising=False)
    assert len(read_mspdi_bytes(at_limit)) == MAX_FILE_BYTES
    with pytest.raises(MspdiError, match="20 MB"):
        read_mspdi_bytes(over)
    # An unbounded read() would pull a huge file or an endless stream into memory.
    assert sizes, "read_mspdi_bytes didn't read through open()"
    assert all(isinstance(size, int) and 0 <= size <= MAX_FILE_BYTES + 1 for size in sizes), sizes


def minimal_tasks(count: int) -> str:
    return "".join(f"<Task><UID>{uid}</UID></Task>" for uid in range(count))


def test_accepts_exactly_the_task_limit():
    assert len(parse_mspdi(mspdi(minimal_tasks(MAX_TASKS))).tasks) == MAX_TASKS


def test_many_baselines_on_one_task_parse_in_linear_time():
    # Under 2 MB, far inside the size limit. A duplicate-number check that
    # rescans every earlier Baseline takes about 20 s here; a set, under 2 s.
    count = 40_000
    baselines = "".join(f"<Baseline><Number>{number}</Number></Baseline>" for number in range(count))
    data = mspdi(f"<Task><UID>1</UID>{baselines}</Task>")
    started = time.perf_counter()
    (task,) = parse_mspdi(data).tasks
    elapsed = time.perf_counter() - started
    assert len(task.baselines) == count
    assert elapsed < 5, f"{count} Baselines took {elapsed:.1f} s"


def test_rejects_more_tasks_than_the_limit():
    with pytest.raises(MspdiError, match=f"more than {MAX_TASKS} tasks"):
        parse_mspdi(mspdi(minimal_tasks(MAX_TASKS + 1)))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(f'<Tasks xmlns="{NS}"/>'.encode(), id="wrong-root"),
        pytest.param(b'<Project xmlns="http://example.com/not-project"/>', id="wrong-namespace"),
        pytest.param(b"<Project/>", id="no-namespace"),
    ],
)
def test_rejects_wrong_root_or_namespace(data):
    with pytest.raises(MspdiError, match="root element must be Project"):
        parse_mspdi(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"not xml", "not well-formed", id="not-xml"),
        pytest.param(b'<?xml version="1.0" encoding="x-nope"?><Project/>', "encoding", id="unknown-encoding"),
        pytest.param(b'<?xml version="1.0" encoding="utf-7"?><Project/>', "encoding", id="multibyte-encoding"),
        pytest.param(
            f'<Project xmlns="{NS}"><Tasks/><Tasks/></Project>'.encode(), "more than one Tasks", id="two-tasks"
        ),
    ],
)
def test_rejects_malformed_files(data, message):
    with pytest.raises(MspdiError, match=message):
        parse_mspdi(data)


DEEP = "<x>" * 100_000 + "</x>" * 100_000

BAD_TASKS = {
    "missing-uid": ("<Task><Name>no uid</Name></Task>", "missing UID"),
    "uid-not-int": ("<Task><UID>abc</UID></Task>", "UID is not an integer"),
    "uid-full-width-digit": ("<Task><UID>\uff11</UID></Task>", "UID is not an integer"),
    "uid-negative": ("<Task><UID>-1</UID></Task>", "out of range"),
    "uid-duplicate": ("<Task><UID>1</UID></Task><Task><UID>1</UID></Task>", "duplicate task UID 1"),
    "two-names": ("<Task><UID>1</UID><Name>a</Name><Name>b</Name></Task>", "more than one Name"),
    "percent-over-100": ("<Task><UID>1</UID><PercentComplete>101</PercentComplete></Task>", "out of range"),
    "month-13": ("<Task><UID>1</UID><Start>2026-13-01T08:00:00</Start></Task>", "not a valid date-time"),
    "date-words": ("<Task><UID>1</UID><Finish>tomorrow</Finish></Task>", "not a date-time"),
    "duration-words": ("<Task><UID>1</UID><Duration>8 hours</Duration></Task>", "not a duration"),
    "duration-empty-pt": ("<Task><UID>1</UID><Work>PT</Work></Task>", "not a duration"),
    "summary-yes": ("<Task><UID>1</UID><Summary>yes</Summary></Task>", "not a boolean"),
    "link-without-uid": (
        "<Task><UID>1</UID><PredecessorLink><Type>1</Type></PredecessorLink></Task>",
        "without PredecessorUID",
    ),
    "link-type-9": (
        "<Task><UID>1</UID><PredecessorLink><PredecessorUID>2</PredecessorUID><Type>9</Type>"
        "</PredecessorLink></Task>",
        "out of range",
    ),
    "baseline-without-number": (
        "<Task><UID>1</UID><Baseline><Start>2026-06-01T08:00:00</Start></Baseline></Task>",
        "without Number",
    ),
    "baseline-twice": (
        "<Task><UID>1</UID><Baseline><Number>0</Number></Baseline><Baseline><Number>0</Number></Baseline></Task>",
        "more than one Baseline 0",
    ),
    "baseline-cost-words": (
        "<Task><UID>1</UID><Baseline><Number>0</Number><Cost>lots</Cost></Baseline></Task>",
        "not a decimal",
    ),
    "baseline-unnamespaced-child": (
        '<Task><UID>1</UID><Baseline><Number>0</Number><y xmlns=""/></Baseline></Task>',
        "outside any namespace",
    ),
    "baseline-nested-too-deep": (
        f"<Task><UID>1</UID><Baseline><Number>0</Number>{DEEP}</Baseline></Task>",
        "nested too deeply",
    ),
    # A field with child elements has no single text value, so it is refused, not read in part.
    "uid-with-child": ("<Task><UID>1<x/>2</UID></Task>", "UID has child elements"),
    "percent-with-child": (
        "<Task><UID>1</UID><PercentComplete>4<x/>0</PercentComplete></Task>",
        "PercentComplete has child elements",
    ),
    "summary-with-child": ("<Task><UID>1</UID><Summary>1<x/></Summary></Task>", "Summary has child elements"),
    "name-with-child": ("<Task><UID>1</UID><Name>Pour<b>EVIL</b> slab</Name></Task>", "Name has child elements"),
    "wbs-with-child": ("<Task><UID>1</UID><WBS>1.2<x/>.3</WBS></Task>", "WBS has child elements"),
    "notes-with-child": ("<Task><UID>1</UID><Notes>a<x/>b</Notes></Task>", "Notes has child elements"),
    "link-uid-with-child": (
        "<Task><UID>1</UID><PredecessorLink><PredecessorUID>2<x/></PredecessorUID></PredecessorLink></Task>",
        "PredecessorUID has child elements",
    ),
    "baseline-start-with-child": (
        "<Task><UID>1</UID><Baseline><Number>0</Number><Start>2026-06-01T08:00:00<x/></Start></Baseline></Task>",
        "Start has child elements",
    ),
    # Only XML whitespace (space, tab, CR, LF) may surround a typed value.
    "uid-no-break-spaces": ("<Task><UID>\u00a012\u00a0</UID></Task>", "UID is not an integer"),
    "summary-em-space": ("<Task><UID>1</UID><Summary>\u20031</Summary></Task>", "Summary is not a boolean"),
}


@pytest.mark.parametrize(("tasks", "message"), BAD_TASKS.values(), ids=BAD_TASKS.keys())
def test_rejects_bad_task_fields(tasks, message):
    with pytest.raises(MspdiError, match=message):
        parse_mspdi(mspdi(tasks))


def test_rejects_project_title_with_child_elements():
    data = f'<Project xmlns="{NS}"><Title>River<x/>bend</Title></Project>'.encode()
    with pytest.raises(MspdiError, match="Title has child elements"):
        parse_mspdi(data)


def test_accepts_xml_whitespace_around_typed_values():
    (task,) = parse_mspdi(mspdi("<Task><UID>\t 12 \n</UID><PercentComplete> 40\r\n</PercentComplete></Task>")).tasks
    assert (task.uid, task.percent_complete) == (12, 40)
