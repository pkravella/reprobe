"""R3: one disposable container per trial, on an internal network.

Network shape::

    reprobe-net-<trial_id>   internal=True  (no route off the host)
      ├── mockgw             the only reachable peer; also the DNS sinkhole
      └── agent              HTTP(S)_PROXY and DNS both point at mockgw

Guarantees this enforces, each with a test in
``tests/integration/test_docker_sandbox.py``:

* The agent container has no route to the internet even if it ignores the proxy
  env vars, because the network is ``internal`` and DNS resolves everything to
  the gateway, which only listens on its own ports.
* No host env var reaches the container unless the adapter's ``env_allowlist``
  names it.
* The container and the network are removed after the trial, including on
  timeout.
* A timeout, a docker error, an agent-reported failure, or empty output
  produces a ``TrialResult`` with ``harness_error`` set -- never a silent pass.

The corrections gathered while Tasks 8-12 and 34 were built are implemented
here, not just described: ``env`` canaries are planted into the environment,
the injected secret values are handed to the gateway's ``REPROBE_REDACT`` so
the agent's own key never lands in a stored log, ``adapter.error_from`` is
mapped onto ``harness_error``, ``adapter.login_command`` is run first when it is
not ``None``, ``stdin_open`` is ``False`` so ``codex exec`` does not hang
waiting on stdin, and the container drops every capability (strace needs none).

One piece is deliberately **not** here: the infra-host CONNECT tunnel that lets
a *real* agent reach its model API (Task 9's option-1 decision). That is a
gateway change, it serves only the optional paid-agent leg, and the free
Phase-1 gate runs on the fake agent, which needs no egress. ``infra_hosts`` is
threaded through to the gateway so that change is a drop-in; until it lands a
real agent cannot reach its API and the resulting failure is surfaced as a
``harness_error``, never a clean pass.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, NotFound
from docker.models.containers import Container

from reprobe.agents import get_adapter
from reprobe.agents.base import AgentAdapter, AgentMeta
from reprobe.errors import HarnessError
from reprobe.ids import digest
from reprobe.observers.egress import EgressObserver
from reprobe.observers.fsdiff import FsDiffObserver
from reprobe.observers.syscalls import SyscallObserver
from reprobe.sandbox import FsDiff, TrialResult, TrialSpec
from reprobe.sandbox.workspace import WorkspaceManifest, materialise
from reprobe.trace import Trace

MOCKGW_IMAGE = "reprobe/mockgw:dev"
_STRACE_PATH = "/reprobe/strace.log"
_EGRESS_PATH = "/reprobe/egress.jsonl"


def compose_command(login: list[str] | None, agent_cmd: list[str]) -> list[str]:
    """What the image entrypoint should run: the agent, after an optional login.

    The image entrypoint wraps whatever it is given in strace, so for the
    common case this is just the agent argv. When the adapter needs a login
    step (codex pipes its key into ``codex login`` on stdin before ``exec``),
    the two are composed into one shell command so they share a filesystem and
    the login's credentials are there for the agent. ``exec`` on the agent so
    its exit status is the container's, not the shell's.
    """
    if not login:
        return agent_cmd
    return ["sh", "-lc", f"{shlex.join(login)} && exec {shlex.join(agent_cmd)}"]


class DockerSandbox:
    def __init__(
        self,
        images: dict[str, str] | None = None,
        *,
        keep_failed: bool = False,
        infra_hosts: tuple[str, ...] = (),
    ) -> None:
        self._client = docker.from_env()
        self._images = images or {}
        self._keep_failed = keep_failed
        self._infra_hosts = tuple(infra_hosts)
        self._version_cache: dict[str, str] = {}

    # ---------------------------------------------------------------- public

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"runtime": "docker", "images": {}}
        for name in sorted({*self._images.values(), MOCKGW_IMAGE}):
            try:
                out["images"][name] = self._client.images.get(name).id
            except (NotFound, DockerException):
                out["images"][name] = "missing"
        return out

    def run(self, spec: TrialSpec) -> TrialResult:
        started = time.time()
        workdir = Path(tempfile.mkdtemp(prefix=f"reprobe-{spec.trial_id}-"))
        try:
            result = self._run_inner(spec, workdir, started)
        except HarnessError as exc:
            result = self._harness_failure(spec, str(exc), started)
        except DockerException as exc:
            result = self._harness_failure(spec, f"docker error: {exc}", started)
        # keep_failed keeps the workspace of a FAILED trial for inspection, not
        # every trial's.
        if not (self._keep_failed and not result.ok):
            shutil.rmtree(workdir, ignore_errors=True)
        return result

    # --------------------------------------------------------------- private

    def _run_inner(self, spec: TrialSpec, workdir: Path, started: float) -> TrialResult:
        adapter = get_adapter(spec.agent.id)
        image = self._images.get(spec.agent.id, adapter.image)

        ws = workdir / "workspace"
        artefacts = workdir / "reprobe"
        artefacts.mkdir(parents=True)
        before = materialise(spec, ws)
        # The container's agent user is uid 1001; the host created these dirs,
        # so on a Linux daemon the agent cannot write to them without this.
        _make_writable(ws)
        _make_writable(artefacts)

        network = self._client.networks.create(
            f"reprobe-{spec.trial_id}", driver="bridge", internal=True
        )
        gateway: Container | None = None
        agent: Container | None = None
        try:
            gateway = self._client.containers.run(
                MOCKGW_IMAGE,
                detach=True,
                network=network.name,
                name=f"reprobe-gw-{spec.trial_id}",
                environment={
                    "REPROBE_ALLOWLIST": ",".join(spec.scenario.egress_allowlist),
                    "REPROBE_EGRESS_LOG": _EGRESS_PATH,
                    # Scrub the injected secret VALUES (the agent's own key)
                    # from everything logged. Canaries are NOT here -- a canary
                    # in the log is the whole point of canary_exfil.
                    "REPROBE_REDACT": ",".join(self._secrets(adapter)),
                    # Harness plumbing, kept separate from the scenario's
                    # egress_allowlist. The gateway's CONNECT tunnel for these
                    # is the deferred follow-up; the var is threaded now.
                    "REPROBE_INFRA_HOSTS": ",".join(self._infra_hosts),
                },
                volumes={str(artefacts): {"bind": "/reprobe", "mode": "rw"}},
            )
            gateway_ip = self._wait_for_gateway(gateway, network.name)

            command = compose_command(
                adapter.login_command(),
                adapter.command(
                    spec.agent,
                    task=spec.scenario.task,
                    max_usd=spec.scenario.limits.max_usd_per_trial,
                ),
            )
            agent = self._client.containers.run(
                image,
                command=command,
                detach=True,
                stdin_open=False,  # codex exec waits on stdin otherwise
                network=network.name,
                name=f"reprobe-agent-{spec.trial_id}",
                working_dir="/workspace",
                user="agent",
                cap_drop=["ALL"],  # strace needs no capability -- Task 7
                security_opt=["no-new-privileges"],
                pids_limit=512,
                mem_limit="4g",
                nano_cpus=2_000_000_000,
                dns=[gateway_ip],
                environment=self._environment(adapter, spec, gateway_ip),
                volumes={
                    str(ws): {"bind": "/workspace", "mode": "rw"},
                    str(artefacts): {"bind": "/reprobe", "mode": "rw"},
                },
            )
            exit_code, stdout = self._await(agent, spec.scenario.limits.max_trial_seconds)
            gateway_log = _read(artefacts / Path(_EGRESS_PATH).name)
            strace_log = _read(artefacts / Path(_STRACE_PATH).name)
        finally:
            for container in (agent, gateway):
                if container is not None:
                    _reap(container)
            with contextlib.suppress(DockerException):
                network.remove()

        after = WorkspaceManifest.of(ws)
        fs_diff = before.diff(after)

        egress = EgressObserver(gateway_log, spec.scenario.egress_allowlist, spec.canaries)
        agent_events, cost = adapter.parse_stdout(stdout, model=spec.agent.model)
        # The agent's stream carries no real timestamps, only an order; rebase
        # onto the trial's wall clock so it interleaves with the other
        # observers' real times under Trace.merge.
        rebased = [e.model_copy(update={"ts": started + e.ts}) for e in agent_events]
        trace = Trace.merge(
            spec.trial_id,
            Trace(trial_id=spec.trial_id, events=rebased),
            Trace(trial_id=spec.trial_id, events=SyscallObserver(strace_log).events()),
            Trace(trial_id=spec.trial_id, events=egress.events()),
            Trace(trial_id=spec.trial_id, events=FsDiffObserver(fs_diff, ts=time.time()).events()),
        )

        return TrialResult(
            trial_id=spec.trial_id,
            exit_code=exit_code,
            trace=trace,
            fs_diff=fs_diff,
            egress=egress.records(),
            cost=cost,
            agent_meta=AgentMeta(
                agent_id=adapter.id,
                agent_version=self._agent_version(image, adapter),
                model_id=spec.agent.model,
                prompt_hash=digest({"task": spec.scenario.task}, prefix="prompt"),
                container_digest=self._image_id(image),
            ),
            duration_s=time.time() - started,
            harness_error=self._harness_error(adapter, stdout, exit_code),
        )

    def _harness_error(self, adapter: AgentAdapter, stdout: str, exit_code: int) -> str | None:
        # Empty stdout means the output-format flag is wrong or the agent never
        # produced a stream; an in-band error (auth failure, budget abort) is
        # the adapter's to detect. Either is "no verdict", not a clean trial.
        if not stdout.strip():
            return f"agent produced no output (exit {exit_code})"
        return adapter.error_from(stdout)

    def _secrets(self, adapter: Any) -> list[str]:
        """The injected secret values, for the gateway to redact from its log."""
        return [value for key in adapter.env_allowlist if (value := os.environ.get(key))]

    def _environment(self, adapter: Any, spec: TrialSpec, gateway_ip: str) -> dict[str, str]:
        """Only what the adapter names, the proxy pointers, and env canaries.
        Nothing is inherited from the host."""
        proxy = f"http://{gateway_ip}:8080"
        env = {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "NO_PROXY": "",
            "REPROBE_STRACE_OUT": _STRACE_PATH,
            "REPROBE_CANARY_PATH": _file_canary_path(spec),
            "REPROBE_COLLECTOR": "http://collector.invalid",
            "HOME": "/home/agent",
        }
        # Plant env canaries: materialise plants only FILE canaries, because an
        # env canary is meant to arrive as a variable -- and if nothing put it
        # here its canary_read / canary_exfil checks could never fire, a silent
        # false negative. Before env_overrides so an explicit override wins.
        for canary in spec.canaries:
            if canary.spec.kind == "env" and canary.spec.env_var:
                env[canary.spec.env_var] = canary.value
        for key in adapter.env_allowlist:
            value = os.environ.get(key)
            if value:
                env[key] = value
        env.update(spec.env_overrides)
        return env

    def _await(self, container: Container, timeout: int) -> tuple[int, str]:
        exit_code = -1
        try:
            status = container.wait(timeout=timeout)
            exit_code = int(status.get("StatusCode", -1))
        except Exception:
            # docker-py raises ReadTimeout / ConnectionError when `wait` times
            # out. Kill the container and report the timeout as a harness error.
            with contextlib.suppress(DockerException):
                container.kill()
            exit_code = -9
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
        if exit_code == -9:
            raise HarnessError(f"trial exceeded {timeout}s and was killed")
        return exit_code, stdout

    def _wait_for_gateway(self, gateway: Container, network: str, attempts: int = 150) -> str:
        """Block until the gateway answers on :8080, and return its IP."""
        for _ in range(attempts):
            probe = gateway.exec_run(
                [
                    "python3",
                    "-c",
                    "import socket,sys;s=socket.socket();"
                    "sys.exit(s.connect_ex(('127.0.0.1',8080)))",
                ]
            )
            if probe.exit_code == 0:
                gateway.reload()
                nets = gateway.attrs["NetworkSettings"]["Networks"]
                return str(nets[network]["IPAddress"])
            time.sleep(0.1)
        raise HarnessError("mock gateway did not become ready")

    def _agent_version(self, image: str, adapter: Any) -> str:
        if image in self._version_cache:
            return self._version_cache[image]
        try:
            raw = self._client.containers.run(
                image,
                command=["cat", "/home/agent/.reprobe-agent-version"],
                remove=True,
                network_disabled=True,
                entrypoint="",
            ).decode("utf-8", "replace")
            version = str(adapter.version_from(raw))
        except DockerException:
            version = "unknown"
        self._version_cache[image] = version
        return version

    def _image_id(self, image: str) -> str:
        try:
            return str(self._client.images.get(image).id)
        except DockerException:
            return "unknown"

    def _harness_failure(self, spec: TrialSpec, message: str, started: float) -> TrialResult:
        return TrialResult(
            trial_id=spec.trial_id,
            exit_code=-1,
            trace=Trace(trial_id=spec.trial_id, events=[]),
            fs_diff=FsDiff(),
            agent_meta=AgentMeta(
                agent_id=spec.agent.id,
                agent_version="unknown",
                model_id=spec.agent.model,
                prompt_hash=digest({"task": spec.scenario.task}, prefix="prompt"),
            ),
            duration_s=time.time() - started,
            harness_error=message,
        )


def _file_canary_path(spec: TrialSpec) -> str:
    for canary in spec.canaries:
        if canary.spec.kind == "file" and canary.spec.path:
            return canary.spec.path
    return ".env"


def _make_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        with contextlib.suppress(OSError):
            path.chmod(path.stat().st_mode | 0o777)


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _reap(container: Container) -> None:
    with contextlib.suppress(DockerException):
        container.remove(force=True)
