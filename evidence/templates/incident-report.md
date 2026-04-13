<!--
Template for an incident captured by hand rather than by the chaos harness.

Copy it to evidence/<YYYY-MM-DD>-<slug>/report.md, create an img/ directory alongside it,
fill in the placeholders, then run `make evidence` so the row appears in
evidence/README.md.

The structure mirrors chaos/harness/report.py::_render exactly, so hand-written and
measured incidents read the same and sit in the same summary table. If that function
changes, change this file with it.

Duration format matches report.duration(): "45s", "3m 12s", or "n/a".
Verdict line is one of:
    The hypothesis held.
    **The hypothesis did not hold.** See the notes below.
Alert line is one of:
    <AlertName>
    none, as predicted
    **none. <AlertName> was expected but did not fire**
-->

# <one-line title, e.g. "Catalog pod killed">

`<scenario-id>` · <YYYY-MM-DD> · cluster: `<kind|eks>`

## Hypothesis

<What you expected to happen, written BEFORE the run. If it was not written before the
run, say so here — a hypothesis reconstructed afterwards is a description, not a
prediction, and the difference is the whole value of this document.>

<verdict line — see the header comment>

## What was injected

<What was done, concretely enough to repeat: the command, the manifest, the fault
configuration. "30% error rate on recs via PUT /internal/faults on both pods", not
"errors were injected".>

Fault held for <duration>.

## Timeline (UTC)

| Time | Event | Source |
|---|---|---|
| HH:MM:SS | fault injected (T0) | harness \| manual |
| HH:MM:SS | <AlertName> firing (page \| ticket) | Alertmanager |
| HH:MM:SS | fault removed | harness \| manual |
| HH:MM:SS | SLI healthy and holding | Prometheus |

## Measurements

| Metric | Value |
|---|---|
| Time to detect | <duration> |
| Time to recover (MTTR) | <duration> |
| Alert fired | <alert line — see the header comment> |
| Error budget consumed | <N.NN>% of the 30-day budget |

Time to detect is measured from injection to the alert entering `firing` in
Alertmanager. MTTR is measured from injection to the SLO burn rate returning below 1.0
and staying there — not to the moment the fault was removed, which would flatter the
number by however long the rate window takes to catch up.

<!-- Where the numbers come from, if you are filling these in by hand:

     time to detect     the alert's startsAt in Alertmanager, minus T0.
                        Use `firing`, not `pending` — a pending alert woke nobody.
                          curl -s localhost:9093/api/v2/alerts | jq '.[] | {name: .labels.alertname, state: .status.state, startsAt}'

     MTTR               the first moment slo:current_burn_rate:ratio{sloth_id="..."}
                        went below 1.0 and stayed there for 180s, minus T0.
                        Read it off the Prometheus graph, not off the dashboard's
                        smoothed panel.

     budget consumed    slo:period_error_budget_remaining:ratio{sloth_id="..."}
                        before minus after, times 100. On a cluster that has been up
                        for hours rather than 30 days this is "since Prometheus
                        started" — see docs/slo-methodology.md before quoting it as a
                        monthly figure.
-->

## Evidence

<!-- Add screenshots to ./img/ and reference them here. Suggested set:
     01-dashboard-before.png    the service dashboard immediately before injection
     02-alert-firing.png        Alertmanager or the Grafana alert panel
     03-dashboard-during.png    RED metrics at the worst point
     04-recovered.png           the same dashboard after recovery
-->

<!-- Worth capturing as well, when the incident involved them:
     05-argocd-app.png          the Argo CD Application, for a drift or sync incident
     06-rollout.png             `kubectl argo rollouts get rollout ... ` at the abort
     07-trace.png               a Tempo trace of a failing request
     08-burn-rate.png           slo:current_burn_rate:ratio across the whole incident

     Screenshots stay manual on purpose. An automated capture would be easy and would
     also be less convincing — the point of this directory is that a person watched
     these dashboards while the system was broken. Set Grafana to an absolute time
     range covering the incident before capturing, so the images stay meaningful after
     the cluster is gone.
-->

## Notes

- <Anything the timeline does not carry: what was surprising, what was checked and ruled
  out, which runbook step was wrong or missing.>

## What I would change

<!-- One honest paragraph. What did this run reveal about the system, the alerting, or
     the assumptions behind the SLO? A scenario that behaved perfectly still teaches
     something — usually that a threshold is too loose. -->
