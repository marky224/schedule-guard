"""Public-repo hygiene (PLAN.md section 8): no tracked file holds a GUID, a tenant or secret assignment, or a key.

The scan reads what `git ls-files` lists, so it needs a git checkout: on a
copy with no .git it errors, and can't pass without having looked. A finding
names the file and line, never the value. The planted values below are built
from parts, since this file is tracked too.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
GUID_ALLOWLIST: frozenset[str] = frozenset()  # fixture files that may hold a GUID; none does today
ASSIGNMENT = re.compile(r"""(?<![a-z0-9])(?:tenant(?:[_-]?id)?|client[_-]?secret)["']?\s*[:=]\s*["']?[^\s"']""", re.I)
KEY_FILES = (".pem", ".pfx")


def tracked(root: Path) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [name for name in listed.decode().split("\0") if name]


def findings(root: Path, names: list[str], guid_allowlist: frozenset[str] = GUID_ALLOWLIST) -> list[str]:
    """What the hygiene rules find in the files `names` under `root`."""
    found = []
    for name in names:
        if name.lower().endswith(KEY_FILES):
            found.append(f"{name}: key file")
        path = root / name
        if not path.is_file():  # deleted from the working tree, not yet from the index
            continue
        for number, line in enumerate(path.read_bytes().decode("utf-8", "replace").splitlines(), 1):
            if name not in guid_allowlist and GUID.search(line):
                found.append(f"{name}:{number}: GUID")
            if ASSIGNMENT.search(line):
                found.append(f"{name}:{number}: tenant or secret assignment")
    return found


def test_no_tracked_file_breaks_the_hygiene_rules():
    names = tracked(REPO)
    assert "tests/test_hygiene.py" in names  # it really looked at the repo
    assert findings(REPO, names) == []


def test_only_fixture_files_can_be_on_the_guid_allowlist():
    assert all(name.startswith("tests/fixtures/") for name in GUID_ALLOWLIST)


PLANTED_GUID = "-".join(["0f1e2d3c", "4b5a", "6978", "8796", "a5b4c3d2e1f0"])
SECRET_WORD = "client_" + "secret"
TENANT_WORD = "ten" + "ant"


def git_repo(root: Path, *planted: tuple[str, str]) -> None:
    """A real git repo at `root` with each (name, text) tracked; -f, in case a global ignore names a key file."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, text in planted:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
        subprocess.run(["git", "add", "-f", name], cwd=root, check=True)


@pytest.mark.parametrize(("name", "text", "found"), [
    ("notes.md", f"The site is {PLANTED_GUID}.\n", "notes.md:1: GUID"),
    ("notes.md", f"line one\nsite {PLANTED_GUID.upper()}\n", "notes.md:2: GUID"),
    ("settings.py", f'{SECRET_WORD} = "abc123"\n', "settings.py:1: tenant or secret assignment"),
    ("settings.env", f"AZURE_{SECRET_WORD.upper()}=abc123\n", "settings.env:1: tenant or secret assignment"),
    ("settings.env", f"AZURE_{TENANT_WORD.upper()}_ID=abc123\n", "settings.env:1: tenant or secret assignment"),
    ("settings.yml", f"{TENANT_WORD}: example-org\n", "settings.yml:1: tenant or secret assignment"),
    ("settings.json", f'{{"{TENANT_WORD}Id": "abc123"}}\n', "settings.json:1: tenant or secret assignment"),
    ("app.pem", "", "app.pem: key file"),
    ("deploy/APP.PFX", "", "deploy/APP.PFX: key file"),
], ids=["guid", "upper-guid", "secret", "env-secret", "env-tenant-id", "yaml-tenant", "json-tenant", "pem", "pfx"])
def test_a_planted_file_in_a_git_repo_is_found(tmp_path, name, text, found):
    git_repo(tmp_path, (name, text))
    assert findings(tmp_path, tracked(tmp_path)) == [found]


def test_a_finding_never_prints_the_value(tmp_path):
    git_repo(tmp_path, ("settings.py", f'{SECRET_WORD} = "abc123"  # {PLANTED_GUID}\n'))
    report = "\n".join(findings(tmp_path, tracked(tmp_path)))
    assert report.count("settings.py:1:") == 2
    assert "abc123" not in report and PLANTED_GUID not in report


def test_a_file_that_isnt_tracked_is_not_scanned(tmp_path):
    git_repo(tmp_path, ("notes.md", "Nothing to see.\n"))
    (tmp_path / "local.env").write_text(f"{SECRET_WORD}=abc123\n{PLANTED_GUID}\n")
    (tmp_path / "local.pem").write_text("")
    assert findings(tmp_path, tracked(tmp_path)) == []


@pytest.mark.parametrize("line", [
    f"The {TENANT_WORD} admin sets this up once.",
    f"{TENANT_WORD.capitalize()} settings live in a gitignored file.",
    f"`{TENANT_WORD}`/`{SECRET_WORD}` assignments and key files",
    f"{TENANT_WORD.upper()}_ID=",
    f'{SECRET_WORD} = ""',
    f"subgraph {TENANT_WORD}[{TENANT_WORD.capitalize()} (Phase 2)]",
    "subtenant = abc",
], ids=["prose", "prose-capital", "names-in-backticks", "empty-env", "empty-string", "mermaid", "longer-word"])
def test_a_mention_or_an_empty_value_is_not_an_assignment(tmp_path, line):
    git_repo(tmp_path, ("notes.md", f"{line}\n"))
    assert findings(tmp_path, tracked(tmp_path)) == []


def test_a_guid_passes_only_in_a_file_on_the_allowlist(tmp_path):
    fixture = "tests/fixtures/updates.jsonl"
    git_repo(tmp_path, (fixture, f"{PLANTED_GUID}\n"), ("other.jsonl", f"{PLANTED_GUID}\n"))
    assert findings(tmp_path, tracked(tmp_path), frozenset({fixture})) == ["other.jsonl:1: GUID"]
