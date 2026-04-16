# Demo script

A guided tour for someone who has just run `make demo` (or `bash scripts/demo.sh`) and has
a healthy cluster. Steps 1–5 take about ten minutes. Steps 6 and 7 add another ten if you
have them.

Everything below assumes Git Bash on Windows, or any shell on macOS/Linux. Leave
`scripts/urls.sh` running in its own terminal for the whole tour.

---

## 1. Open the UIs (30s)

```bash
bash scripts/urls.sh
```

It prints the URLs and both admin passwords, then holds open port-forwards to Grafana
(3000), Argo CD (8080), Prometheus (9090), Alertmanager (9093) and the Argo Rollouts
dashboard (3100). Ctrl-C closes them all.

These are port-forwards rather than Ingress routes on purpose: putting Grafana behind the
same Ingress the demo traffic uses would mean a reviewer clicking around a dashboard shows
up as storefront error-budget burn.

## 2. The application works (1 min)

```bash
curl -s http://localhost/products | head -c 400; echo
curl -s http://localhost/products/1; echo
```

The first is the cheap read — storefront calls catalog only. The second is the interesting
one: storefront fans out to catalog and recs concurrently with `asyncio.gather`. Look for
`"recommendations_degraded": false` in the response. That flag is the whole graceful-
degradation story in one field, and step 5 makes it flip.

Now try a path that is deliberately not routed:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/internal/faults
```

404 from nginx. The Ingress is a two-prefix allowlist (`/products`, `/orders`), not `/`,
because anyone who could reach `PUT /internal/faults` from outside could dial in the exact
failure the evidence claims was injected under controlled conditions.

## 3. The dashboards (2 min)

Grafana: <http://localhost:3000>, credentials printed by `urls.sh`.

**Dashboards → "storefront — service SLO".** RED metrics — rate, errors, duration — from the service's own
`http_requests_total` and `http_request_duration_seconds`. Note the request rate sits near
8 req/s: the load generator uses k6's `constant-arrival-rate` executor, so offered load
stays flat even when the system slows down. Under `constant-VUs` a latency injection would
*reduce* the request rate, shrinking the SLI denominator exactly when the numerator grows.

**Dashboards → "recs — service SLO".** The latency distribution is bimodal by design: 250ms baseline with
±20% jitter, and 2% of calls carrying an extra 400ms. A service whose latency is uniformly
250ms makes a boring dashboard and a meaningless p95.

**Dashboards → "SLO overview — error budgets".** Six SLOs, their current burn rates and
remaining budget. Everything should be near zero burn.

## 4. The SLOs and the error budget (2 min)

Prometheus: <http://localhost:9090>.

```promql
sloth_slo_info
```

Six series, one per SLO, carrying `sloth_objective` and `sloth_service`. These are compiled
from `gitops/slo/specs/*.yaml` by Sloth at build time and committed to
`gitops/slo/generated/` — no controller generates them in the cluster, so the alert
arithmetic is reviewable in a pull request (ADR 0003).

```promql
slo:current_burn_rate:ratio
slo:period_error_budget_remaining:ratio
```

Burn rate 1.0 means the budget is being spent exactly on schedule and runs out precisely at
day 30. Above 14.4 is page territory.

Now look at the alert that would page, in **Status → Rules**, group
`sloth-slo-alerts-storefront-availability`:

```promql
max(slo:sli_error:ratio_rate5m{sloth_id="storefront-availability", ...} > (14.4 * 0.005))
and
max(slo:sli_error:ratio_rate1h{sloth_id="storefront-availability", ...} > (14.4 * 0.005))
```

`0.005` is storefront's error budget (1 − 99.5%). `14.4` is `0.02 × 720h / 1h` — the burn
rate that spends 2% of a 30-day budget in one hour. Multiply them and the page fires at 7.2%
of requests returning 5xx, in both windows at once. `docs/slo-methodology.md` derives the
whole table and explains why the short window exists.

Then look at the `symptom-alerts` group in the same list. Those four rules are hand-written
rather than generated, and they exist because burn-rate alerting is deliberately slow: a
50-second total database outage spends 1.9% of catalog's month and stays under every window,
correctly. `DependencyHardDown` is what fires instead. Budget alerts and symptom alerts are
complements, and having both is the substance of the `pod-kill-postgres` scenario.

## 5. GitOps drift, reverted (1 min)

Argo CD: <http://localhost:8080>. Fourteen Applications, all Synced and Healthy, expanded
from the single root app-of-apps that `make bootstrap` applied.

Break one by hand:

```bash
kubectl -n demo scale deployment/recs --replicas=5
kubectl -n demo get deployment recs -w
```

Watch the replica count go to 5 and come back to 2 within seconds. Argo CD's
`syncPolicy.automated.selfHeal: true` on the `recs` Application means the cluster is not
just *compared* against Git, it is *corrected* to match it. Ctrl-C the watch.

The same thing measured rather than watched:

```bash
python chaos/harness/run.py run gitops-drift
```

Recovery for that scenario is checked against a jsonpath on the resource, not against an
SLI — the failure being tested is configuration drift, not availability, and it should be
invisible to a customer.

## 6. A chaos scenario, with a measured MTTR (5 min)

```bash
python chaos/harness/run.py list
python chaos/harness/run.py run pod-kill-catalog
```

This one is the control case. Two catalog replicas and a readiness probe should absorb a
single pod deletion: a few in-flight requests may fail, but not enough to trip a 14.4x burn
rate, so **no page should fire**. A scenario that quietly recovers is as much a result as
one that pages, and a platform that alerts on every recoverable event trains its operators
to ignore it.

The run takes four to six minutes because the harness will not call it recovered until the
burn rate has been under 1.0 for 180 consecutive seconds. Watch the `storefront` and
`catalog` dashboards while it runs — you should see a brief latency blip and no error spike.

Before it starts, the harness refuses to run if the target is serving less than 0.1 req/s.
With no traffic every ratio SLI is 0/0, the experiment would inject a fault, observe
nothing, and report a flawless recovery — the most misleading possible result.

At the end it prints time-to-detect, MTTR and budget consumed, and writes
`evidence/<date>-pod-kill-catalog/report.md` pre-filled with the measured timeline.

Two more are worth running, but not during a ten-minute tour: `error-spike-recs` holds an
80% error rate for 20 minutes and `latency-catalog` holds a 500ms network delay for 15,
because that is genuinely how long a fault must last before a 1h burn-rate window can cross
its threshold (`chaos/scenarios.yaml` carries the inequality that sets each duration; the
derivation is in `docs/slo-methodology.md`). Start one and come back.

`error-spike-recs` is the one that proves graceful degradation: storefront should keep
serving product pages with `recommendations_degraded: true` while recs' budget collapses —
two services, one broken, one alert. `latency-catalog` is a Chaos Mesh network delay that
catalog's own logs and traces show nothing about; finding it means comparing storefront's
`dependency_request_duration_seconds` against catalog's own `http_request_duration_seconds`
and noticing the gap.

## 7. The canary aborts itself (3 min)

`python chaos/harness/run.py run canary-abort` does this and records it, holding the fault
for its full ten minutes so the analysis reaches its own verdict first. Run it by hand
instead if you want to watch the abort happen, which is the part worth seeing.

In one terminal (this needs the `kubectl argo rollouts` plugin — see
`docs/prerequisites.md`; without it, the Rollouts dashboard at <http://localhost:3100>
shows the same thing):

```bash
kubectl argo rollouts get rollout storefront -n demo --watch
```

In another:

```bash
bash chaos/experiments/canary-abort.sh apply
```

The "broken version" is not a different image. It is the same image with
`FAULT_ERROR_RATE=0.5`, which changes the pod template, which starts a canary. One mechanism
serves both the chaos suite and this demo, so the signals are comparable.

What to watch, in order:

1. A canary ReplicaSet scales to 1 of 4 pods — `setWeight: 25`. Four replicas rather than
   two, because at two the smallest possible canary is half of production traffic.
2. An `AnalysisRun` appears and starts measuring after `initialDelay: 60s`.
3. Three consecutive failed measurements (60s, 90s, 120s) trip `failureLimit: 2` and the
   rollout aborts, returning all traffic to stable.

The analysis query is scoped to canary pods only, through
`rollouts_pod_template_hash="{{args.canary-hash}}"`. That scoping is the load-bearing detail:
at 25% weight an unscoped query blends canary and stable, so anything failing less than 20%
of its requests would sail under the 5% gate. The label reaches Prometheus only because the
PodMonitor carries `podTargetLabels: [rollouts-pod-template-hash]`.

Success here is not "no errors". It is **"the errors stopped without a human"**.

Clean up:

```bash
bash chaos/experiments/canary-abort.sh remove
```

That aborts (idempotently), restores `FAULT_ERROR_RATE=0.0`, re-enables the Application's
automated sync, and promotes the known-good template — an aborted Rollout stays `Degraded`
until a new template arrives, so Argo CD would otherwise show the Application unhealthy
indefinitely.

## 8. The evidence

```bash
make evidence          # or: python chaos/harness/run.py report
```

Rebuilds `evidence/README.md` from every recorded run, latest per scenario. Each row is a
scenario, the cluster it ran on, what detected it, time-to-detect, MTTR, and a link to the
full report.

Each report records the hypothesis *before* the run and then what happened. Where the two
disagree, the report says so — those are left in rather than re-run until they look good.
An unexpected page and a missing page are both failures of a hypothesis, and both are more
interesting than a scenario that went to plan.

**Start here: [`evidence/README.md`](../evidence/README.md).** Provisioning infrastructure is
a claim; that directory is the receipt.

---

## Tearing down

```bash
make kind-down
```
