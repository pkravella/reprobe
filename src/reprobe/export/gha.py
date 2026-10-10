"""R11: a GitHub Action that runs an exported suite.

The workflow runs in the *user's* repository, which has neither Reprobe's
Makefile nor its images, and before a release Reprobe is not on PyPI either.
So it checks out Reprobe itself at a pinned ref, builds only the images the
exported findings' agents need from that checkout, and runs the suite with
Reprobe installed from the same source.

Scheduled weekly by default rather than on every push, because each run spends
real agent budget. A pull request touching the suite re-checks it -- but only
from this repository: fork PRs get no secrets, so the opted-in suite would fail
on every one of them by design.

Everything interpolated is validated or escaped. The header comment lists the
findings' action keys, which carry paths the agent produced under attack, and a
line break ends a YAML comment (PyYAML also breaks on NEL and the Unicode line
and paragraph separators), so non-printables are escaped there. Values go in as
JSON strings, which are valid YAML scalars. The suite path reaches the shell
through `env`, never as text in a `run:` line, and a `${{` anywhere a value can
reach is refused, because Actions evaluates it before any shell sees it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from reprobe import __version__
from reprobe.agents import get_adapter
from reprobe.errors import ConfigError
from reprobe.export.pytest_export import _doc, _env, _required_env
from reprobe.finding import Finding

DEFAULT_SCHEDULE = "0 6 * * 1"  # Mondays, 06:00 UTC
REPROBE_REPOSITORY = "pkravella/reprobe"
SOURCE_DIR = ".reprobe-src"

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-?#LW]+$")
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


def _resolve_ref(reprobe_ref: str | None) -> str:
    if reprobe_ref is None:
        if not _RELEASE.match(__version__):
            raise ConfigError(
                f"reprobe {__version__} is a development version with no release tag; pass "
                "reprobe_ref (a tag, branch or commit of the Reprobe repository) to pin one"
            )
        reprobe_ref = f"v{__version__}"
    if not _REF.match(reprobe_ref):
        raise ConfigError(f"reprobe_ref {reprobe_ref!r} is not a plain git ref")
    return reprobe_ref


def _check_schedule(schedule: str) -> None:
    fields = schedule.split()
    if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
        raise ConfigError(f"schedule {schedule!r} is not five cron fields")


def _images(findings: Sequence[Finding]) -> list[str]:
    """Make targets for the images the findings' agents run in, in order."""
    targets = ["base", "mockgw"]
    for finding in findings:
        image = get_adapter(finding.agent_meta.agent_id).image  # e.g. reprobe/fakeagent:dev
        target = image.split("/", 1)[-1].split(":", 1)[0]
        if target not in targets:
            targets.append(target)
    return targets


def render_workflow(
    findings: Sequence[Finding],
    *,
    schedule: str = DEFAULT_SCHEDULE,
    suite_dir: str = "tests/reprobe",
    reprobe_ref: str | None = None,
    max_usd: float = 25.0,
    timeout_minutes: int = 120,
) -> str:
    if not findings:
        raise ConfigError("no findings to export")
    _check_schedule(schedule)
    if "${{" in suite_dir:
        raise ConfigError(f"suite_dir {suite_dir!r} contains an Actions expression")
    env = _env()
    env.filters["comment"] = _doc
    env.filters["yamlstr"] = json.dumps
    return env.get_template("workflow.yml.j2").render(
        findings=list(findings),
        schedule=schedule,
        suite_dir=suite_dir,
        reprobe_ref=_resolve_ref(reprobe_ref),
        repository=REPROBE_REPOSITORY,
        source_dir=SOURCE_DIR,
        images=_images(findings),
        required_env=_required_env(findings),
        max_usd=max_usd,
        timeout_minutes=timeout_minutes,
    )


def write_workflow(
    findings: Sequence[Finding],
    dest: Path,
    *,
    schedule: str = DEFAULT_SCHEDULE,
    suite_dir: str = "tests/reprobe",
    reprobe_ref: str | None = None,
    max_usd: float = 25.0,
    timeout_minutes: int = 120,
) -> Path:
    text = render_workflow(
        findings,
        schedule=schedule,
        suite_dir=suite_dir,
        reprobe_ref=reprobe_ref,
        max_usd=max_usd,
        timeout_minutes=timeout_minutes,
    )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


__all__ = [
    "DEFAULT_SCHEDULE",
    "REPROBE_REPOSITORY",
    "SOURCE_DIR",
    "render_workflow",
    "write_workflow",
]
