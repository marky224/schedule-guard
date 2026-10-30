"""Generate the fictional Riverbend Clinic MSPDI fixture.

Writes tests/fixtures/riverbend-clinic.xml: a 40-task clinic build by the
fictional "Example Builders Co." in seven phases, with 5 milestones,
finish-to-start predecessor links and Baseline 0, and no resources or
assignments (PLAN.md §6, C5). A seeded RNG picks crew sizes, cost rates
and the tasks that slipped after the baseline was saved, so the same seed
always gives the same bytes and some tasks show baseline variance.
Dates use a Monday-to-Friday, 08:00-17:00 calendar with no holidays.

Work is crew-hours written straight onto each task. With no assignments,
MS Project would derive Work itself; the fixture is input for the guard,
not a check of Project's scheduling.

Usage: python scripts/make_fixture.py [--out PATH]
"""

import argparse
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "http://schemas.microsoft.com/project"
SEED = 20261023
PROJECT_START = date(2026, 6, 1)  # a Monday
DAY_START = time(8, 0)
DAY_FINISH = time(17, 0)
HOURS_PER_DAY = 8
SLIPPED_TASKS = 6
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "riverbend-clinic.xml"

# Phase -> tasks as (name, working days, predecessor names); 0 days is a milestone.
# Predecessors always come earlier in this table.
PHASES = [
    ("Site work", [
        ("Notice to proceed", 0, []),
        ("Mobilize and site fencing", 3, ["Notice to proceed"]),
        ("Clearing and rough grading", 6, ["Mobilize and site fencing"]),
        ("Underground utilities", 10, ["Clearing and rough grading"]),
    ]),
    ("Foundations", [
        ("Excavate footings", 5, ["Clearing and rough grading"]),
        ("Footings and foundation walls", 12, ["Excavate footings"]),
        ("Underslab plumbing", 4, ["Footings and foundation walls", "Underground utilities"]),
        ("Slab on grade", 5, ["Underslab plumbing"]),
        ("Foundations complete", 0, ["Slab on grade"]),
    ]),
    ("Structure", [
        ("Structural steel erection", 15, ["Foundations complete"]),
        ("Metal deck and elevated slab", 8, ["Structural steel erection"]),
        ("Roof framing", 6, ["Metal deck and elevated slab"]),
    ]),
    ("Envelope", [
        ("Exterior framing and sheathing", 10, ["Metal deck and elevated slab"]),
        ("Roofing", 8, ["Roof framing"]),
        ("Windows and storefront", 10, ["Exterior framing and sheathing"]),
        ("Dry-in", 0, ["Roofing", "Windows and storefront"]),
        ("Exterior cladding", 15, ["Windows and storefront"]),
    ]),
    ("MEP rough-in", [
        ("Plumbing rough-in", 12, ["Dry-in"]),
        ("HVAC rough-in", 15, ["Dry-in"]),
        ("Electrical rough-in", 14, ["Dry-in"]),
        ("Medical gas and fire sprinkler rough-in", 8, ["Plumbing rough-in"]),
        ("Above-ceiling inspection", 2,
         ["HVAC rough-in", "Electrical rough-in", "Medical gas and fire sprinkler rough-in"]),
    ]),
    ("Interiors", [
        ("Drywall hang and finish", 15, ["Above-ceiling inspection"]),
        ("Interior paint", 8, ["Drywall hang and finish"]),
        ("Ceilings and flooring", 12, ["Interior paint"]),
        ("Doors, frames & hardware", 6, ["Interior paint"]),
        ("Casework and millwork", 8, ["Ceilings and flooring"]),
    ]),
    ("Closeout", [
        ("MEP trim and fixtures", 10, ["Ceilings and flooring"]),
        ("Testing and balancing", 5, ["MEP trim and fixtures"]),
        ("Substantial completion", 0,
         ["Testing and balancing", "Casework and millwork", "Doors, frames & hardware",
          "Exterior cladding"]),
        ("Punch list", 10, ["Substantial completion"]),
        ("Final completion", 0, ["Punch list"]),
    ]),
]


@dataclass
class Row:
    uid: int
    name: str
    outline: str  # written as both OutlineNumber and WBS
    level: int
    baseline_days: int = 0
    days: int = 0  # current planned duration; more than baseline_days if it slipped
    crew: int = 0
    rate: int = 0  # cost per crew-hour
    preds: list[int] = field(default_factory=list)
    children: list["Row"] = field(default_factory=list)

    @property
    def is_summary(self) -> bool:
        return bool(self.children)

    @property
    def is_milestone(self) -> bool:
        return not self.children and self.baseline_days == 0


def next_workday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def earliest_start(pred_finish: datetime) -> datetime:
    """A successor starts at the first working moment after its predecessor finishes."""
    if pred_finish.time() == DAY_FINISH:
        return datetime.combine(next_workday(pred_finish.date()), DAY_START)
    return pred_finish


def working_hours(start: datetime, finish: datetime) -> int:
    days, d = 1, start.date()
    while d < finish.date():
        d = next_workday(d)
        days += 1
    hours = days * HOURS_PER_DAY
    if start.time() == DAY_FINISH:
        hours -= HOURS_PER_DAY
    if finish.time() == DAY_START:
        hours -= HOURS_PER_DAY
    return hours


def forward_pass(leaves: list[Row], duration_of) -> dict[int, tuple[datetime, datetime]]:
    dates: dict[int, tuple[datetime, datetime]] = {}
    for row in leaves:
        days = duration_of(row)
        if not row.preds:
            start = datetime.combine(PROJECT_START, DAY_START)
        elif days == 0:
            # A milestone sits at the moment its last predecessor finishes.
            start = max(dates[p][1] for p in row.preds)
        else:
            start = max(earliest_start(dates[p][1]) for p in row.preds)
        if days == 0:
            dates[row.uid] = (start, start)
            continue
        finish_day = start.date()
        for _ in range(days - 1):
            finish_day = next_workday(finish_day)
        dates[row.uid] = (start, datetime.combine(finish_day, DAY_FINISH))
    return dates


def roll_up(row: Row, dates: dict[int, tuple[datetime, datetime]]) -> tuple[datetime, datetime]:
    if row.children:
        spans = [roll_up(child, dates) for child in row.children]
        dates[row.uid] = (min(s for s, _ in spans), max(f for _, f in spans))
    return dates[row.uid]


def work_hours(row: Row, days_of) -> int:
    if row.children:
        return sum(work_hours(child, days_of) for child in row.children)
    return days_of(row) * HOURS_PER_DAY * row.crew


def cost(row: Row, days_of) -> int:
    if row.children:
        return sum(cost(child, days_of) for child in row.children)
    return days_of(row) * HOURS_PER_DAY * row.crew * row.rate


def build_rows(rng: random.Random) -> tuple[Row, list[Row]]:
    root = Row(uid=0, name="Riverbend Clinic", outline="0", level=0)
    rows, by_name, leaves = [root], {}, []
    for p, (phase, tasks) in enumerate(PHASES, start=1):
        summary = Row(uid=len(rows), name=phase, outline=str(p), level=1)
        rows.append(summary)
        root.children.append(summary)
        for t, (name, days, preds) in enumerate(tasks, start=1):
            leaf = Row(uid=len(rows), name=name, outline=f"{p}.{t}", level=2,
                       baseline_days=days, days=days, preds=[by_name[n] for n in preds])
            if days:
                leaf.crew = rng.randint(2, 6)
                leaf.rate = rng.randint(11, 19) * 5
            rows.append(leaf)
            summary.children.append(leaf)
            by_name[name] = leaf.uid
            leaves.append(leaf)
    for leaf in rng.sample([r for r in leaves if r.days], SLIPPED_TASKS):
        leaf.days += rng.randint(1, 4)
    return root, rows


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def hours(h: int) -> str:
    return f"PT{h}H0M0S"


def add(parent: ET.Element, tag: str, text: str) -> ET.Element:
    el = ET.SubElement(parent, f"{{{NS}}}{tag}")
    el.text = text
    return el


def build() -> bytes:
    rng = random.Random(SEED)
    root, rows = build_rows(rng)
    leaves = [r for r in rows if not r.children]
    now = forward_pass(leaves, lambda r: r.days)
    base = forward_pass(leaves, lambda r: r.baseline_days)
    roll_up(root, now)
    roll_up(root, base)

    project = ET.Element(f"{{{NS}}}Project")
    add(project, "Name", "riverbend-clinic.xml")
    add(project, "Title", "Riverbend Clinic")
    add(project, "Company", "Example Builders Co.")
    add(project, "CreationDate", "2026-05-15T09:00:00")
    add(project, "ScheduleFromStart", "1")
    add(project, "StartDate", fmt(now[0][0]))
    add(project, "FinishDate", fmt(now[0][1]))
    tasks = ET.SubElement(project, f"{{{NS}}}Tasks")
    # Child order follows the MSPDI Task and Baseline schema sequence [L14].
    for row in rows:
        start, finish = now[row.uid]
        b_start, b_finish = base[row.uid]
        work = hours(work_hours(row, lambda r: r.days))
        task = ET.SubElement(tasks, f"{{{NS}}}Task")
        add(task, "UID", str(row.uid))
        add(task, "ID", str(row.uid))
        add(task, "Name", row.name)
        add(task, "WBS", row.outline)
        add(task, "OutlineNumber", row.outline)
        add(task, "OutlineLevel", str(row.level))
        add(task, "Start", fmt(start))
        add(task, "Finish", fmt(finish))
        add(task, "Duration", hours(working_hours(start, finish)))
        add(task, "DurationFormat", "7")
        add(task, "Work", work)
        add(task, "Milestone", "1" if row.is_milestone else "0")
        add(task, "Summary", "1" if row.is_summary else "0")
        add(task, "PercentComplete", "0")
        add(task, "RemainingWork", work)
        for pred in row.preds:
            link = ET.SubElement(task, f"{{{NS}}}PredecessorLink")
            add(link, "PredecessorUID", str(pred))
            add(link, "Type", "1")  # finish-to-start
            add(link, "CrossProject", "0")
            add(link, "LinkLag", "0")
            add(link, "LagFormat", "7")
        baseline = ET.SubElement(task, f"{{{NS}}}Baseline")
        add(baseline, "Number", "0")
        add(baseline, "Start", fmt(b_start))
        add(baseline, "Finish", fmt(b_finish))
        add(baseline, "Duration", hours(working_hours(b_start, b_finish)))
        add(baseline, "DurationFormat", "7")
        add(baseline, "Work", hours(work_hours(row, lambda r: r.baseline_days)))
        add(baseline, "Cost", str(cost(row, lambda r: r.baseline_days)))

    ET.indent(project, space="  ")
    return ET.tostring(project, encoding="utf-8", xml_declaration=True, default_namespace=NS) + b"\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the fictional Riverbend Clinic MSPDI fixture.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output path (default: {DEFAULT_OUT.relative_to(DEFAULT_OUT.parents[2])})")
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
