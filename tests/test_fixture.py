import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "make_fixture.py"
FIXTURE = ROOT / "tests" / "fixtures" / "riverbend-clinic.xml"


def test_regenerating_the_fixture_gives_identical_bytes(tmp_path):
    out = tmp_path / "riverbend-clinic.xml"
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(out)], check=True)
    assert out.read_bytes() == FIXTURE.read_bytes()
