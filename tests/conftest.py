"""Shared fixtures and the opt-in gates for the expensive test lanes.

`pyproject.toml` documents `docker` as "skipped unless REPROBE_DOCKER_TESTS=1"
and `agent` as "skipped unless REPROBE_AGENT_TESTS=1". `addopts` deselects both
markers by default, but a developer who runs `pytest -m docker` has overridden
that and would otherwise reach for a daemon, or spend money, without having
opted in. These hooks make the documented contract true.

If the lane IS opted into and the daemon is missing, the tests fail rather than
skip: a silent skip is how a broken sandbox ships green.
"""

import os

import pytest

_LANES = {
    "docker": ("REPROBE_DOCKER_TESTS", "needs a Docker daemon"),
    "agent": ("REPROBE_AGENT_TESTS", "spends real agent/API budget"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for marker, (env_var, why) in _LANES.items():
        if os.environ.get(env_var) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{why}; set {env_var}=1 to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)
