import shutil
import subprocess
import sys
import sysconfig

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
    # Look only in this environment's scripts directory, so another
    # schedule-guard on PATH can't stand in for the one this venv installed.
    script = shutil.which("schedule-guard", path=sysconfig.get_path("scripts"))
    assert script, "schedule-guard console script is not installed"
    result = subprocess.run(
        [script, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"schedule-guard {__version__}"
