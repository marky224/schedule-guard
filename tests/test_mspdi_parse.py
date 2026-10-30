import sys
import time
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from schedule_guard.mspdi import parse as parse_module
from schedule_guard.mspdi.parse import (
    MAX_FILE_BYTES,
    MAX_NESTING,
    MAX_TASKS,
    NS,
    MspdiError,
    PredecessorLink,
    parse_mspdi,
    read_mspdi_bytes,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "riverbend-clinic.xml"


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


def test_plan_bytes_are_c14n_of_the_master_schedule_elements():
    task, bare = parse_mspdi(mspdi(TASK)).tasks
    assert task.plan_c14n == (
        f'<Task xmlns="{NS}"><Name>Pour slab &amp; cure</Name><WBS>2.4</WBS><OutlineLevel>2</OutlineLevel>'
        "<Start>2026-07-16T08:00:00</Start><Finish>2026-07-22T17:00:00</Finish>"
        "<Duration>PT40H0M0S</Duration><Work>PT160H0M0S</Work><Milestone>0</Milestone><Summary>0</Summary>"
        "<PredecessorLink><PredecessorUID>11</PredecessorUID><Type>1</Type><LinkLag>4800</LinkLag></PredecessorLink>"
        "<PredecessorLink><PredecessorUID>7</PredecessorUID><Type>3</Type></PredecessorLink></Task>"
    ).encode()
    assert bare.plan_c14n == f'<Task xmlns="{NS}"></Task>'.encode()
    prefixed = (
        f'<p:Project xmlns:p="{NS}"><p:Tasks><p:Task><p:UID>1</p:UID><p:Name>a</p:Name>\n'
        "<p:PercentComplete>5</p:PercentComplete><p:Finish >2026-07-22T17:00:00</p:Finish>"
        "</p:Task></p:Tasks></p:Project>"
    ).encode()
    (task,) = parse_mspdi(prefixed).tasks
    assert task.plan_c14n == f'<Task xmlns="{NS}"><Name>a</Name><Finish>2026-07-22T17:00:00</Finish></Task>'.encode()


PLAN_BASE = (
    "<Task><UID>1</UID><Name>Frame walls</Name><Finish>2026-07-22T17:00:00</Finish>"
    "<PercentComplete>10</PercentComplete><Notes>n</Notes>"
    "<Baseline><Number>0</Number><Finish>2026-07-20T17:00:00</Finish></Baseline></Task>"
)


@pytest.mark.parametrize(
    ("old", "new", "planned"),
    [
        pytest.param("<PercentComplete>10<", "<PercentComplete>90<", False, id="percent-complete"),
        pytest.param("<Notes>n<", "<Notes>other<", False, id="notes"),
        pytest.param("<Number>0</Number><Finish>2026-07-20", "<Number>0</Number><Finish>2026-07-27", False,
                     id="baseline-finish"),
        pytest.param("</Notes>", "</Notes><ActualStart>2026-07-01T08:00:00</ActualStart>", False, id="actual-start"),
        pytest.param("<Finish>2026-07-22", "<Finish>2026-07-29", True, id="finish"),
        pytest.param("<Name>Frame walls<", "<Name>Frame wall<", True, id="name"),
        pytest.param("</Notes>", "</Notes><PredecessorLink><PredecessorUID>2</PredecessorUID></PredecessorLink>", True,
                     id="predecessor"),
    ],
)
def test_plan_bytes_change_with_master_schedule_fields_only(old, new, planned):
    assert PLAN_BASE.count(old) == 1
    (before,) = parse_mspdi(mspdi(PLAN_BASE)).tasks
    (after,) = parse_mspdi(mspdi(PLAN_BASE.replace(old, new))).tasks
    assert (before.plan_c14n != after.plan_c14n) is planned


def test_accepts_other_namespaces_outside_the_bytes_it_canonicalizes():
    # Only Baselines and the master-schedule elements go through C14N; the
    # rest of a task is kept as it is in the stored raw bytes.
    data = mspdi('<Task><UID>1</UID><a:Extra xmlns:a="urn:x" a:b="1"/><Name>n</Name></Task>')
    (task,) = parse_mspdi(data).tasks
    assert task.plan_c14n == f'<Task xmlns="{NS}"><Name>n</Name></Task>'.encode()


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
    """A real file that records, per read(), the size asked for and the bytes returned."""

    def __init__(self, f, reads: list):
        self._f, self._reads = f, reads

    def read(self, size=-1):
        chunk = self._f.read(size)
        self._reads.append((size, len(chunk)))
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


def test_reads_a_file_up_to_the_size_limit_and_no_further(tmp_path, monkeypatch):
    at_limit, over = tmp_path / "at-limit.xml", tmp_path / "over.xml"
    with open(at_limit, "wb") as f:
        f.truncate(MAX_FILE_BYTES)
    with open(over, "wb") as f:
        f.truncate(10 * MAX_FILE_BYTES)  # sparse, so it takes no disk space
    opens: list[list] = []

    def recording_open(*args, **kwargs):
        opens.append(reads := [])
        return ReadRecorder(open(*args, **kwargs), reads)

    monkeypatch.setattr(parse_module, "open", recording_open, raising=False)
    assert len(read_mspdi_bytes(at_limit)) == MAX_FILE_BYTES
    with pytest.raises(MspdiError, match="20 MB"):
        read_mspdi_bytes(over)
    # An unbounded read(), or a loop of reads until EOF, would pull a huge file
    # or an endless stream into memory, so count what each open returned.
    assert opens and all(opens), "read_mspdi_bytes didn't read through open()"
    for reads in opens:
        assert all(isinstance(size, int) and 0 <= size <= MAX_FILE_BYTES + 1 for size, _ in reads), reads
        assert sum(length for _, length in reads) <= MAX_FILE_BYTES + 1, reads


def minimal_tasks(count: int) -> str:
    return "".join(f"<Task><UID>{uid}</UID></Task>" for uid in range(count))


def test_accepts_exactly_the_task_limit():
    assert len(parse_mspdi(mspdi(minimal_tasks(MAX_TASKS))).tasks) == MAX_TASKS


def test_many_baselines_on_one_task_stop_at_number_11():
    # Under 2 MB, far inside the size limit. Numbers stop at 10 and repeats
    # are refused, so at most 11 Baselines per task are read before refusing.
    count = 40_000
    baselines = "".join(f"<Baseline><Number>{number}</Number></Baseline>" for number in range(count))
    data = mspdi(f"<Task><UID>1</UID>{baselines}</Task>")
    started = time.perf_counter()
    with pytest.raises(MspdiError, match="Baseline: Number 11 is out of range"):
        parse_mspdi(data)
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"{count} Baselines took {elapsed:.1f} s"


def test_accepts_baseline_numbers_0_to_10():
    baselines = "".join(f"<Baseline><Number>{number}</Number></Baseline>" for number in range(11))
    (task,) = parse_mspdi(mspdi(f"<Task><UID>1</UID>{baselines}</Task>")).tasks
    assert [b.number for b in task.baselines] == list(range(11))


def namespaced_attributes(count: int) -> str:
    return " ".join(f'a{i}:x="1" xmlns:a{i}="urn:{i}"' for i in range(count))


def namespaced_elements(count: int) -> str:
    return "".join(f'<y xmlns="urn:{i}"/>' for i in range(count))


def in_baseline(body: str) -> str:
    return f"<Task><UID>1</UID><Baseline><Number>0</Number>{body}</Baseline></Task>"


def in_predecessor_link(body: str) -> str:
    return f"<Task><UID>1</UID><PredecessorLink><PredecessorUID>2</PredecessorUID>{body}</PredecessorLink></Task>"


# Each about 0.5 MB, far inside the size limit: 16,000 attributes on one
# Start, or 24,000 sibling elements, each in its own namespace.
MANY_NAMESPACES = {
    "baseline-attributes": in_baseline(f"<Start {namespaced_attributes(16_000)}>2026-06-01T08:00:00</Start>"),
    "baseline-elements": in_baseline(namespaced_elements(24_000)),
    "plan-attributes": f"<Task><UID>1</UID><Start {namespaced_attributes(16_000)}>2026-06-01T08:00:00</Start></Task>",
    "plan-elements": in_predecessor_link(namespaced_elements(24_000)),
}


@pytest.mark.parametrize("tasks", MANY_NAMESPACES.values(), ids=MANY_NAMESPACES.keys())
def test_many_namespaces_parse_in_linear_time(tasks):
    # The stdlib C14N writer searches its namespace list once per name, so
    # passing one of these to it takes 10 to 13 s here.
    data = mspdi(tasks)
    started = time.perf_counter()
    try:
        parse_mspdi(data)
    except MspdiError:
        pass  # refusing the file is fine; this test checks how long it takes
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"took {elapsed:.1f} s"


def declared_namespaces(xml_text: str) -> list[tuple[str, str]]:
    parser = ET.XMLPullParser(events=("start-ns",))
    parser.feed(xml_text)
    return [ns for _, ns in parser.read_events()]


@pytest.fixture
def c14n_inputs(monkeypatch) -> list[str]:
    """Every text the parser hands to ET.canonicalize."""
    inputs: list[str] = []
    canonicalize = ET.canonicalize

    def spy(xml_data=None, **kwargs):
        inputs.append(xml_data)
        return canonicalize(xml_data=xml_data, **kwargs)

    monkeypatch.setattr(ET, "canonicalize", spy)
    return inputs


def test_c14n_of_the_fixture_sees_only_the_mspdi_namespace(c14n_inputs):
    parse_mspdi(read_mspdi_bytes(FIXTURE))
    assert len(c14n_inputs) == 80  # each task's Baseline 0 and its planned elements
    assert all(declared_namespaces(text) == [("", NS)] for text in c14n_inputs)


FEW_NAMESPACES = {
    "baseline-attributes": in_baseline(f"<Start {namespaced_attributes(2)}>2026-06-01T08:00:00</Start>"),
    "baseline-elements": in_baseline(namespaced_elements(2)),
    "plan-attributes": f"<Task><UID>1</UID><Name {namespaced_attributes(2)}>n</Name></Task>",
    "plan-elements": in_predecessor_link(namespaced_elements(2)),
}


@pytest.mark.parametrize("tasks", FEW_NAMESPACES.values(), ids=FEW_NAMESPACES.keys())
def test_c14n_never_sees_a_namespace_besides_mspdi(tasks, c14n_inputs):
    # A bound by counting, not timing: the C14N writer's search costs one step
    # per declared namespace, and only the MSPDI one ever reaches it.
    try:
        parse_mspdi(mspdi(tasks))
    except MspdiError:
        pass
    namespaces = [declared_namespaces(text) for text in c14n_inputs]
    assert all(ns == [("", NS)] for ns in namespaces), namespaces


def nested(depth: int, siblings: int) -> str:
    return "<x>" * depth + "<y/>" * siblings + "</x>" * depth


# About 1.6 MB: 400,000 empty elements under 900 levels of nesting.
DEEP_AND_WIDE = {
    "baseline": in_baseline(nested(900, 400_000)),
    "plan": in_predecessor_link(nested(900, 400_000)),
}


@pytest.mark.parametrize("tasks", DEEP_AND_WIDE.values(), ids=DEEP_AND_WIDE.keys())
def test_deep_nesting_parses_in_linear_time(tasks):
    # The stdlib C14N writer walks one stack entry per level for each name,
    # so passing this to it takes about 10 s here, against 2 s unnested.
    data = mspdi(tasks)
    started = time.perf_counter()
    try:
        parse_mspdi(data)
    except MspdiError:
        pass  # refusing the file is fine; this test checks how long it takes
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"took {elapsed:.1f} s"


def nesting_depth(xml_text: str) -> int:
    parser = ET.XMLPullParser(events=("start", "end"))
    parser.feed(xml_text)
    depth = deepest = 0
    for event, _ in parser.read_events():
        depth += 1 if event == "start" else -1
        deepest = max(deepest, depth)
    return deepest - 1  # levels below the root


@pytest.mark.parametrize("tasks", [in_baseline(nested(10, 2)), in_predecessor_link(nested(10, 2))], ids=["baseline", "plan"])
def test_c14n_never_sees_more_than_a_few_levels(tasks, c14n_inputs):
    # A bound by counting: 3 levels below a Baseline or a PredecessorLink,
    # which is 4 below the Task that holds the planned elements.
    try:
        parse_mspdi(mspdi(tasks))
    except MspdiError:
        pass
    depths = [nesting_depth(text) for text in c14n_inputs]
    assert all(depth <= MAX_NESTING + 1 for depth in depths), depths


def test_c14n_bytes_read_a_carriage_return_in_text_as_a_line_feed():
    # Documented: the bytes are the C14N of the tree as ElementTree writes it
    # out again, which turns a CR in text into a LF; the read values keep it.
    (cr,) = parse_mspdi(mspdi("<Task><UID>1</UID><Name>a&#13;b</Name></Task>")).tasks
    (lf,) = parse_mspdi(mspdi("<Task><UID>1</UID><Name>a&#10;b</Name></Task>")).tasks
    assert (cr.name, lf.name) == ("a\rb", "a\nb")
    assert cr.plan_c14n == lf.plan_c14n == f'<Task xmlns="{NS}"><Name>a\nb</Name></Task>'.encode()


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
        # The namespace check reads "{NS}" as the start of a name; that holds
        # only because expat refuses "}" inside a namespace URI.
        pytest.param(
            f'<Project xmlns="{NS}"><Tasks><Task><UID>1</UID><Baseline><Number>0</Number>'
            f'<a:y xmlns:a="{NS}}}x"/></Baseline></Task></Tasks></Project>'.encode(),
            "not well-formed",
            id="namespace-uri-with-brace",
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
        "element 'y' is outside the MSPDI namespace",
    ),
    # Every element and attribute in a Baseline is in the MSPDI namespace, so C14N sees no other.
    "baseline-foreign-child": (
        '<Task><UID>1</UID><Baseline><Number>0</Number><y xmlns="urn:x"/></Baseline></Task>',
        r"element '\{urn:x\}y' is outside the MSPDI namespace",
    ),
    # No attributes at all: MSPDI uses none in a Baseline or a master-schedule element.
    "baseline-foreign-attribute": (
        '<Task><UID>1</UID><Baseline><Number xmlns:a="urn:x" a:b="1">0</Number></Baseline></Task>',
        r"Number has attribute '\{urn:x\}b'; MSPDI uses none here",
    ),
    "baseline-xml-space-attribute": (
        '<Task><UID>1</UID><Baseline><Number xml:space="preserve">0</Number></Baseline></Task>',
        r"Number has attribute '\{http://www.w3.org/XML/1998/namespace\}space'",
    ),
    "baseline-unnamespaced-attribute": (
        '<Task><UID>1</UID><Baseline b="1"><Number>0</Number></Baseline></Task>',
        "Baseline has attribute 'b'",
    ),
    "baseline-mspdi-attribute": (
        f'<Task><UID>1</UID><Baseline><Number xmlns:p="{NS}" p:z="1">0</Number></Baseline></Task>',
        r"task UID 1 Baseline 0: Number has attribute '\{http://schemas.microsoft.com/project\}z'",
    ),
    # The same holds for the master-schedule elements kept as plan_c14n.
    "name-foreign-attribute": (
        '<Task><UID>1</UID><Name xmlns:a="urn:x" a:b="1">n</Name></Task>',
        r"task UID 1 planned elements: Name has attribute '\{urn:x\}b'",
    ),
    "start-unnamespaced-attribute": (
        '<Task><UID>1</UID><Start b="1">2026-06-01T08:00:00</Start></Task>',
        "task UID 1 planned elements: Start has attribute 'b'",
    ),
    "name-mspdi-attribute": (
        f'<Task><UID>1</UID><Name xmlns:p="{NS}" p:z="1">n</Name></Task>',
        r"task UID 1 planned elements: Name has attribute '\{http://schemas.microsoft.com/project\}z'",
    ),
    "link-mspdi-attribute": (
        f'<Task><UID>1</UID><PredecessorLink xmlns:p="{NS}" p:z="1"><PredecessorUID>2</PredecessorUID>'
        "</PredecessorLink></Task>",
        r"task UID 1 planned elements: PredecessorLink has attribute '\{http://schemas.microsoft.com/project\}z'",
    ),
    "link-foreign-child": (
        '<Task><UID>1</UID><PredecessorLink><PredecessorUID>2</PredecessorUID><y xmlns="urn:x"/>'
        "</PredecessorLink></Task>",
        r"task UID 1 planned elements: element '\{urn:x\}y' is outside the MSPDI namespace",
    ),
    "baseline-nested-too-deep": (
        f"<Task><UID>1</UID><Baseline><Number>0</Number>{DEEP}</Baseline></Task>",
        "nested more than 3 levels below Baseline",
    ),
    # At most 3 levels below a Baseline or a PredecessorLink (UNVERIFIED until M7).
    "baseline-four-levels": (
        "<Task><UID>1</UID><Baseline><Number>0</Number><a><b><c><d/></c></b></a></Baseline></Task>",
        "task UID 1 Baseline 0: elements are nested more than 3 levels below Baseline",
    ),
    "link-four-levels": (
        "<Task><UID>1</UID><PredecessorLink><PredecessorUID>2</PredecessorUID><a><b><c><d/></c></b></a>"
        "</PredecessorLink></Task>",
        "task UID 1 planned elements: elements are nested more than 4 levels below Task",
    ),
    # Numbers stop at 10, MS Project's Baseline plus Baseline1-10 (UNVERIFIED until M7).
    "baseline-number-11": (
        "<Task><UID>1</UID><Baseline><Number>11</Number></Baseline></Task>",
        "task UID 1 Baseline: Number 11 is out of range",
    ),
    "baseline-number-huge": (
        "<Task><UID>1</UID><Baseline><Number>999999999999999999</Number></Baseline></Task>",
        "Number 999999999999999999 is out of range",
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


def test_accepts_three_levels_below_a_baseline_or_a_predecessor_link():
    three = "<a><b><c/></b></a>"
    data = mspdi(
        f"<Task><UID>1</UID><PredecessorLink><PredecessorUID>2</PredecessorUID>{three}</PredecessorLink>"
        f"<Baseline><Number>0</Number>{three}</Baseline></Task>"
    )
    (task,) = parse_mspdi(data).tasks
    expected = f'<Baseline xmlns="{NS}"><Number>0</Number><a><b><c></c></b></a></Baseline>'.encode()
    assert task.baselines[0].xml_c14n == expected


def test_accepts_xml_whitespace_around_typed_values():
    (task,) = parse_mspdi(mspdi("<Task><UID>\t 12 \n</UID><PercentComplete> 40\r\n</PercentComplete></Task>")).tasks
    assert (task.uid, task.percent_complete) == (12, 40)
