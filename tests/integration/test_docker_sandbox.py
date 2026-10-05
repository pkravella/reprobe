"""The real sandbox guarantees (R3), tested with probe adapters -- no agent, no
API key, no spend.

Marked `docker`: deselected by default, skipped unless REPROBE_DOCKER_TESTS=1.
Build first with `make base mockgw`.
"""

import docker
import pytest

from reprobe.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.docker


def _as(spec, probe_id):
    """Point a spec at a probe adapter."""
    return spec.model_copy(update={"agent": spec.agent.model_copy(update={"id": probe_id})})


def _sandbox():
    # Every probe runs on the base image.
    return DockerSandbox(
        images={
            pid: "reprobe/base:dev"
            for pid in (
                "probe-curl",
                "probe-env",
                "probe-true",
                "probe-sleep",
                "probe-write",
                "probe-fail",
            )
        }
    )


def _text(result) -> str:
    return "\n".join(e.attrs.get("text", "") for e in result.trace.of_kind("agent_message"))


def test_describe_reports_image_ids():
    described = DockerSandbox(images={"x": "reprobe/base:dev"}).describe()
    assert described["runtime"] == "docker"
    assert described["images"]["reprobe/mockgw:dev"].startswith("sha256:")


def test_the_network_is_internal_so_the_agent_cannot_reach_the_internet(minimal_spec, probes):
    # The strongest guarantee in the project, tested directly: a probe that
    # ignores the proxy and dials a real host must fail. curl prints 000 when
    # it cannot connect.
    result = _sandbox().run(_as(minimal_spec, "probe-curl"))
    assert result.ok, result.harness_error
    assert "000" in _text(result), f"expected an unreachable host, got: {_text(result)!r}"


def test_no_unlisted_host_env_var_reaches_the_container(minimal_spec, probes, monkeypatch):
    monkeypatch.setenv("REPROBE_SECRET_CANARY", "must-not-leak")
    result = _sandbox().run(_as(minimal_spec, "probe-env"))
    assert result.ok, result.harness_error
    assert "must-not-leak" not in _text(result)


def test_the_proxy_pointers_do_reach_the_container(minimal_spec, probes):
    # The complement of the isolation test: the agent is given a proxy, just
    # one that only reaches the gateway.
    result = _sandbox().run(_as(minimal_spec, "probe-env"))
    assert "HTTP_PROXY=" in _text(result)


def test_containers_and_network_are_removed_after_a_trial(minimal_spec, probes):
    client = docker.from_env()
    spec = _as(minimal_spec, "probe-true")
    _sandbox().run(spec)
    assert not any(spec.trial_id in c.name for c in client.containers.list(all=True))
    assert not any(spec.trial_id in n.name for n in client.networks.list())


def test_a_timeout_is_a_harness_error_not_a_pass(minimal_spec, probes):
    slow = minimal_spec.scenario.model_copy(
        update={"limits": minimal_spec.scenario.limits.model_copy(update={"max_trial_seconds": 2})}
    )
    spec = _as(minimal_spec.model_copy(update={"scenario": slow}), "probe-sleep")
    result = _sandbox().run(spec)
    assert not result.ok
    assert "exceeded" in (result.harness_error or "")


def test_a_timeout_still_removes_the_container(minimal_spec, probes):
    client = docker.from_env()
    slow = minimal_spec.scenario.model_copy(
        update={"limits": minimal_spec.scenario.limits.model_copy(update={"max_trial_seconds": 2})}
    )
    spec = _as(minimal_spec.model_copy(update={"scenario": slow}), "probe-sleep")
    _sandbox().run(spec)
    assert not any(spec.trial_id in c.name for c in client.containers.list(all=True))


def test_workspace_writes_show_up_in_the_fs_diff(minimal_spec, probes):
    result = _sandbox().run(_as(minimal_spec, "probe-write"))
    assert "PROBE.md" in result.fs_diff.created


def test_an_agent_reported_failure_becomes_a_harness_error(minimal_spec, probes):
    # error_from -> harness_error: an in-band failure must not read as a clean
    # trial with no violations.
    result = _sandbox().run(_as(minimal_spec, "probe-fail"))
    assert not result.ok
    assert "probe failure" in (result.harness_error or "")


def test_strace_observed_the_agents_real_syscalls(minimal_spec, probes):
    # The sandbox wires the syscall observer: a probe that writes a file should
    # produce file_write events in the merged trace from more than one observer.
    result = _sandbox().run(_as(minimal_spec, "probe-write"))
    sources = {e.source for e in result.trace.events}
    assert "strace" in sources
    assert "fsdiff" in sources


def test_the_workspace_is_a_fresh_copy_each_trial(minimal_spec, probes):
    # The fixture is materialised per trial, so a write in one does not bleed
    # into the next.
    sandbox = _sandbox()
    first = sandbox.run(_as(minimal_spec, "probe-write"))
    second = sandbox.run(_as(minimal_spec, "probe-write"))
    assert "PROBE.md" in first.fs_diff.created
    assert "PROBE.md" in second.fs_diff.created


# --- the egress carve-out (option 1) --------------------------------------


def test_with_no_infra_hosts_there_is_no_egress_network(minimal_spec, probes):
    # The free fake-agent gate: maximum isolation, no second network at all.
    import docker

    client = docker.from_env()
    before = {n.name for n in client.networks.list()}
    DockerSandbox(images={"probe-true": "reprobe/base:dev"}).run(_as(minimal_spec, "probe-true"))
    after = {n.name for n in client.networks.list()}
    assert not any("egress" in n for n in after - before)


def test_with_infra_hosts_the_agent_still_cannot_reach_the_internet(minimal_spec, probes):
    # The dual-homing must not weaken the agent's isolation: even with an egress
    # network present (for the gateway), the agent is on the internal net only,
    # so a direct dial of a raw IP still fails.
    sandbox = DockerSandbox(images={"probe-curl": "reprobe/base:dev"}, infra_hosts=("example.com",))
    result = sandbox.run(_as(minimal_spec, "probe-curl"))
    assert result.ok, result.harness_error
    assert "000" in _text(result), f"agent reached the internet: {_text(result)!r}"


def test_the_egress_network_is_removed_after_the_trial(minimal_spec, probes):
    import docker

    client = docker.from_env()
    spec = _as(minimal_spec, "probe-true")
    DockerSandbox(images={"probe-true": "reprobe/base:dev"}, infra_hosts=("example.com",)).run(spec)
    assert not any(spec.trial_id in n.name for n in client.networks.list())
