import subprocess
import sys

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
