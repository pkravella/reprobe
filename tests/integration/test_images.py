"""The images build, run unprivileged, and produce a parseable syscall log.

Marked `docker`: deselected by default, and skipped unless
REPROBE_DOCKER_TESTS=1. Build them first with `make images`.
"""

import json
import subprocess

import pytest

pytestmark = pytest.mark.docker

BASE = "reprobe/base:dev"
MOCKGW = "reprobe/mockgw:dev"
CLAUDE = "reprobe/claude-code:dev"
IMAGES = [BASE, MOCKGW, CLAUDE]

ENTRYPOINT = "/usr/local/bin/reprobe-entrypoint"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("docker", *args), capture_output=True, text=True)


def _ok(*args: str) -> str:
    done = _docker(*args)
    if done.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args)}\nexit={done.returncode}\n"
            f"--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}"
        )
    return done.stdout


def _sh(script: str, *, image: str = BASE, run_args: tuple[str, ...] = ()) -> str:
    """Run a shell snippet in `image`, bypassing the strace entrypoint."""
    return _ok("run", "--rm", *run_args, "--entrypoint", "sh", image, "-lc", script)


def _trace_report(command: str, *, run_args: tuple[str, ...] = ()) -> dict[str, int]:
    """Run `command` through the real entrypoint and summarise its strace log.

    Everything is measured inside the one container that produced the log. A
    second `docker run` would get a fresh filesystem and an empty log.
    """
    script = (
        f"{ENTRYPOINT} {command} >/dev/null 2>&1; "
        "echo pids=$(awk '{print $1}' /tmp/strace.log | sort -u | wc -l); "
        "echo openats=$(grep -c openat /tmp/strace.log); "
        "echo lines=$(wc -l < /tmp/strace.log); "
        "echo hostname_paths=$(grep -c /etc/hostname /tmp/strace.log)"
    )
    out = _sh(script, run_args=("-e", "REPROBE_STRACE_OUT=/tmp/strace.log", *run_args))
    return {key: int(value) for key, value in (line.split("=") for line in out.split())}


@pytest.mark.parametrize("image", IMAGES)
def test_image_exists(image):
    _ok("image", "inspect", image)


def test_base_image_has_strace_and_runs_unprivileged():
    out = _sh("id -un && strace --version | head -1")
    assert out.splitlines()[0] == "agent"
    assert "strace" in out


def test_base_image_satisfies_the_agent_cli_declared_node_engine():
    # @anthropic-ai/claude-code 2.1.289 declares engines node>=22.0.0; Debian
    # bookworm's own nodejs is 18.20.4, so the base is a node:<major>-bookworm-slim
    # image. This asserts the constraint, not a version, so a Dependabot major
    # bump does not produce a spurious failure.
    # Measured: Node 18 installs with only an EBADENGINE warning and runs
    # `--help` and `-p --output-format json` indistinguishably, so this is not a
    # break -- it pins a deliberate choice to stay on a supported runtime rather
    # than carry an unsupported-engine variable into a 100-trial gate.
    out = _ok("run", "--rm", "--entrypoint", "node", BASE, "--version")
    assert int(out.strip().lstrip("v").split(".")[0]) >= 22, out


def test_the_agent_user_can_write_to_its_working_directories():
    # The entrypoint's strace log lands in /reprobe, and Task 8 materialises the
    # fixture into /workspace. Both are root-created, so without an explicit
    # chown the unprivileged agent cannot write to either.
    assert _sh("touch /reprobe/w /workspace/w && echo writable").strip() == "writable"


def test_entrypoint_produces_a_syscall_log():
    assert _trace_report("/bin/sh -c 'cat /etc/hostname'")["lines"] > 0


def test_strace_needs_no_added_capability():
    # The plan called --cap-add SYS_PTRACE "the only added capability". It is not
    # needed: strace forks the agent and traces a direct child via
    # PTRACE_TRACEME, which standard permissions allow. This asserts the
    # strongest form -- every capability dropped -- because the sandbox's whole
    # purpose is to withhold privilege, and a host whose ptrace policy breaks
    # this should fail loudly here rather than silently inside a trial.
    report = _trace_report("/bin/sh -c 'cat /etc/hostname'", run_args=("--cap-drop", "ALL"))
    assert report["lines"] > 0


def test_strace_follows_forks_and_decodes_paths():
    # -f and -yy are what make the log usable to Task 10: a fork tree must be
    # followed, and an openat must show the resolved path, not a bare fd.
    tree = "/bin/sh -c 'for i in 1 2 3; do (sh -c \"cat /etc/hostname\" >/dev/null); done'"
    report = _trace_report(tree, run_args=("--cap-drop", "ALL"))
    assert report["pids"] > 1, "strace -f did not follow forks"
    assert report["openats"] > 0
    assert report["hostname_paths"] > 0, "strace -yy did not record the resolved path"


def test_entrypoint_refuses_an_empty_command():
    done = _docker("run", "--rm", BASE)
    assert done.returncode == 64
    assert "no agent command given" in done.stderr


def test_entrypoint_execs_the_agent_so_its_exit_code_survives():
    # The adapter reads the agent's exit status. strace must not swallow it, or
    # a crashed agent reads as a clean trial.
    done = _docker(
        "run", "--rm", "-e", "REPROBE_STRACE_OUT=/tmp/s.log", BASE, "/bin/sh", "-c", "exit 42"
    )
    assert done.returncode == 42, done.stderr


def test_the_agent_image_records_a_usable_version_string():
    # R11 pins every exported finding to this string, so it must be a version,
    # not an error message that a `|| true` let through.
    out = _ok("run", "--rm", "--entrypoint", "cat", CLAUDE, "/home/agent/.reprobe-agent-version")
    assert out.strip(), "version file is empty"
    assert out.strip()[0].isdigit(), f"not a version string: {out!r}"


def test_images_report_an_id_a_finding_can_be_pinned_to():
    for image in IMAGES:
        raw = _ok("image", "inspect", image, "--format", "{{json .Id}}")
        assert json.loads(raw).startswith("sha256:")
