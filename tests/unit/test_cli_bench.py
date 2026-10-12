from typer.testing import CliRunner

from reprobe import cli
from reprobe.sandbox.fake import FakeSandbox
from tests.support.results import clean_result, leaky_result

runner = CliRunner()
MINIMAL = "tests/data/scenarios/minimal/scenario.yaml"


def _sandbox(spec):
    if "POST" in "\n".join(spec.payloads.values())[:300]:
        return leaky_result(canary=spec.canaries[0].value)
    return clean_result()


def _bench(tmp_path, monkeypatch, *args):
    built = []

    def factory(*a, **k):
        built.append(k.get("infra_hosts"))
        return FakeSandbox(_sandbox)

    monkeypatch.setattr(cli, "DockerSandbox", factory)
    result = runner.invoke(
        cli.app,
        [
            "bench",
            "--scenario",
            MINIMAL,
            "--trials",
            "12",
            "--seeds",
            "1,2",
            "--concurrency",
            "1",
            "--store",
            str(tmp_path / "store"),
            *args,
        ],
    )
    return result, built


def test_bench_writes_the_report_and_prints_the_headline(tmp_path, monkeypatch):
    out = tmp_path / "arm-a.md"
    result, built = _bench(tmp_path, monkeypatch, "--out", str(out))
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert text.startswith("# Reprobe benchmark")
    assert "ratio:" in result.output
    assert len(built) == 4  # a fresh sandbox per arm
    assert all(hosts == ("api.anthropic.com",) for hosts in built)


def test_bench_takes_several_scenarios_and_a_profile(tmp_path, monkeypatch):
    result, _ = _bench(
        tmp_path,
        monkeypatch,
        "--scenario",
        MINIMAL,
        "--agent",
        "fake-agent",
        "--model",
        "reprobe-fake",
        "--agent-profile",
        "flaky_60",
        "--out",
        str(tmp_path / "r.md"),
    )
    assert result.exit_code == 0, result.output
    assert "profile `flaky_60`" in (tmp_path / "r.md").read_text()
    assert "2 scenario(s)" in (tmp_path / "r.md").read_text()


def test_bench_refuses_to_overwrite_the_gate_record(tmp_path, monkeypatch):
    """The plan's command wrote over docs/benchmark-v0.1.md, which holds the
    Phase 1-3 gate measurements."""
    out = tmp_path / "benchmark-v0.1.md"
    out.write_text("# Benchmark -- v0.1\n\ngate record\n")
    result, _ = _bench(tmp_path, monkeypatch, "--out", str(out))
    assert result.exit_code == 2
    assert "exists" in result.output
    assert out.read_text().endswith("gate record\n")


def test_bench_rejects_bad_seeds_and_profiles_cleanly(tmp_path, monkeypatch):
    for args, word in ((["--seeds", "1,x"], "seeds"), (["--agent-profile", "nope"], "nope")):
        result, _ = _bench(tmp_path, monkeypatch, *args, "--out", str(tmp_path / "r.md"))
        assert result.exit_code == 2, result.output
        assert word in result.output
        assert isinstance(result.exception, SystemExit)


def test_bench_with_an_unknown_agent_is_a_clean_error(tmp_path, monkeypatch):
    result, _ = _bench(tmp_path, monkeypatch, "--agent", "nope", "--out", str(tmp_path / "r.md"))
    assert result.exit_code == 2
    assert "nope" in result.output
    assert not (tmp_path / "r.md").exists()


def test_bench_refuses_an_unwritable_report_before_spending_anything(tmp_path, monkeypatch):
    """The report was written only after the whole run; a bad --out lost it."""
    (tmp_path / "out").mkdir()
    result, built = _bench(tmp_path, monkeypatch, "--out", str(tmp_path / "out"), "--force")
    assert result.exit_code == 2
    assert built == []
