"""Claims the documentation makes that can be checked mechanically.

Docs drift silently: a command is added and never documented, a guide that
"ships with v0.1" never ships, a README keeps describing what a command *will*
do after it does something else. These tests are the cheap half of keeping the
docs honest; the expensive half is reading them.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
]
SHIPPED = [
    "R1",
    "R2",
    "R3",
    "R4",
    "R5",
    "R6",
    "R7",
    "R8",
    "R9",
    "R10",
    "R11",
    "R12",
    "R14",
    "R16",
    "R17",
]


def _prose(text: str) -> str:
    """Text outside code: fenced blocks and inline code spans removed, so a
    seed whose payload *is* `# TODO(agent):` is not a placeholder."""
    text = re.sub(r"^(`{3,}|~{3,}).*?^\1", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """One `## ` section: from its heading to the next one."""
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 3)
    return text[start : end if end >= 0 else None]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_doc_contains_a_placeholder(path):
    offenders = [
        f"{path.name}:{i}"
        for i, line in enumerate(_prose(path.read_text(encoding="utf-8")).splitlines(), 1)
        if re.search(r"\b(TODO|TBD|FIXME|XXX)\b", line)
    ]
    assert not offenders, offenders


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_doc_promises_what_has_already_shipped(path):
    """ "Ships with v0.1" and "not built yet" were true once. A doc still saying
    so after the thing exists sends a reader looking for nothing."""
    text = _prose(path.read_text(encoding="utf-8")).lower()
    for stale in ("ships with v0.1", "not built yet", "export next", "will be published"):
        assert stale not in text, f"{path.name}: {stale!r}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_point_at_files_that_exist(path):
    text = path.read_text(encoding="utf-8")
    broken = []
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if re.match(r"[a-z]+:", target):
            continue
        if not (path.parent / target).exists():
            broken.append(target)
    assert not broken, f"{path.name}: {broken}"


def test_readme_walks_the_whole_pipeline_in_order():
    text = _read("README.md")
    steps = [
        "reprobe fuzz",
        "reprobe triage",
        "reprobe export",
        "pytest",
        "reprobe verify",
        "reprobe report",
    ]
    walkthrough = _section(text, "Try it")
    positions = [walkthrough.find(step) for step in steps]
    assert all(p >= 0 for p in positions), dict(zip(steps, positions, strict=True))
    assert positions == sorted(positions)


def test_every_cli_command_is_in_the_readmes_command_table():
    from typer.main import get_command

    from reprobe.cli import app

    table = _section(_read("README.md"), "Commands")
    group = get_command(app)
    for name, command in group.commands.items():  # type: ignore[attr-defined]
        subs = getattr(command, "commands", None)
        for full in [f"{name} {sub}" for sub in subs] if subs else [name]:
            assert f"| `reprobe {full}` |" in table, f"undocumented command: {full}"


def test_readme_states_the_licence_and_the_safety_boundary():
    text = _read("README.md").lower()
    assert "apache-2.0" in text
    assert "synthetic" in text and "mock gateway" in text


def test_readme_quotes_the_measured_benchmark_not_the_target():
    """If the 2x target was missed, the README says what was measured."""
    section = _section(_read("README.md"), "Benchmark")
    assert "0.75" + chr(0xD7) in section  # the multiplication sign, as written there
    assert "Missed" in section
    assert "docs/benchmark-v0.1.md" in section


def test_readme_does_not_offer_an_install_that_does_not_exist_yet():
    """`reprobe-agents` is not on PyPI until the release is cut."""
    from reprobe import __version__

    if ".dev" in __version__:
        assert "pip install reprobe-agents" not in _read("README.md")
        assert "uv tool install reprobe-agents" not in _read("README.md")


def test_disclosure_guide_names_each_supported_agents_published_contact():
    text = _read("docs/disclosure.md")
    assert "github.com/anthropics/claude-code/blob/main/SECURITY.md" in text
    assert "github.com/openai/codex/blob/main/SECURITY.md" in text
    assert "90" in text and "coordinated" in text.lower()
    assert "terms of service" in text.lower()


def test_security_policy_links_the_disclosure_guide():
    assert "docs/disclosure.md" in _read("SECURITY.md")


def test_every_shipped_requirement_maps_to_a_module_that_exists():
    """The architecture doc's traceability table, checked against the tree:
    each shipped requirement names at least one source file, and it exists."""
    text = _read("docs/architecture.md")
    for rid in SHIPPED:
        row = next(
            (line for line in text.splitlines() if re.match(rf"\|\s*{rid}\s*\|", line)), None
        )
        assert row, f"{rid} has no row in the traceability table"
        files = re.findall(r"`(src/reprobe/[^`]+\.py)`", row)
        assert files, f"{rid} names no source file"
        for name in files:
            assert (ROOT / name).is_file(), f"{rid}: {name} does not exist"


def test_the_architecture_doc_shows_the_trial_record_in_full():
    """The trial record is the replay contract; the doc must not drift from
    what `_store_record` writes."""
    import inspect

    from reprobe import trial

    source = inspect.getsource(trial._store_record)
    keys = re.findall(r'^\s*"([a-z_]+)":', source, flags=re.M)
    text = _read("docs/architecture.md")
    assert keys
    missing = [k for k in keys if f'"{k}"' not in text]
    assert not missing, missing


def test_ci_guide_covers_what_an_adopter_needs():
    text = _read("docs/ci.md")
    for needle in (
        "REPROBE_REGRESSION_TESTS",
        "--workflow",
        "INCONCLUSIVE",
        "reprobe verify",
        ".reprobe/",
        "--max-trials",
    ):
        assert needle in text, needle


def test_shipped_commands_in_docs_parse():
    """Every `reprobe ...` command line in the README must name real options."""
    from typer.main import get_command

    from reprobe.cli import app

    group = get_command(app)
    text = _read("README.md")
    pattern = r"^(?:[A-Z_]+=\S+ )*(?:uv run )?reprobe ([a-z]+)((?: \\\n|[^\n])*)"
    matches = list(re.finditer(pattern, text, flags=re.M))
    assert len(matches) >= 6, f"only {len(matches)} command lines found; the check would be vacuous"
    for match in matches:
        name, rest = match.group(1), match.group(2)
        command = group.commands[name]  # type: ignore[attr-defined]
        if name == "seeds":
            continue
        known = {o for p in command.params for o in getattr(p, "opts", [])}
        known |= {o for p in command.params for o in getattr(p, "secondary_opts", [])}
        for option in re.findall(r"(?<!\S)(--[a-z-]+)", rest):
            assert option in known, f"reprobe {name}: unknown option {option}"
