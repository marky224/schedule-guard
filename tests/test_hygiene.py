"""Public-repo hygiene (PLAN.md section 8): no tracked file holds a GUID, a tenant or secret value, or a key.

The scan reads what `git ls-files` lists, so it needs a git checkout: on a
copy with no .git it errors, and can't pass without having looked. A file
that starts with a UTF-16 BOM, or whose bytes alternate between mostly NUL
and mostly not, is scanned as UTF-16 and as UTF-8, so an ASCII line in it is
found too; a line both decodes flag counts once. A finding names the file
and line, never the value. The planted values below are built from parts,
since this file is tracked too.
"""

import codecs
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
GUID_ALLOWLIST: frozenset[str] = frozenset()  # fixture files that may hold a GUID; none does today
# A tenant or client secret name: a word of its own in any case (tenant, TENANT_ID, client-secret, clientSecret),
# or the camelCase end of a longer name (aadTenantId, AADTenantId, MSALClientSecret). A longer word such as
# subtenant is neither.
NAME = (r"(?:(?<![A-Za-z0-9])(?i:tenant(?:[_-]?id)?|client[_-]?secret)"
        r"|(?<=[A-Za-z0-9])(?:Tenant(?:[_-]?I[dD])?|Client[_-]?Secret))")
ASSIGNMENT = re.compile(NAME + r"""["']?\s*[:=]\s*["']?[^\s"']""")
SETTING = re.compile(r"""\bkey\s*=\s*["'][^"']*?""" + NAME + r"""["']\s+value\s*=\s*["'][^"']""")  # <add key= value=/>
# An element with such a name, a value and its closing tag, so a placeholder such as <tenant-id> in a command isn't one.
ELEMENT = re.compile(r"<((?:[\w.-]+:)?[\w.-]*?" + NAME + r")(?:\s[^>]*)?>\s*[^<\s][^<]*</\1\s*>")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
KEY_FILES = (".pem", ".pfx", ".p12", ".key", ".cer", ".crt", ".der")  # the suffixes .gitignore keeps out


def tracked(root: Path) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [name for name in listed.decode().split("\0") if name]


def text(data: bytes) -> str:
    """A file's text: UTF-16 if it starts with a UTF-16 BOM, or if one byte parity is mostly NUL and the other
    mostly not (so NUL padding, as in a binary file, still reads as UTF-8), else UTF-8."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    for first_nul, codec in ((1, "utf-16-le"), (0, "utf-16-be")):
        nuls, other = data[first_nul::2], data[1 - first_nul::2]
        if nuls and nuls.count(0) > len(nuls) / 2 and other.count(0) < len(other) / 2:
            return data.decode(codec, "replace")
    return data.decode("utf-8", "replace")


def findings(root: Path, names: list[str], guid_allowlist: frozenset[str] = GUID_ALLOWLIST) -> list[str]:
    """What the hygiene rules find in the files `names` under `root`."""
    found = []
    for name in names:
        if name.lower().endswith(KEY_FILES):
            found.append(f"{name}: key file")
        path = root / name
        if not path.is_file():  # deleted from the working tree, not yet from the index
            continue
        data = path.read_bytes()
        here = []
        for decoded in dict.fromkeys([text(data), data.decode("utf-8", "replace")]):  # one, unless text() read UTF-16
            for number, line in enumerate(decoded.splitlines(), 1):
                if name not in guid_allowlist and GUID.search(line):
                    here.append(f"{name}:{number}: GUID")
                if ASSIGNMENT.search(line) or SETTING.search(line):
                    here.append(f"{name}:{number}: tenant or secret assignment")
                if ELEMENT.search(line):
                    here.append(f"{name}:{number}: tenant or secret element")
                if PRIVATE_KEY.search(line):
                    here.append(f"{name}:{number}: private key")
        found.extend(dict.fromkeys(here))  # the same file, line and kind from both decodes is one finding
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
SECRET_CAMEL = "Client" + "Secret"
TENANT_CAMEL = "Tenant" + "Id"


def key_header(kind: str) -> str:
    """The first line of a PEM block of `kind`, such as "RSA PRIVATE KEY"."""
    return "-" * 5 + "BEGIN " + kind + "-" * 5


def git_repo(root: Path, *planted: tuple[str, str | bytes]) -> None:
    """A real git repo at `root` with each (name, content) tracked; -f, in case a global ignore names a key file."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, content in planted:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
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
    ("certs/app.p12", "", "certs/app.p12: key file"),
    ("certs/app.key", "", "certs/app.key: key file"),
    ("certs/app.cer", "", "certs/app.cer: key file"),
    ("certs/app.crt", "", "certs/app.crt: key file"),
    ("certs/APP.DER", "", "certs/APP.DER: key file"),
    ("notes.txt", f"Keep this safe:\n{key_header('PRIVATE KEY')}\nMIIEvQIBADAN\n", "notes.txt:2: private key"),
    ("notes.txt", f"{key_header('RSA PRIVATE KEY')}\n", "notes.txt:1: private key"),
    ("notes.txt", f"{key_header('EC PRIVATE KEY')}\n", "notes.txt:1: private key"),
    ("notes.txt", f"{key_header('OPENSSH PRIVATE KEY')}\n", "notes.txt:1: private key"),
    ("notes.txt", f"{key_header('ENCRYPTED PRIVATE KEY')}\n", "notes.txt:1: private key"),
    ("app.config", f"<appSettings>\n  <{SECRET_CAMEL}>abc123</{SECRET_CAMEL}>\n", "app.config:2: tenant or secret element"),
    ("app.config", f'<{TENANT_CAMEL} kind="aad">example-org</{TENANT_CAMEL}>\n', "app.config:1: tenant or secret element"),
    ("app.config", f"<cfg:{SECRET_CAMEL}>abc123</cfg:{SECRET_CAMEL}>\n", "app.config:1: tenant or secret element"),
    ("notes.txt", f"The site is {PLANTED_GUID}.\n".encode("utf-16"), "notes.txt:1: GUID"),
    ("settings.env", f"{SECRET_WORD}=abc123\n".encode("utf-16-le"), "settings.env:1: tenant or secret assignment"),
    ("settings.env", f"{TENANT_WORD}Id=abc123\n".encode("utf-16-be"), "settings.env:1: tenant or secret assignment"),
    ("blob.bin", b"\0" * 400 + f'\n{{"{SECRET_WORD}": "abc123"}}\n'.encode(),
     "blob.bin:2: tenant or secret assignment"),
    ("blob.bin", b"\0" * 400 + f"\n{PLANTED_GUID}\n".encode(), "blob.bin:2: GUID"),
    ("data.sqlite3", b"SQLite format 3\0" + b"\0" * 3000 + f"{SECRET_WORD}=abc123\n".encode(),
     "data.sqlite3:1: tenant or secret assignment"),
    ("settings.env", f"AAD{TENANT_CAMEL}=abc123\n", "settings.env:1: tenant or secret assignment"),
    ("appsettings.json", f'{{"AzureAD{SECRET_CAMEL}": "abc123"}}\n',
     "appsettings.json:1: tenant or secret assignment"),
    ("settings.env", f"MSAL{SECRET_CAMEL}=abc123\n", "settings.env:1: tenant or secret assignment"),
    ("notes.txt", f"{key_header('PGP PRIVATE KEY BLOCK')}\n", "notes.txt:1: private key"),
    ("web.config", f'<appSettings>\n  <add key="{SECRET_CAMEL}" value="abc123" />\n</appSettings>\n',
     "web.config:2: tenant or secret assignment"),
    ("blob.bin", b"\x01\x00" * 300 + f"\n{SECRET_WORD}=abc123\n".encode(), "blob.bin:2: tenant or secret assignment"),
    ("notes.txt", codecs.BOM_UTF16_LE + "Notes for the app\n".encode("utf-16-le")
     + f"{SECRET_WORD}=abc123\n".encode(), "notes.txt:2: tenant or secret assignment"),
    ("settings.env", f"{SECRET_WORD}=abc123\n".encode() + "Notes for the app\n".encode("utf-16-le") * 20,
     "settings.env:1: tenant or secret assignment"),
    ("notes.txt", PLANTED_GUID.encode() + f" {PLANTED_GUID}\n".encode("utf-16-le"), "notes.txt:1: GUID"),
], ids=["guid", "upper-guid", "secret", "env-secret", "env-tenant-id", "yaml-tenant", "json-tenant", "pem", "pfx", "p12",
        "key", "cer", "crt", "der", "private-key", "rsa-private-key", "ec-private-key", "openssh-private-key",
        "encrypted-private-key", "xml-secret", "xml-tenant-with-attribute", "xml-namespaced-secret", "utf16-bom-guid",
        "utf16le-secret", "utf16be-tenant-id", "nul-padded-secret", "nul-padded-guid", "sqlite-header-secret",
        "acronym-tenant-id", "acronym-json-secret", "acronym-env-secret", "pgp-private-key", "app-setting-secret",
        "alternating-bytes-then-ascii-secret", "utf16-then-ascii-secret", "ascii-secret-then-utf16",
        "guid-in-both-decodes"])
def test_a_planted_file_in_a_git_repo_is_found(tmp_path, name, text, found):
    git_repo(tmp_path, (name, text))
    assert findings(tmp_path, tracked(tmp_path)) == [found]


def test_camel_case_names_in_a_json_file_are_found(tmp_path):
    settings = f'{{\n  "azure{SECRET_CAMEL}": "abc123",\n  "aad{TENANT_CAMEL}": "example-org"\n}}\n'
    git_repo(tmp_path, ("appsettings.json", settings))
    assert findings(tmp_path, tracked(tmp_path)) == [
        "appsettings.json:2: tenant or secret assignment", "appsettings.json:3: tenant or secret assignment",
    ]


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
    "SUBTENANT = abc",
    f"The {TENANT_CAMEL} column is filled in by the {TENANT_WORD} admin.",
    f"<{SECRET_CAMEL}></{SECRET_CAMEL}>",
    f"<{SECRET_CAMEL}/>",
    f"Connect-MgGraph -{TENANT_CAMEL} <{TENANT_WORD}-id> -Scopes Sites.Selected",
    f"az login --{TENANT_WORD} <{TENANT_WORD}-id> --allow-no-subscriptions",
    f"https://login.microsoftonline.com/<{TENANT_WORD}>/oauth2/v2.0/token",
    f"Replace <{SECRET_WORD.replace('_', '-')}> with the value from the portal.",
    f"<sub{TENANT_WORD}>abc</sub{TENANT_WORD}>",
    f'<add key="{SECRET_CAMEL}" value="" />',
], ids=["prose", "prose-capital", "names-in-backticks", "empty-env", "empty-string", "mermaid", "longer-word",
        "longer-word-upper", "camel-in-prose", "empty-element", "self-closing-element", "graph-placeholder",
        "az-placeholder", "url-placeholder", "secret-placeholder", "longer-word-element", "empty-app-setting"])
def test_a_mention_or_an_empty_value_is_not_an_assignment(tmp_path, line):
    git_repo(tmp_path, ("notes.md", f"{line}\n"))
    assert findings(tmp_path, tracked(tmp_path)) == []


def test_a_guid_passes_only_in_a_file_on_the_allowlist(tmp_path):
    fixture = "tests/fixtures/updates.jsonl"
    git_repo(tmp_path, (fixture, f"{PLANTED_GUID}\n"), ("other.jsonl", f"{PLANTED_GUID}\n"))
    assert findings(tmp_path, tracked(tmp_path), frozenset({fixture})) == ["other.jsonl:1: GUID"]


@pytest.mark.parametrize("kind", ["PUBLIC KEY", "CERTIFICATE"])
def test_a_public_key_or_certificate_block_is_not_a_private_key(tmp_path, kind):
    git_repo(tmp_path, ("notes.txt", f"{key_header(kind)}\nMIIBIjANBgkq\n"))
    assert findings(tmp_path, tracked(tmp_path)) == []
