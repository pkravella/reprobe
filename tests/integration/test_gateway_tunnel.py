"""The infra-host CONNECT tunnel (option 1).

A real agent reaches its model API over HTTPS, which a proxy serves with
CONNECT, and the trial network has no other route out. The gateway tunnels a
CONNECT to a host in REPROBE_INFRA_HOSTS and refuses every other CONNECT, so a
named, logged set of model-API hosts is reachable and nothing else is.

Tested without any real egress: a dummy TCP echo server stands in for the model
API on a shared network, and the assertions are that bytes flow to the infra
host and that a non-infra CONNECT is refused. TLS is not involved -- CONNECT
tunnels raw bytes, and the point under test is the tunnel, not what rides it.

Marked `docker`: deselected by default, skipped unless REPROBE_DOCKER_TESTS=1.
"""

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.docker

BASE = "reprobe/base:dev"
GATEWAY = "reprobe/mockgw:dev"

# A one-connection TCP echo server: reads a line, sends it back with a prefix.
# Stands in for an infra host (the model API) without needing TLS.
ECHO_SERVER = (
    "import socket;"
    "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
    "s.bind(('0.0.0.0',9000));s.listen();"
    "\nwhile True:\n"
    " c,_=s.accept();d=c.recv(1024);c.sendall(b'echo:'+d);c.close()"
)

# A client that drives the gateway as an HTTP proxy: sends CONNECT <target>,
# and on a 200 sends a line through the tunnel and prints what comes back.
# Prints the status line otherwise. Argv: gateway_ip, target.
CLIENT = (
    "import socket,sys;"
    "gw,target=sys.argv[1],sys.argv[2];"
    "s=socket.create_connection((gw,8080),timeout=10);"
    "s.sendall(('CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n'%(target,target)).encode());"
    "head=s.recv(128);"
    "sys.stdout.write(head.decode('latin-1','replace').splitlines()[0]+'\\n');"
    "\nif b'200' in head:\n"
    " s.sendall(b'ping-through-tunnel');"
    "sys.stdout.write(s.recv(1024).decode('latin-1','replace'))"
)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("docker", *args), capture_output=True, text=True)


def _ok(*args: str) -> str:
    done = _docker(*args)
    if done.returncode != 0:
        pytest.fail(f"docker {' '.join(args)}\nexit={done.returncode}\n{done.stderr}")
    return done.stdout


@pytest.fixture
def tunnel_setup(tmp_path):
    """A network with a dummy infra host, the gateway (infra-allowed), and a log."""
    suffix = uuid.uuid4().hex[:8]
    net = f"reprobe-tunnel-{suffix}"
    log_dir = tmp_path / "gw"
    log_dir.mkdir()
    _ok("network", "create", net)
    echo = _ok(
        "run",
        "-d",
        "--rm",
        "--network",
        net,
        "--network-alias",
        "infra.test",
        "--name",
        f"reprobe-echo-{suffix}",
        "--entrypoint",
        "python3",
        BASE,
        "-c",
        ECHO_SERVER,
    ).strip()
    gw = _ok(
        "run",
        "-d",
        "--rm",
        "--network",
        net,
        "--network-alias",
        "mockgw",
        "--name",
        f"reprobe-gw-{suffix}",
        "-e",
        "REPROBE_ALLOWLIST=registry.npmjs.org",
        "-e",
        "REPROBE_INFRA_HOSTS=infra.test",
        "-v",
        f"{log_dir}:/reprobe",
        GATEWAY,
    ).strip()
    # Wait for the gateway to answer.
    for _ in range(50):
        probe = _docker(
            "run",
            "--rm",
            "--network",
            net,
            "--entrypoint",
            "python3",
            BASE,
            "-c",
            "import socket,sys;sys.exit(socket.socket().connect_ex(('mockgw',8080)))",
        )
        if probe.returncode == 0:
            break
    else:
        pytest.fail(f"gateway never came up:\n{_docker('logs', gw).stdout}")
    try:
        yield net, log_dir
    finally:
        _docker("kill", gw)
        _docker("kill", echo)
        _docker("network", "rm", net)


def _client(net: str, target: str) -> str:
    return _ok(
        "run",
        "--rm",
        "--network",
        net,
        "--entrypoint",
        "python3",
        BASE,
        "-c",
        CLIENT,
        "mockgw",
        target,
    )


def _egress(log_dir):
    import json

    path = log_dir / "egress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_connect_to_an_infra_host_is_tunnelled_and_bytes_flow(tunnel_setup):
    net, log_dir = tunnel_setup
    out = _client(net, "infra.test:9000")
    assert "200 Connection established" in out
    assert "echo:ping-through-tunnel" in out, "bytes did not reach the infra host"
    record = next(r for r in _egress(log_dir) if r["method"] == "CONNECT")
    assert record["host"] == "infra.test"
    assert record["tunneled"] is True


def test_a_connect_to_a_non_infra_host_is_refused(tunnel_setup):
    net, log_dir = tunnel_setup
    out = _client(net, "attacker.example:443")
    assert "403" in out
    assert "echo:" not in out, "a non-infra host must not be tunnelled"
    record = next(r for r in _egress(log_dir) if r["host"] == "attacker.example")
    assert record["method"] == "CONNECT"
    assert record["tunneled"] is False


def test_an_allowlisted_host_is_not_automatically_an_infra_host(tunnel_setup):
    # egress_allowlist (what a scenario permits over HTTP) and infra hosts (the
    # model-API carve-out) are separate lists. npm is allowlisted for HTTP but
    # must NOT be CONNECT-tunnellable.
    net, log_dir = tunnel_setup
    out = _client(net, "registry.npmjs.org:443")
    assert "403" in out
    record = next(r for r in _egress(log_dir) if r["host"] == "registry.npmjs.org")
    assert record["tunneled"] is False
