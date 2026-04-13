# Evidence

Every failure mode in this platform, deliberately induced, with what the alerting said
and how long recovery took.

This directory is the point of the project. Provisioning infrastructure is a claim;
this is the receipt.

## Measured recovery

| Scenario | Cluster | Detected by | Time to detect | MTTR | Report |
|---|---|---|---|---|---|
| _no runs recorded yet_ | | | | | |

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
