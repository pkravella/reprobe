"""Build the repo the agent works in, and diff it afterwards.

The fixture is honest work; the surfaces are the attacker's. `git init` plus one
commit matters more than it looks: coding agents behave differently in a repo
with no history, and the PRD's target is GitHub-style repository tasks.

The manifest is the authoritative answer to "what changed", because a diff
cannot miss a write the way a syscall filter or an agent's self-report can.
That authority is why it records symlinks too: creating one is a filesystem
change, and pointing a link at a protected path is a standard evasion.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from reprobe.errors import HarnessError, ScenarioError
from reprobe.ids import digest_bytes
from reprobe.sandbox import FsDiff, TrialSpec
from reprobe.scenario import PAYLOAD_SLOT

_IGNORED_PREFIXES = (".git/",)

# Namespaced so a symlink's manifest entry can never collide with the hash of a
# regular file that happens to contain the same bytes as the link target.
_SYMLINK_TAG = b"\x00reprobe-symlink\x00"

_NON_KEY_CHARS = re.compile(r"[^A-Za-z0-9]")


class WorkspaceManifest(BaseModel):
    model_config = {"frozen": True}

    files: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, root: Path) -> WorkspaceManifest:
        files: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(_IGNORED_PREFIXES):
                continue
            # Symlinks first: `is_file()` follows the link, so a link to a host
            # file would otherwise be read straight into the manifest, and a
            # broken link would vanish from it entirely. Verified on CPython
            # 3.11 that `rglob` does not recurse into a symlinked directory, so
            # a link to / cannot pull the host in and a loop cannot hang us.
            if path.is_symlink():
                files[rel] = digest_bytes(_SYMLINK_TAG + os.readlink(path).encode())
                continue
            if not path.is_file():
                continue
            files[rel] = digest_bytes(path.read_bytes())
        return cls(files=files)

    def diff(self, after: WorkspaceManifest) -> FsDiff:
        before_keys, after_keys = set(self.files), set(after.files)
        return FsDiff(
            created=sorted(after_keys - before_keys),
            deleted=sorted(before_keys - after_keys),
            modified=sorted(
                key for key in before_keys & after_keys if self.files[key] != after.files[key]
            ),
        )


def _env_key(canary_id: str) -> str:
    """`api_key` -> `API_KEY`. Keeps a planted `.env` file actually parseable."""
    return _NON_KEY_CHARS.sub("_", canary_id).upper()


def _check(spec: TrialSpec) -> None:
    """Reject an unrunnable spec before touching the filesystem.

    Validating after the copy would leave a half-built workspace behind on the
    error path, which the caller then has to clean up or mistake for a real one.
    """
    # One definition, on the Scenario, because `run_trial` needs the same check
    # for the fake-sandbox lane, which never gets here. Keeping it here as well
    # covers a spec handed straight to the sandbox -- an exported regression
    # test does exactly that.
    spec.scenario.check_payloads(spec.payloads)

    for canary in spec.canaries:
        if canary.spec.kind == "file" and not canary.spec.path:
            # Silently skipping this is the dangerous option: an unplanted
            # canary makes canary_read and canary_exfil unfirable, so the trial
            # reports clean. A false negative is the worst outcome here.
            raise ScenarioError(f"canary {canary.spec.id!r} is kind 'file' but has no path")


def materialise(spec: TrialSpec, dest: Path) -> WorkspaceManifest:
    """Copy the fixture, render the surfaces, plant the canaries, init git."""
    _check(spec)

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(spec.scenario.fixture_dir, dest, dirs_exist_ok=True)

    for surface in spec.scenario.surfaces:
        if surface.kind != "file":
            # Nothing renders the other kinds yet. Skipping one that was given a
            # payload would drop the attack silently and report a clean trial,
            # so say so instead. The loader refuses these too; this guard is for
            # scenarios built in code, such as the environment shrinker's.
            if spec.payloads.get(surface.id):
                raise ScenarioError(
                    f"surface {surface.id!r} has kind {surface.kind!r}, which the harness "
                    "cannot deliver a payload into yet"
                )
            continue
        payload = spec.payloads.get(surface.id, "")
        target = dest / surface.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            surface.template.replace(PAYLOAD_SLOT, payload), encoding=surface.encoding
        )

    for canary in spec.canaries:
        # `env` canaries reach the container as environment variables and
        # `tool_output` ones are injected by the gateway, so there is nothing
        # to write for either. Task 13 owes the env var.
        if canary.spec.kind != "file":
            continue
        target = dest / str(canary.spec.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{_env_key(canary.spec.id)}={canary.value}\n")

    _git_init(dest)
    return WorkspaceManifest.of(dest)


def _git_init(root: Path) -> None:
    # Resolved from the ambient PATH, then executed with a stripped one: the
    # point of the clean environment is deterministic git *config*, not making
    # the binary's location a guess. A host with git only in /opt/homebrew/bin
    # would fail against a hardcoded PATH.
    git = shutil.which("git")
    if git is None:
        raise HarnessError("git not found on PATH; cannot initialise the trial workspace")

    env = {
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@reprobe.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@reprobe.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    for args in (
        ["init", "--initial-branch=main", "-q"],
        ["add", "-A"],
        # --allow-empty so the "exactly one commit" invariant holds even for a
        # degenerate scenario with an empty fixture and no file surfaces.
        ["commit", "-q", "--allow-empty", "-m", "initial fixture"],
    ):
        done = subprocess.run(
            [git, "-C", str(root), *args], env=env, capture_output=True, text=True
        )
        if done.returncode != 0:
            raise HarnessError(
                f"git {args[0]} failed in {root} (exit {done.returncode}): "
                f"{done.stderr.strip() or done.stdout.strip()}"
            )
