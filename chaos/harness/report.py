"""Turning a run into evidence.

Everything written here is committed, so every write pins newline="\n": on Windows the
default translates to CRLF, which would flip the generated files to CRLF on every run and
trip the repo's line-ending check.

Two outputs per run: a JSON record (machine-readable, kept out of Git) and a Markdown
incident report pre-filled with the measured timeline (committed, with screenshots
added by hand afterwards).

The screenshots are the one part that stays manual. An automated capture would be
easy and would also be less convincing - the point of the evidence directory is that a
person watched these dashboards while the system was broken.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "chaos" / "harness" / "out"
EVIDENCE_DIR = REPO_ROOT / "evidence"

#: Pinned on every write. Python's default on Windows translates "\n" to CRLF, which
#: would flip these committed files to CRLF on every run and trip the repo's
#: line-ending check - which exists because sloth silently rejects CRLF YAML.
LF = "\n"


@dataclass
class Event:
    at: str
    label: str
    source: str


@dataclass
class RunResult:
    scenario_id: str
    title: str
    hypothesis: str
    cluster: str
    started_at: str
    injected: str
    expected_alert: str | None
    alert_fired: str | None = None
    alert_severity: str | None = None
    time_to_detect_s: float | None = None
    time_to_recover_s: float | None = None
    fault_duration_s: float | None = None
    budget_remaining_before: float | None = None
    budget_remaining_after: float | None = None
    burn_rate_before: float | None = None
    burn_rate_peak: float | None = None
    recovery_failed: bool = False
    outcome: str = "unknown"
    notes: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def record(self, label: str, source: str, at: datetime | None = None) -> None:
        stamp = (at or datetime.now(tz=UTC)).strftime("%H:%M:%S")
        self.events.append(Event(at=stamp, label=label, source=source))

    @property
    def budget_consumed_pct(self) -> float | None:
        if self.budget_remaining_before is None or self.budget_remaining_after is None:
            return None
        # Sloth reports remaining budget as a ratio of the 30-day allowance.
        return (self.budget_remaining_before - self.budget_remaining_after) * 100.0

    @property
    def alerting_as_predicted(self) -> bool:
        """An unexpected page and a missing page are both failures of the prediction."""
        return bool(self.expected_alert) == bool(self.alert_fired)

    @property
    def hypothesis_held(self) -> bool:
        """Did the run match its prediction, on every axis it was measured on?

        Alerting is not the whole hypothesis. gitops-drift predicts nothing about
        alerts - it predicts that Argo CD reverts a hand-edited replica count - so a run
        where the revert never happened must not report "hypothesis held" merely because
        no alert fired, which is exactly what an alerting-only check would do. That
        would put "GitOps self-healing works" in the evidence directory on the strength
        of a run that demonstrated the opposite.
        """
        if self.recovery_failed:
            return False
        return self.alerting_as_predicted


def duration(seconds: float | None) -> str:
    # ASCII only: this string goes to both a Markdown table and a Windows console,
    # and cp1252 cannot encode the em dash that would otherwise read better here.
    if seconds is None:
        return "n/a"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _burn(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def write_json(result: RunResult) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{result.scenario_id}-{stamp}.json"
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8", newline=LF)
    return path


def report_date(result: RunResult) -> str:
    """The date the run STARTED.

    Not `now()`. A run-all pass takes hours, so a scenario that begins at 23:50 finishes
    on the following UTC day - and `rebuild_summary` builds its links from
    `started_at[:10]`. Deriving the directory name from a different clock than the links
    is how every row in the summary table ends up pointing at a directory that does not
    exist.
    """
    return result.started_at[:10]


def write_report(result: RunResult) -> Path:
    date = report_date(result)
    directory = EVIDENCE_DIR / f"{date}-{result.scenario_id}"
    (directory / "img").mkdir(parents=True, exist_ok=True)
    path = directory / "report.md"

    if path.exists():
        # Never clobber a report a human has annotated. The template asks for a
        # "What I would change" paragraph and screenshots; a re-run on the same day
        # would silently delete exactly the parts that make the evidence worth reading.
        existing = path.read_text(encoding="utf-8")
        if _HUMAN_MARKER not in existing or _has_written_analysis(existing):
            stamp = datetime.now(tz=UTC).strftime("%H%M%S")
            path = directory / f"report-{stamp}.md"

    path.write_text(_render(result, date), encoding="utf-8", newline=LF)
    return path


#: Present in every generated report; its absence means someone rewrote the file.
_HUMAN_MARKER = "## What I would change"


def _has_written_analysis(existing: str) -> bool:
    """True if someone has filled in the closing section rather than leaving the prompt."""
    tail = existing.split(_HUMAN_MARKER, 1)[-1]
    # The generated version leaves only an HTML comment under that heading.
    stripped = "\n".join(
        line for line in tail.splitlines()
        if line.strip() and not line.strip().startswith(("<!--", "-->", "     "))
    ).strip()
    return bool(stripped)


def _render(result: RunResult, date: str) -> str:
    timeline = "\n".join(
        f"| {event.at} | {event.label} | {event.source} |" for event in result.events
    ) or "| — | no events recorded | — |"

    budget = result.budget_consumed_pct
    budget_line = f"{budget:.2f}% of the 30-day budget" if budget is not None else "n/a"

    alert_line = result.alert_fired or (
        "none, as predicted" if not result.expected_alert
        else f"**none. {result.expected_alert} was expected but did not fire**"
    )

    verdict = (
        "The hypothesis held." if result.hypothesis_held
        else "**The hypothesis did not hold.** See the notes below."
    )

    notes = "\n".join(f"- {note}" for note in result.notes) or "- (none recorded)"

    return f"""# {result.title}

`{result.scenario_id}` · {date} · cluster: `{result.cluster}`

## Hypothesis

{result.hypothesis}

{verdict}

## What was injected

{result.injected}

Fault held for {duration(result.fault_duration_s)}.

## Timeline (UTC)

| Time | Event | Source |
|---|---|---|
{timeline}

## Measurements

| Metric | Value |
|---|---|
| Time to detect | {duration(result.time_to_detect_s)} |
| Time to recover (MTTR) | {duration(result.time_to_recover_s)} |
| Alert fired | {alert_line} |
| Peak burn rate | {_burn(result.burn_rate_peak)} |
| Error budget consumed | {budget_line} |

Peak burn rate is how far past the budget policy this went: 1.0 is spending budget exactly
as fast as the SLO allows, and 14.4 is the fast-burn page threshold. A run with a peak
under 1.0 never breached its SLO at all, and no MTTR is reported for it — there was
nothing to recover from.

Time to detect is measured from injection to the alert entering `firing` in
Alertmanager. MTTR is measured from injection to the SLO burn rate returning below 1.0
and staying there — not to the moment the fault was removed, which would flatter the
number by however long the rate window takes to catch up.

## Evidence

<!-- Add screenshots to ./img/ and reference them here. Suggested set:
     01-dashboard-before.png    the service dashboard immediately before injection
     02-alert-firing.png        Alertmanager or the Grafana alert panel
     03-dashboard-during.png    RED metrics at the worst point
     04-recovered.png           the same dashboard after recovery
-->

## Notes

{notes}

## What I would change

<!-- One honest paragraph. What did this run reveal about the system, the alerting, or
     the assumptions behind the SLO? A scenario that behaved perfectly still teaches
     something — usually that a threshold is too loose. -->
"""


def load_runs() -> list[dict]:
    if not OUT_DIR.exists():
        return []
    runs = []
    for path in sorted(OUT_DIR.glob("*.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return runs


def rebuild_summary() -> Path:
    """Regenerate evidence/README.md from every recorded run.

    Latest run per scenario wins: a re-run after a fix should replace the old number,
    not sit next to it.
    """
    latest: dict[str, dict] = {}
    for run in load_runs():
        latest[run["scenario_id"]] = run

    rows = []
    for run in latest.values():
        detected_by = run.get("alert_fired") or (
            "absorbed, no page" if not run.get("expected_alert") else "**not detected**"
        )
        date = run["started_at"][:10]
        rows.append(
            f"| {run['title']} | `{run['cluster']}` | {detected_by} "
            f"| {duration(run.get('time_to_detect_s'))} "
            f"| {duration(run.get('time_to_recover_s'))} "
            f"| [report]({date}-{run['scenario_id']}/report.md) |"
        )

    table = "\n".join(rows) if rows else (
        "| _no runs recorded yet_ | | | | | |"
    )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / "README.md"
    path.write_text(_summary_document(table), encoding="utf-8", newline=LF)
    return path


def _summary_document(table: str) -> str:
    return f"""# Evidence

Every failure mode in this platform, deliberately induced, with what the alerting said
and how long recovery took.

This directory is the point of the project. Provisioning infrastructure is a claim;
this is the receipt.

## Measured recovery

| Scenario | Cluster | Detected by | Time to detect | MTTR | Report |
|---|---|---|---|---|---|
{table}

Regenerate this table with `make evidence` after a run.

## How the numbers are defined

**Time to detect** — from fault injection to the alert entering `firing` in
Alertmanager. Not to `pending`: a pending alert has not woken anyone up.

**MTTR** — from fault injection to the SLO burn rate falling below 1.0 and staying
there for three consecutive minutes. Measured against the same recording rules the
alerts use, so the harness cannot report a recovery the on-call dashboard disagrees
with. Note this is deliberately pessimistic: because the SLIs use 5-minute rate
windows, the clock keeps running for several minutes after the system is actually
healthy again.

**Peak burn rate** — how far past the budget policy a run went. 1.0 is spending budget
exactly as fast as the SLO allows; 14.4 is the fast-burn page threshold. A run whose peak
stayed under 1.0 never breached its SLO, and no MTTR is reported for it, because there was
nothing to recover from. The harness will not print a recovery time for a service that
never broke.

**"Absorbed, no page"** is a success, not a gap. Killing one pod out of two should not
page anyone; a platform that alerts on every recoverable event trains its operators to
ignore it.

## Two kinds of alert, on purpose

Burn-rate alerts answer "are we spending error budget faster than the policy allows", and
they answer it deliberately slowly — a short incident does not spend much budget. A total
database outage lasting 50 seconds spends 1.9% of catalog's 30-day budget, just under the
2%-per-hour the fast-burn page fires on, so every
burn-rate window ignores it, correctly.

It is still a total outage. The hand-written symptom rules in `gitops/slo/handwritten/`
catch what the budget rules are designed not to, and `pod-kill-postgres` exists to
demonstrate that gap rather than paper over it. Rows detected by `DependencyHardDown` or
`ServiceHardDown` are the symptom tier; rows detected by a `*SLOBurn` alert are the budget
tier.

## Reading a report

Each report records the hypothesis *before* the run, then what happened. Where the two
disagree, the report says so — those are the interesting ones, and they are left in
rather than re-run until they look good.
"""
