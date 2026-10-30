import shutil
import subprocess
import sys
from pathlib import Path

from schedule_guard import __version__


def test_cli_version_prints_package_version():
    result = subprocess.run(
        [sys.executable, "-m", "schedule_guard.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"schedule-guard {__version__}"


def test_installed_console_script_prints_package_version():
    bin_dir = Path(sys.executable).parent
    script = shutil.which("schedule-guard", path=str(bin_dir)) or shutil.which("schedule-guard")
    assert script, "schedule-guard console script is not installed"
    result = subprocess.run(
        [script, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"schedule-guard {__version__}"
