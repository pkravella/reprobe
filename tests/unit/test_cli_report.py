from typer.testing import CliRunner

from reprobe import cli
from reprobe.store import RunStore
from tests.support.findings import make_finding

runner = CliRunner()


def _store(tmp_path, *findings):
    store = RunStore(tmp_path / "store")
    run_id = store.open_run({"command": "fuzz", "scenario": "minimal"})
    for finding in findings:
        store.append(run_id, "findings", finding.to_record())
    return store, run_id


def _report(tmp_path, *args):
    return runner.invoke(cli.app, ["report", str(tmp_path / "store"), *args])


def test_report_writes_markdown(tmp_path):
    _store(tmp_path, make_finding())
    result = _report(tmp_path, "--out", str(tmp_path / "r.md"))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "r.md").read_text().startswith("# Reprobe findings")
    assert "r.md" in result.output


def test_report_writes_html_by_suffix(tmp_path):
    _store(tmp_path, make_finding())
    assert _report(tmp_path, "--out", str(tmp_path / "r.html")).exit_code == 0
    assert "<html" in (tmp_path / "r.html").read_text()


def test_report_reads_a_named_run(tmp_path):
    store, first = _store(tmp_path, make_finding(actions=["canary_read:/first"]))
    later = store.open_run({"command": "fuzz"})
    store.append(later, "findings", make_finding(actions=["canary_read:/later"]).to_record())
    assert _report(tmp_path, "--run", first, "--out", str(tmp_path / "r.md")).exit_code == 0
    text = (tmp_path / "r.md").read_text()
    assert "/first" in text and "/later" not in text


def test_report_on_an_empty_store_or_unknown_run_says_so(tmp_path):
    result = _report(tmp_path, "--out", str(tmp_path / "r.md"))
    assert result.exit_code == 2
    assert "no runs" in result.output
    _store(tmp_path, make_finding())
    result = _report(tmp_path, "--run", "run_nope", "--out", str(tmp_path / "r.md"))
    assert result.exit_code == 2
    assert "run_nope" in result.output
    assert not (tmp_path / "r.md").exists()


def test_report_of_a_run_with_no_findings_still_writes_one(tmp_path):
    """An empty report is a result -- it says why it is empty."""
    _store(tmp_path)
    assert _report(tmp_path, "--out", str(tmp_path / "r.md")).exit_code == 0
    assert "no candidates" in (tmp_path / "r.md").read_text().lower()
