import subprocess
import sys
from pathlib import Path

from schedule_guard.mspdi.parse import parse_mspdi, read_mspdi_bytes

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "make_fixture.py"
FIXTURE = ROOT / "tests" / "fixtures" / "riverbend-clinic.xml"

PHASES = ["Site work", "Foundations", "Structure", "Envelope", "MEP rough-in", "Interiors", "Closeout"]
MILESTONES = ["Notice to proceed", "Foundations complete", "Dry-in", "Substantial completion", "Final completion"]


def test_regenerating_the_fixture_gives_identical_bytes(tmp_path):
    out = tmp_path / "riverbend-clinic.xml"
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(out)], check=True)
    assert out.read_bytes() == FIXTURE.read_bytes()


def test_fixture_has_the_planned_shape():
    raw = read_mspdi_bytes(FIXTURE)
    tasks = parse_mspdi(raw).tasks
    assert len(tasks) == 40
    assert [t.name for t in tasks if t.is_summary] == ["Riverbend Clinic", *PHASES]
    assert [t.name for t in tasks if t.is_milestone] == MILESTONES
    assert all([b.number for b in t.baselines] == [0] for t in tasks)
    uids = {t.uid for t in tasks}
    leaves = [t for t in tasks if not t.is_summary]
    assert all(t.predecessors for t in leaves if t.name != "Notice to proceed")
    assert all(link.predecessor_uid in uids for t in tasks for link in t.predecessors)
    # Some tasks slipped after Baseline 0 was saved, so the report has variance to show.
    assert any(t.planned_finish != t.baselines[0].finish for t in leaves)
    # No resources or assignments (PLAN.md C5).
    assert b"<Resources" not in raw and b"<Assignments" not in raw
