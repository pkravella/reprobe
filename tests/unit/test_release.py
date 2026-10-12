"""The release: what gets published, by whom, and only when it should.

A release is the one thing this project does that cannot be taken back, so the
workflow's shape is tested rather than trusted: only a version tag triggers it,
the tag must match the package version, the checks run before anything is
built, only the publish job can mint a PyPI token, and it runs in the `pypi`
environment whose required reviewer is the last gate.
"""

import re
import tomllib
from pathlib import Path

import yaml

from reprobe import __version__

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _runs(job):
    return " ".join(str(step.get("run", "")) for step in job["steps"])


def test_the_two_version_strings_agree():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


def test_only_a_version_tag_triggers_a_release():
    triggers = _workflow()[True]  # PyYAML reads `on:` as True
    assert triggers == {"push": {"tags": ["v*"]}}


def test_the_token_is_read_only_unless_a_job_says_otherwise():
    assert _workflow()["permissions"] == {"contents": "read"}


def test_only_the_publish_job_can_mint_a_pypi_token():
    jobs = _workflow()["jobs"]
    minting = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    ]
    assert minting == ["publish"]


def test_publishing_waits_in_the_pypi_environment():
    """The environment PyPI's trusted publisher names, and where the required
    reviewer approves the upload."""
    publish = _workflow()["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert "uv publish --trusted-publishing always" in _runs(publish)


def test_the_tag_must_match_the_package_version_before_anything_is_built():
    """A `v0.1.1` tag on a commit still saying 0.1.0 would publish the wrong
    version, permanently."""
    build = _workflow()["jobs"]["build"]
    runs = [str(s.get("run", "")) for s in build["steps"]]
    guard = next(i for i, r in enumerate(runs) if "GITHUB_REF_NAME" in r and "__version__" in r)
    built = next(i for i, r in enumerate(runs) if "uv build" in r)
    assert guard < built


def test_the_checks_run_before_the_build():
    runs = [str(s.get("run", "")) for s in _workflow()["jobs"]["build"]["steps"]]
    built = next(i for i, r in enumerate(runs) if "uv build" in r)
    for check in ("ruff check", "ruff format --check", "mypy", "pytest tests/unit"):
        position = next((i for i, r in enumerate(runs) if check in r), None)
        assert position is not None and position < built, check


def test_the_jobs_run_in_order_and_hand_over_the_built_files():
    jobs = _workflow()["jobs"]
    assert jobs["publish"]["needs"] == "build"
    assert jobs["github-release"]["needs"] == "publish"
    uses = {name: [s.get("uses", "") for s in job["steps"]] for name, job in jobs.items()}
    assert any(u.startswith("actions/upload-artifact@") for u in uses["build"])
    assert any(u.startswith("actions/download-artifact@") for u in uses["publish"])


def test_the_github_release_attaches_the_published_files():
    release = _workflow()["jobs"]["github-release"]
    assert release["permissions"] == {"contents": "write"}
    assert "gh release create" in _runs(release)
    assert "dist/*" in _runs(release)


def test_no_secret_is_needed_to_publish():
    """Trusted publishing: PyPI trusts the workflow's identity, not a token."""
    assert "secrets." not in WORKFLOW.read_text()


def test_a_release_readme_installs_from_pypi():
    if ".dev" not in __version__:
        text = (ROOT / "README.md").read_text()
        assert "uv tool install reprobe-agents" in text
        assert "install from source" not in text.lower()


def test_the_changelog_has_a_section_for_this_version():
    text = (ROOT / "CHANGELOG.md").read_text()
    if ".dev" not in __version__:
        assert re.search(
            rf"^## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, flags=re.M
        )
        assert (
            f"[{__version__}]: https://github.com/pkravella/reprobe/releases/tag/v{__version__}"
            in text
        )
        assert "Planned for" not in text


def test_the_licence_and_notice_ship_with_the_package():
    """Found by building the wheel: neither LICENSE nor NOTICE was in it, so
    the package would have gone out without its licence text or the
    third-party attribution NOTICE carries."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert set(pyproject["project"].get("license-files", [])) >= {"LICENSE", "NOTICE"}
    runs = " ".join(str(s.get("run", "")) for s in _workflow()["jobs"]["build"]["steps"])
    assert "licenses/LICENSE" in runs and "licenses/NOTICE" in runs


def test_no_licence_classifier_alongside_the_spdx_expression():
    """PEP 639: with `License-Expression` set, a `License ::` classifier is
    deprecated, and tools are allowed to reject the pair. The build backend
    warned about it. A one-shot upload is no place to find out which tools do."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["license"] == "Apache-2.0"
    assert not [c for c in pyproject["project"]["classifiers"] if c.startswith("License ::")]


# --- the steps themselves, run rather than read -----------------------------------


def _step(job, needle):
    return next(
        s["run"] for s in _workflow()["jobs"][job]["steps"] if needle in str(s.get("run", ""))
    )


def _run_wheel_check(tmp_path, listing):
    """Run the wheel-content step against a faked `unzip -l` listing."""
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "unzip").write_text(f"#!/bin/sh\ncat <<'EOF'\n{listing}\nEOF\n")
    (bin_dir / "unzip").chmod(0o755)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "x.whl").write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "-e", "-c", _step("build", "unzip -l")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


GOOD_WHEEL = "\n".join(
    [
        "reprobe/export/templates/test_finding.py.j2",
        "reprobe/export/templates/workflow.yml.j2",
        "reprobe/export/templates/report.html.j2",
        "reprobe_agents-0.1.0.dist-info/licenses/LICENSE",
        "reprobe_agents-0.1.0.dist-info/licenses/NOTICE",
    ]
)


def test_the_wheel_check_passes_a_good_wheel(tmp_path):
    assert _run_wheel_check(tmp_path, GOOD_WHEEL).returncode == 0


def test_the_wheel_check_fails_a_wheel_that_ships_the_distribution_name_as_a_package(tmp_path):
    """`! grep -q` under `bash -e` never fails a step: -e ignores a negated
    command, so this guard was green whatever the wheel contained."""
    result = _run_wheel_check(tmp_path, GOOD_WHEEL + "\nreprobe_agents/__init__.py")
    assert result.returncode != 0


def test_the_wheel_check_fails_a_wheel_without_its_licence(tmp_path):
    listing = GOOD_WHEEL.replace("licenses/NOTICE", "nothing")
    assert _run_wheel_check(tmp_path, listing).returncode != 0


def _notes(tmp_path, changelog):
    import os
    import subprocess

    (tmp_path / "CHANGELOG.md").write_text(changelog)
    env = {**os.environ, "GITHUB_REF_NAME": "v0.1.0", "GITHUB_REPOSITORY": "o/r"}
    result = subprocess.run(
        ["bash", "-e", "-c", _step("build", "CHANGELOG.md")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    notes = tmp_path / "notes.md"
    return result, notes.read_text() if notes.exists() else ""


def test_release_notes_are_the_section_alone_with_links_pinned_to_the_tag(tmp_path):
    result, notes = _notes(tmp_path, (ROOT / "CHANGELOG.md").read_text())
    assert result.returncode == 0, result.stderr
    assert "## [0.1.0]" not in notes
    assert not re.search(r"^\[[^\]]+\]: ", notes, flags=re.M), "link reference lines leaked in"
    assert not [t for t in re.findall(r"\]\(([^)]+)\)", notes) if "://" not in t]
    assert "blob/v0.1.0/" in notes


def test_a_tag_with_no_changelog_section_fails_before_anything_is_published(tmp_path):
    """The notes used to be built after the PyPI upload, so a missing section
    failed the run only once the package was already public."""
    result, _ = _notes(tmp_path, "# Changelog\n\n## [0.0.9] - 2026-01-01\n\n- old\n")
    assert result.returncode != 0
    jobs = _workflow()["jobs"]
    assert "CHANGELOG.md" not in _runs(jobs["github-release"])
    assert any("notes.md" in str(s.get("with", {}).get("path", "")) for s in jobs["build"]["steps"])


def test_only_a_commit_on_main_can_be_released():
    """A `v*` tag on a branch commit that happens to carry the version would
    otherwise publish code main never had."""
    build = _workflow()["jobs"]["build"]
    checkout = next(
        s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    runs = [str(s.get("run", "")) for s in build["steps"]]
    guard = next(
        i for i, r in enumerate(runs) if "merge-base --is-ancestor" in r and "origin/main" in r
    )
    assert guard < next(i for i, r in enumerate(runs) if "uv build" in r)
