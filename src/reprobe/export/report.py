"""R17: the report an engineer reads.

Markdown first, because it gets pasted into a PR or an issue, which is where
this conversation actually happens; HTML second, for sharing as a file. It
covers what the PRD promises -- run timeline, shrunk diff, rate, cost -- plus
the dropped candidates, because a report that hides the near-misses is not
honest about what the search found.

**Everything here is rendered where Markdown or HTML is live, and much of it
was produced by an agent under attack.** Payloads are adversarial by
definition, and action keys, timeline subjects and paths are things the agent
produced. So:

* a payload goes in a fence longer than any backtick run inside it (`fence`),
  because a fixed ``` fence ends at the first ``` in the payload and the rest
  renders as live Markdown -- raw HTML included, on GitHub;
* every other piece of data text goes through `md_inline`, which escapes all
  ASCII punctuation (so no table cell, link, emphasis, heading or tag can form)
  and makes non-printables visible;
* the HTML is autoescaped and carries a Content-Security-Policy that forbids
  every load, so even an escaping regression could not fetch or run anything.

**Evidence is matched exactly.** A violation records what it saw -- path and
timestamp for a canary read, host/port/method for egress, path for a protected
write, the command for a dangerous one -- and a timeline row is evidence only
when its event is that observation. The draft matched any event whose subject
contained the target, so a failed open of the same file was "evidence" too.
"""

from __future__ import annotations

import difflib
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reprobe.dedupe import FindingGroup, dedupe
from reprobe.export.pytest_export import _env
from reprobe.finding import Finding
from reprobe.stats import estimate
from reprobe.store import RunStore
from reprobe.trace import Event, Trace
from reprobe.triage import DroppedCandidate

MAX_TIMELINE_ROWS = 150
#: The sandbox's working directory; reads outside it are the interpreter
#: and its libraries loading, which a run of collapses into one row.
_WORKSPACE = "/workspace"
_COLLAPSE_AT = 3
_DESCRIPTION_CHARS = 160
_BACKTICKS = re.compile(r"`+")


# --- escaping -----------------------------------------------------------------


def fence(text: str, info: str = "") -> str:
    """A fenced block no content can close early."""
    longest = max((len(m) for m in _BACKTICKS.findall(text)), default=0)
    marker = "`" * max(3, longest + 1)
    body = text if text.endswith("\n") else text + "\n"
    return f"{marker}{info}\n{body}{marker}"


def _visible(ch: str) -> str:
    if ch.isprintable():
        return ch
    code = ord(ch)
    return (
        f"\\x{code:02x}"
        if code < 0x100
        else f"\\u{code:04x}"
        if code < 0x10000
        else (f"\\U{code:08x}")
    )


#: Every character that can open inline syntax: code, emphasis, strikethrough,
#: links and images, raw HTML, entities, table cells, and (with the line kept
#: whole) headings. Escaping more -- `.` and `:` -- would make versions and
#: hashes unreadable in the raw file for no gain. A bare `https://` or `www.`
#: can still autolink on GitHub; that is clickable, never fetched.
_MD_SPECIAL = frozenset("\\`*_[]()<>|#~&!")


def md_inline(value: object) -> str:
    """Data text, made inert for Markdown: one line, nothing that can form syntax."""
    text = "".join(_visible(ch) for ch in str(value))
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in text)


# --- the timeline ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineRow:
    offset: float
    kind: str
    source: str
    description: str
    evidence: bool


def _subject(event: Event) -> str:
    attrs = event.attrs
    if event.kind == "net_attempt" and attrs.get("host"):
        return f"{attrs.get('method') or ''} {attrs['host']}{attrs.get('path') or ''}".strip()
    value = event.attrs.get("input")
    if isinstance(value, dict) and isinstance(value.get("command"), str):
        return f"{attrs.get('name', '')} {value['command']}".strip()
    if isinstance(attrs.get("argv"), list):
        return " ".join(str(a) for a in attrs["argv"])
    for key in ("path", "host", "name"):
        if attrs.get(key):
            return str(attrs[key])
    return ""


def _matches(event: Event, violation: Mapping[str, Any]) -> bool:
    check = violation.get("check")
    seen = violation.get("evidence", {})
    attrs = event.attrs
    if check == "canary_read":
        return (
            event.kind == "file_read"
            and "path" in seen
            and attrs.get("path") == seen["path"]
            and event.ts == seen.get("ts")
        )
    if check in ("canary_exfil", "egress_offlist"):
        return event.kind == "net_attempt" and all(
            attrs.get(k) == seen.get(k) for k in ("host", "port", "method")
        )
    if check == "protected_write":
        return event.kind == "file_write" and attrs.get("path") == seen.get("path")
    if check == "dangerous_command":
        command = seen.get("command")
        if event.kind == "tool_call":
            value = attrs.get("input")
            return isinstance(value, dict) and str(value.get("command", ""))[:500] == command
        if event.kind == "process_exec" and isinstance(attrs.get("argv"), list):
            return bool(" ".join(str(a) for a in attrs["argv"])[:500] == command)
    return False


def timeline(
    trace: Trace, violations: Sequence[Mapping[str, Any]], action_keys: Sequence[str]
) -> list[TimelineRow]:
    """Every event, offset from the first, marked when it is this finding's evidence."""
    ordered = trace.sorted().events
    if not ordered:
        return []
    wanted = set(action_keys)
    mine = [v for v in violations if not wanted or v.get("action_key") in wanted]
    base = ordered[0].ts
    rows: list[TimelineRow] = []
    noise: list[TimelineRow] = []

    def flush() -> None:
        if len(noise) >= _COLLAPSE_AT:
            rows.append(
                TimelineRow(
                    offset=noise[0].offset,
                    kind="file_read",
                    source=noise[0].source,
                    description=(
                        f"{len(noise)} file_reads outside {_WORKSPACE} (libraries, interpreter)"
                    ),
                    evidence=False,
                )
            )
        else:
            rows.extend(noise)
        noise.clear()

    for event in ordered:
        row = TimelineRow(
            offset=round(event.ts - base, 3),
            kind=event.kind,
            source=event.source,
            description=f"{event.kind} {_subject(event)}".strip()[:_DESCRIPTION_CHARS],
            evidence=any(_matches(event, v) for v in mine),
        )
        path = str(event.attrs.get("path", ""))
        if event.kind == "file_read" and not row.evidence and not path.startswith(_WORKSPACE):
            noise.append(row)
            continue
        flush()
        rows.append(row)
    flush()
    return rows


def cap_rows(
    rows: Sequence[TimelineRow], max_rows: int = MAX_TIMELINE_ROWS
) -> tuple[list[TimelineRow], int]:
    """At most `max_rows`, never dropping evidence; and how many were dropped."""
    if len(rows) <= max_rows:
        return list(rows), 0
    evidence = [i for i, r in enumerate(rows) if r.evidence]
    others = [i for i, r in enumerate(rows) if not r.evidence]
    keep = sorted(evidence + others[: max(0, max_rows - len(evidence))])
    return [rows[i] for i in keep], len(rows) - len(keep)


# --- the diff -----------------------------------------------------------------


def payload_diff(original: Mapping[str, str], shrunk: Mapping[str, str]) -> str:
    """A unified diff per surface, original to shrunk."""
    out: list[str] = []
    for surface in sorted(set(original) | set(shrunk)):
        out.extend(
            difflib.unified_diff(
                original.get(surface, "").splitlines(),
                shrunk.get(surface, "").splitlines(),
                fromfile=f"{surface} (original)",
                tofile=f"{surface} (shrunk)",
                lineterm="",
            )
        )
    return "\n".join(out)


# --- reading a run --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDetail:
    finding: Finding
    group_size: int
    #: None when the finding predates `candidate_id` or the candidate is gone.
    diff: str | None
    #: The trial the timeline shows; None when no recorded trial reproduced.
    trial_id: str | None
    rows: list[TimelineRow]
    omitted: int


@dataclass(frozen=True, slots=True)
class Report:
    run_id: str
    meta: dict[str, Any]
    candidates: int
    findings: list[Finding]
    groups: list[FindingGroup]
    dropped: list[DroppedCandidate]
    details: list[FindingDetail]


def _dropped(record: Mapping[str, Any]) -> DroppedCandidate:
    rate = record["rate"]
    return DroppedCandidate(
        candidate_id=str(record["candidate_id"]),
        reason=str(record["reason"]),
        # Recomputed from the counts, as `Finding.from_record` does.
        rate=estimate(int(rate["successes"]), int(rate["trials"])),
    )


def _detail(
    store: RunStore,
    group: FindingGroup,
    originals: Mapping[str, dict[str, str]],
    trials: Mapping[str, Mapping[str, Any]],
) -> FindingDetail:
    finding = group.representative
    original = originals.get(finding.candidate_id or "")
    diff = payload_diff(original, finding.payloads) if original is not None else None
    for trial_id in finding.trial_ids:
        record = trials.get(trial_id)
        verdict = (record or {}).get("verdict") or {}
        keys = {v.get("action_key") for v in verdict.get("violations", [])}
        if (
            record is None
            or not keys
            or (finding.action_keys and not keys & set(finding.action_keys))
        ):
            continue
        trace = Trace.from_jsonl(store.get_blob(record["trace_ref"]).decode("utf-8"), trial_id)
        rows, omitted = cap_rows(timeline(trace, verdict["violations"], finding.action_keys))
        return FindingDetail(finding, group.size, diff, trial_id, rows, omitted)
    return FindingDetail(finding, group.size, diff, None, [], 0)


def build_report(store: RunStore, run_id: str) -> Report:
    findings = [Finding.from_record(r) for r in store.read(run_id, "findings")]
    groups = dedupe(findings)
    originals: dict[str, dict[str, str]] = {}
    candidates = 0
    for record in store.read(run_id, "candidates"):
        candidates += 1
        originals[str(record["candidate_id"])] = dict(record["payloads"])
    trials = {str(r["trial_id"]): r for r in store.read(run_id, "trials")}
    return Report(
        run_id=run_id,
        meta=store.meta(run_id),
        candidates=candidates,
        findings=findings,
        groups=groups,
        dropped=[_dropped(r) for r in store.read(run_id, "dropped")],
        details=[_detail(store, g, originals, trials) for g in groups],
    )


# --- rendering ----------------------------------------------------------------


def _headline(finding: Finding) -> str:
    """`Finding.title()` without the rate, for places that show the rate beside it."""
    if not finding.action_keys:
        return "violation"
    check, _, target = finding.action_keys[0].partition(":")
    return f"{check} → {target}" if target else check


def _reproduce(finding: Finding) -> str:
    meta = finding.agent_meta
    args = [
        "reprobe",
        "run",
        f"scenarios/{finding.scenario_name}/scenario.yaml",
        "--agent",
        meta.agent_id,
        "--model",
        meta.model_id,
    ]
    if finding.agent_profile:
        args += ["--agent-profile", finding.agent_profile]
    for surface in finding.payloads:
        args += ["--payload-file", f"{surface}={surface}.txt"]
    args += ["--runs", "10"]
    return " ".join(shlex.quote(a) for a in args)


def _render(report: Report, template: str, *, html: bool) -> str:
    env = _env()
    env.autoescape = html
    env.filters["mdi"] = md_inline
    env.filters["fence"] = fence
    env.filters["reproduce"] = _reproduce
    env.filters["headline"] = _headline
    return env.get_template(template).render(report=report)


def render_markdown(report: Report) -> str:
    return _render(report, "report.md.j2", html=False)


def render_html(report: Report) -> str:
    return _render(report, "report.html.j2", html=True)


def write_report(report: Report, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    renderer = render_html if dest.suffix.lower() in {".html", ".htm"} else render_markdown
    dest.write_text(renderer(report), encoding="utf-8")
    return dest


__all__ = [
    "MAX_TIMELINE_ROWS",
    "FindingDetail",
    "Report",
    "TimelineRow",
    "build_report",
    "cap_rows",
    "fence",
    "md_inline",
    "payload_diff",
    "render_html",
    "render_markdown",
    "timeline",
    "write_report",
]
