# SLO methodology

How the numbers in `gitops/slo/specs/` were chosen, what the generated alerting rules
actually compute, and where the approach breaks down on a cluster that lives for an
afternoon.

## The three terms, concretely

**SLI** — a ratio of bad events to all events over a window. Sloth's convention is that
the SLI *is* the error ratio, so zero is perfect and every recording rule is named
`slo:sli_error:ratio_rateXX`. storefront's availability SLI:

```promql
sum(rate(http_requests_total{service="storefront", status_code=~"5.."}[5m]))
/
sum(rate(http_requests_total{service="storefront"}[5m]))
```

**SLO** — a target on that ratio over a 30-day rolling window. storefront availability is
99.5%, so the error ratio must average no more than 0.005.

**Error budget** — `1 − SLO`, the fraction of requests allowed to fail. Expressed as
wall-clock time over the 720 hours in 30 days it is much easier to argue about:

| SLO | Objective | Error budget | Over 30 days |
|---|---|---|---|
| `storefront` availability | 99.5% | 0.005 | 3h 36m of total failure |
| `catalog` availability | 99.9% | 0.001 | 43m 12s |
| `recs` availability | 99.0% | 0.010 | 7h 12m |
| `storefront` latency (400ms) | 99.0% | 0.010 | 1% of requests over the threshold |
| `catalog` latency (200ms) | 99.0% | 0.010 | 1% of requests over the threshold |
| `recs` latency (800ms) | 99.0% | 0.010 | 1% of requests over the threshold |

The latency objectives are 99%, not the 95% they were first written at. That change came
out of the derivation below rather than out of taste; see
[What the arithmetic caught](#what-the-arithmetic-caught).

**Burn rate** — observed error ratio ÷ error budget. A burn rate of 1 means the budget is
being spent exactly on schedule and runs out precisely at day 30. Sloth records two:
`slo:current_burn_rate:ratio` (the 5m SLI over the budget) and
`slo:period_burn_rate:ratio` (the 30d SLI over the budget). The chaos harness declares
recovery when the first of those returns below 1.0 and stays there.

## Why these targets

The constraint that decided them is stated in `planning.md` and in every Sloth spec: the
budget has to be burnable inside a demo. A 99.99% target over 30 days leaves 4 minutes and
19 seconds of budget. There is no chaos experiment that spends a visible slice of that and
no dashboard panel where a reviewer can watch it move.

Given that floor, the ordering follows the dependency graph:

- **`catalog` 99.9%** — the tightest, because catalog failures propagate. storefront cannot
  degrade around a missing product the way it degrades around missing recommendations, and
  catalog does the least work per request, so there is less that can legitimately go wrong.
- **`recs` 99.0%** — the loosest, because storefront serves the product page without
  recommendations and sets `recommendations_degraded: true`. A recs outage costs the
  recommendations block, not the page. Holding recs to storefront's number would mean
  alerting on the behaviour it was built to have.
- **`storefront` 99.5%** — in between, and the only one a customer would feel, since it is
  the only service behind the Ingress.

The latency thresholds are set where a defect becomes visible rather than where normal
operation sits:

- **catalog 200ms.** A single indexed query against a Postgres in the same namespace is far
  under this. The threshold is where a connection-pool problem (`DB_POOL_MAX`, default 10)
  or a missing index shows up.
- **recs 800ms.** `recs` sleeps `RECS_BASE_LATENCY_MS` (250ms) with ±20% jitter and adds
  `RECS_TAIL_LATENCY_MS` (400ms) to `RECS_TAIL_RATE` (2%) of calls, so the designed tail
  lands at 600–700ms. 800ms sits just above it: tight enough that added latency trips it,
  loose enough that the built-in tail does not.
- **storefront 400ms.** `GET /products/{id}` fans out to catalog and recs with
  `asyncio.gather`, so it costs the slower leg rather than the sum. 400ms sits between the
  two dependencies' thresholds for that reason.

The last one has a steady-state consequence worth being explicit about, because it is the
one number that ties the application's design to its SLO. The load generator's mix is 80%
`GET /products`, 15% `GET /products/{id}`, 5% `POST /orders`, so only 15% of storefront
traffic reaches recs at all. The recs tail is the only thing in a healthy system that
crosses 400ms, so storefront's resting breach rate is:

```
0.15 (fan-out share) × 0.02 (recs tail rate) = 0.003  →  0.3% of storefront requests
0.003 / 0.01 (budget) = 0.30 burn rate
```

`RECS_TAIL_RATE` was 0.05 when the latency objective was 95%. Raising the objective to 99%
cut the budget by a factor of five, and 0.15 × 0.05 = 0.0075 against a 0.01 budget is a
**0.75 burn rate with nothing wrong** — three quarters of the month's latency budget
consumed by the system working exactly as designed. Dropping the tail rate to 0.02 restores
the headroom. The two constants are coupled, and changing either without the other is how
an SLO quietly stops meaning anything.

`planning.md` records these as provisional: the availability targets were chosen for
demonstrability and should be re-derived from observed baseline behaviour once the stack
has run under load for a few hours.

## Deriving one alert from scratch

This is the part that matters. The generated rule for storefront availability, page
severity, is four lines of PromQL with two magic numbers in it. Here is where both come
from.

### Step 1: pick an alerting policy, not a threshold

The policy is: **page when 2% of the 30-day error budget has been consumed in the last
hour.**

That is a statement about money, not about errors. It says an operator should be woken when
the service is spending its month's reliability allowance fast enough that continuing for
two more days would exhaust it — regardless of whether the absolute error rate is 7% or
70%, and regardless of which SLO it is.

### Step 2: turn the policy into a burn rate

Budget consumed over a window = window length × burn rate, both measured in units of the
SLO period.

```
2% of 30 days   = 0.02 × 720h = 14.4 hours of budget
consumed in     = 1 hour
burn rate       = 14.4h / 1h  = 14.4
```

14.4 is not a constant handed down by Google. It is `0.02 × 720 / 1`. Change the policy and
the number moves with it. The full table in `docs/conventions.md` is the same arithmetic
run four times:

| Policy | Window | Burn rate | Check |
|---|---|---|---|
| 2% of budget in 1h | 1h | **14.4** | 14.4 × 1 / 720 = 0.02 |
| 5% of budget in 6h | 6h | **6** | 6 × 6 / 720 = 0.05 |
| 10% of budget in 24h | 24h | **3** | 3 × 24 / 720 = 0.10 |
| 10% of budget in 72h | 72h | **1** | 1 × 72 / 720 = 0.10 |

At burn rate 14.4 the entire 30-day budget is gone in 720/14.4 = 50 hours. At burn rate 1
it lasts exactly the 30 days, which is what "burn rate 1" means.

### Step 3: turn the burn rate into a PromQL threshold

The SLI is a ratio, and burn rate is that ratio divided by the budget. So a burn rate of
`B` is crossed when:

```
observed error ratio > B × (1 − SLO)
```

For storefront availability: `14.4 × 0.005 = 0.072`. The page fires when more than **7.2%**
of storefront requests are returning 5xx. For the 6x tier: `6 × 0.005 = 0.03`, or 3%.

Those are literally the expressions in the generated rule.

### Step 4: why there are two windows

Look at the generated alert (`gitops/slo/generated/storefront-rules.yaml`, group
`sloth-slo-alerts-storefront-availability`):

```promql
(
    max(slo:sli_error:ratio_rate5m{sloth_id="storefront-availability", ...} > (14.4 * 0.005)) without (sloth_window)
    and
    max(slo:sli_error:ratio_rate1h{sloth_id="storefront-availability", ...} > (14.4 * 0.005)) without (sloth_window)
)
or
(
    max(slo:sli_error:ratio_rate30m{sloth_id="storefront-availability", ...} > (6 * 0.005)) without (sloth_window)
    and
    max(slo:sli_error:ratio_rate6h{sloth_id="storefront-availability", ...} > (6 * 0.005)) without (sloth_window)
)
```

Two tiers joined by `or`, each tier a long window `and` a short window. The short window is
always the long one divided by 12: 5m/1h, 30m/6h, 2h/24h, 6h/72h.

The **long window** carries the policy. It is the one whose length appears in the burn-rate
arithmetic above.

The **short window** exists to end the alert. `rate(...[1h])` is a trailing average: once an
incident stops, the errors it produced stay inside the 1h window and keep the ratio above
0.072 for up to an hour after the service is completely healthy. With only the long
condition, every five-minute incident would page for an hour and a five-minute incident is
not what anyone is being paged about at minute 55. The 5m window falls back below the
threshold within minutes of recovery, and because the two are joined by `and`, the alert
resolves close to when the service actually did.

Note also what is *not* there: no `for:` clause. Sloth emits none, because the two-window
`and` is already the debounce. A single scrape spike cannot satisfy both windows.

### Step 5: what "both windows must be true" costs on detection

This is the consequence that is easy to miss when reading the rule and impossible to miss
when running it.

Under a constant bad-event ratio `e` and steady traffic, the ratio observed over a window
`W`, `t` minutes after a fault starts, is approximately `e × min(t, W) / W`. So a window
crosses a threshold `T` at:

```
t_cross(W) = W × T / e
```

Both windows must be true simultaneously, so the alert fires at the **later** of the two —
which is always the long window. For storefront availability, `T = 0.072`:

| Injected 5xx ratio | Burn rate | 5m crosses | 1h crosses | Page fires at |
|---|---|---|---|---|
| 100% | 200x | 22s | 4m 19s | **~4m 20s** |
| 50% | 100x | 43s | 8m 38s | **~8m 40s** |
| 30% | 60x | 1m 12s | 14m 24s | **~14m 30s** |
| 10% | 20x | 3m 36s | 43m 12s | **~43m** |
| 7.2% | 14.4x | never | never | **never** |

Add roughly 40 seconds on top: Prometheus evaluates rules every 30s
(`evaluationInterval: 30s`) and Alertmanager holds a new group for `group_wait: 10s`.

The short window is the *resolve* mechanism, not the *detect* mechanism. A fault has to
outlast `t_cross(long window)` or nothing pages at all — and once the fault stops, the long
window's ratio plateaus and starts decaying, so it can never catch up afterwards.

### What the arithmetic caught

Running `t_cross` over the configuration that was actually committed found two real defects.
Both are recorded here rather than quietly fixed, because the exercise of doing the
arithmetic is the transferable part.

**The chaos scenarios could not have paged.** The suite originally injected a 30% error rate
into storefront for `duration_s: 300` and predicted a page "in about two minutes" — a
prediction that counts only the 5m window. Over five minutes the 1h window reaches
`0.30 × 5/60 = 0.025`, well under the 0.072 threshold, and once the fault stops that ratio
plateaus and decays. It could never have fired. `error-spike-recs` had the same shape, and
so did `latency-catalog`. An evidence directory full of "expected alert did not fire" would
have looked like a broken platform and been a broken test harness.

`chaos/scenarios.yaml` now carries the inequality in its header and sets every duration from
it, with real headroom over the long window:

| Scenario | `e` | Threshold | `d_min` | Held for |
|---|---|---|---|---|
| `error-spike-storefront` | 0.50 | 0.072 | 8m 38s | 15m |
| `error-spike-recs` | 0.80 | 0.144 | 10m 48s | 20m |
| `latency-catalog` | ~1.0 | 0.144 | 8m 38s | 15m |

`test_every_alert_expecting_scenario_can_actually_fire_its_alert` in
`chaos/harness/tests/test_harness.py` asserts each of these clears its own threshold, so the
next person to shorten a duration fails a test instead of an evidence run.

**The latency objectives were unusable at 95%.** All three latency SLOs started at 95%, which
is the number everyone reaches for. A 95% objective is a 5% budget, so the page threshold is
`14.4 × 0.05 = 0.72` — the alert stays silent until **72% of requests are over the line**.
Worse, put `e = 1.0` into `t_cross`, the strictest possible case where every single request
breaches: `60 × 0.72 / 1.0 = 43.2 minutes` before the 1h window can agree. A total latency
collapse would have gone unreported for three quarters of an hour.

The objectives are now 99% (`gitops/slo/specs/*.yaml`, regenerated into
`gitops/slo/generated/`). Budget 0.01, page threshold 0.144, and the same total collapse
pages in 8m 38s.

The general shape is worth keeping: **a looser objective does not buy a service more
tolerance, it buys its alert more silence.** Objective, budget, alert threshold and
detection time are one calculation, and picking the first number without looking at the
last one produces an alert that is technically present and practically decorative.

## Symptom alerts, and why burn-rate alerting needs them

Everything above describes an alerting policy that is deliberately slow. That is correct —
a five-minute blip should not page anyone — and it leaves a gap that no threshold tuning
closes.

Take `pod-kill-postgres`. Postgres is a single replica, so killing it takes catalog's
database away completely. Say it is gone for 50 seconds. Against catalog's 99.9% objective:

```
budget over 30 days       0.001 × 2,592,000 s  = 2,592 s  (43m 12s)
50 s of total failure     50 / 2592            = 1.9% of the month's budget
same event, as a ratio    50 / 3600            = 1.39% over the 1h window
page threshold            14.4 × 0.001         = 1.44%
```

Those two views are the same arithmetic — "2% of the budget in an hour" and "1.44% over the
1h window" are the same statement — and the event lands just under the line. The page does
not fire, and under the policy it is right not to.

But "we spent 1.9% of the month's reliability in fifty seconds and nobody was told" is not
an acceptable outcome for a *total* outage, and lowering the burn-rate thresholds to catch
it would make them fire on ordinary noise for the remaining 29 days.

There is a second, sharper problem in the same scenario. When Postgres goes, catalog's
`/readyz` fails, every catalog replica leaves the Service, and catalog stops receiving
requests at all. Its SLI becomes `0/0`. A burn-rate alert on catalog is not merely quiet in
that state — it is **structurally incapable of firing at any threshold**, because there are
no events to take a ratio of.

`gitops/slo/handwritten/symptom-rules.yaml` covers what the budget rules cannot:

| Alert | Fires when | Severity |
|---|---|---|
| `ServiceHardDown` | a service is serving traffic and 100% of it is 5xx | page |
| `ServiceReceivingNoTraffic` | a service's request rate is flat zero — the `0/0` case | page |
| `DependencyHardDown` | every call from one service to one dependency is failing | page |
| `SLOBudgetNearlyExhausted` | under 10% of a 30-day budget remains | ticket |

These are cause-and-symptom alerts rather than budget alerts, so they are cheap to reason
about and expensive to get wrong in the other direction — they can fire during a deploy or a
drain. `ServiceHardDown` and `DependencyHardDown` both use a `[2m]` window with `for: 1m`,
which means roughly three minutes of *complete* failure before either fires; the group
evaluates at `interval: 15s` rather than the global 30s for the same reason. That is fast
compared with a burn-rate window and slow compared with the event they are aimed at — see
the limitations below.

`DependencyHardDown` is the one that earns its place in the dependency graph: storefront
returning 503s and catalog being unable to reach Postgres are the same incident, and this
alert says which end to look at. Note that it only sees `catalog → postgres` while catalog
is still making calls; once catalog is NotReady and idle, the surviving signal is
`storefront → catalog`.

The division of labour is the point. Sloth's rules protect a monthly budget. These protect
against events short enough to be invisible to it and severe enough to matter anyway.
Neither replaces the other, which is why they sync from one directory
(`gitops/slo/kustomization.yaml` pulls in `generated/` and `handwritten/` together).

## Why latency SLIs are bucket ratios, not `histogram_quantile`

`svc_common.metrics.LATENCY_BUCKETS` is shared by all three services and contains every
SLO threshold as an exact boundary:

```
0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 2.5, 5.0, 10.0
                              ^^^ catalog        ^^^ storefront  ^^^ recs
```

So the latency SLI is a subtraction of two counters:

```promql
(
  sum(rate(http_request_duration_seconds_count{service="storefront"}[5m]))
  -
  sum(rate(http_request_duration_seconds_bucket{service="storefront", le="0.4"}[5m]))
)
/
sum(rate(http_request_duration_seconds_count{service="storefront"}[5m]))
```

The equivalent quantile formulation — the objectives are 99%, so it would be
`histogram_quantile(0.99, ...) < 0.4` — answers a different question. It finds the bucket
containing the 99th percentile and interpolates linearly inside it, assuming the
observations are uniformly distributed within the bucket. They are not: `recs` produces a
deliberately bimodal distribution (98% of calls near 250ms, 2% near 650ms), so its p99 sits
inside the sparse gap between the two modes, which is where interpolation error is worst.
Moving the objective from 95% to 99% made that worse, not better: the p95 was in the dense
lower mode, the p99 is out on the tail where a handful of observations decide the answer.

More fundamentally, the SLO does not ask "what is p99". It asks "what fraction of requests
exceeded 400ms", and `le="0.4"` answers that as a count. One is a measurement; the other is
an estimate of a statistic that happens to be near the measurement.

The practical differences:

- **Exactness.** `le="0.4"` is a boundary, so no request is misclassified. A quantile
  estimate can put the p99 at 0.38 or 0.43 depending on where the observations fell inside
  the bucket, and neither number is a fact about the service.
- **Aggregation.** Quantiles cannot be averaged across scrape targets; buckets can be summed
  freely. Since a service has multiple pods and the recording rules sum over all of them
  anyway, the bucket form is the one that composes.
- **Scrape interval.** The `kube-prometheus-stack` values note this: because the SLI is a
  bucket ratio rather than an interpolation, a coarser scrape interval costs resolution on
  the dashboards but does not bias the number.

The cost is real and worth stating. **The SLO threshold has to be a bucket boundary.**
Moving storefront to 350ms means adding a bucket, and every observation recorded before the
change is in the old bucket layout, so the historical SLI is not comparable across the
edit. That constraint is why `docs/conventions.md` fixes the bucket list once and shares it
across all three services rather than tuning it per service.

## Why probe traffic and 4xx are excluded

**Probe and scrape traffic.** `svc_common.metrics.make_metrics_middleware` returns early
for `EXEMPT_PREFIXES` — `/healthz`, `/readyz`, `/metrics`, `/internal/` — so those requests
are never recorded at all. For storefront on kind that is readiness every 5s plus liveness
every 10s across four pods (1.2 req/s) plus a scrape every 15s per pod (0.27 req/s), about
1.5 req/s against roughly 8 req/s of user traffic. Every one of them succeeds and every one
of them is fast. Counting them would deflate the availability error ratio by about 16% and
the latency ratio by considerably more, and both effects would grow with replica count —
so scaling up would improve the SLI without improving the service.

Excluding at the source rather than with a `route!=` matcher in PromQL means no SLI query,
dashboard panel or ad-hoc investigation can forget the exclusion. The same constant drives
the fault exemption (ADR 0007), so the two cannot drift apart.

**4xx.** The numerator is `status_code=~"5.."` only. A 404 for a product that does not
exist and catalog's 409 for insufficient stock are the service correctly refusing an
impossible request; counting them would let a load-generator bug requesting product 9999
burn a budget that is supposed to measure reliability. storefront passes catalog's 4xx
through unchanged rather than re-labelling them as 5xx
(`app/services/storefront/main.py`, `_upstream_error`), specifically so a correct rejection
does not burn storefront's budget either.

The blind spot this creates is worth naming: a service that answers every request with a
fast, confident 400 has a perfect availability SLI and a perfect latency SLI. Here that risk
is bounded because the load generator only requests ids 1–12, matching the twelve rows in
`002_seed.sql`, so a rise in 4xx is itself anomalous — but the general point stands, and it
is why a real service would carry a separate "correct responses" SLI rather than relying on
the absence of 5xx.

## The alert table

Generated by Sloth from the Google SRE Workbook's multi-window multi-burn-rate table, and
applied identically to all six SLOs.

| Severity | Burn rate | Long window | Short window | Budget consumed before firing |
|---|---|---|---|---|
| page | 14.4x | 1h | 5m | 2% |
| page | 6x | 6h | 30m | 5% |
| ticket | 3x | 24h | 2h | 10% |
| ticket | 1x | 72h | 6h | 10% |

Both severities are emitted under the same `alertname` (for example
`StorefrontAvailabilitySLOBurn`) and are distinguished by the `severity` label, which
carries `page` or `ticket`. Anything routing or querying these alerts has to match on
`severity`, not on the name.

Concrete thresholds per SLO, since `B × (1 − SLO)` is what the rule actually contains:

| SLO | page 14.4x | page 6x | ticket 3x | ticket 1x |
|---|---|---|---|---|
| storefront availability (99.5%) | 7.2% 5xx | 3.0% | 1.5% | 0.5% |
| catalog availability (99.9%) | 1.44% 5xx | 0.6% | 0.3% | 0.1% |
| recs availability (99.0%) | 14.4% 5xx | 6.0% | 3.0% | 1.0% |
| any latency SLO (99.0%) | 14.4% slow | 6.0% | 3.0% | 1.0% |

Catalog availability is the tightest row and the one to hold in mind when reading an
incident: 1.44% of requests failing is enough to page. It is also the row that makes short
total outages invisible to burn-rate alerting, which is what the symptom rules exist for.

One cosmetic artefact of reading the generated file: catalog's threshold appears as
`(14.4 * 0.0009999999999999432)` rather than `(14.4 * 0.001)`. That is Sloth computing
`1 - 0.999` in float64 and printing the result. The difference is about six parts in
10¹⁷ and has no effect on anything; it is worth recognising so nobody goes looking for a
bug in it.

## Honest limitations

**A 30-day window on a cluster that lives for hours.** `slo:sli_error:ratio_rate30d` is
computed as `sum_over_time(rate5m[30d]) / count_over_time(rate5m[30d])`. On kind, Prometheus
retains 24 hours and has no PVC, so the "30-day" number is an average over however long the
cluster has been up, and it resets to 1.0 every time Prometheus restarts. Anything derived
from it — `slo:period_burn_rate:ratio`,
`slo:period_error_budget_remaining:ratio`, and the "budget consumed" figure in every
incident report — should be read as "over the life of this cluster", never as a monthly
figure.

**That 30-day rule is an unweighted mean of 5-minute ratios, not a request-weighted 30-day
ratio.** Under the constant-arrival-rate load generator the two agree. Under variable
traffic they do not: a quiet five minutes with two errors counts exactly as much as a busy
five minutes with two hundred. Worth knowing before quoting "budget remaining" as if it
were a count of requests.

**The ticket windows have no data behind them.** Retention is 24h in both environments, so
`slo:sli_error:ratio_rate1d` and `ratio_rate3d` evaluate over at most the retained window.
The rules exist and produce numbers; the 72h ticket alert is a 24-hour alert
wearing a 72-hour label. Only the 1h/5m and 6h/30m page tiers are meaningful in either
environment, which is why every chaos scenario targets those.

**Honest windows make the chaos suite slow.** `error-spike-recs` now holds a fault for 20
minutes and then waits for the burn rate to settle for another 3, so one scenario is most of
half an hour and `run-all` with its 5-minute cooldowns is a long afternoon. That is the
correct cost of not faking the numbers, but it does mean the suite is not something to
demonstrate live — `docs/demo-script.md` uses `pod-kill-catalog` and `gitops-drift` for
that, and the long scenarios produce committed evidence instead.

**The symptom alerts may be too slow for the scenario that motivates them.** The same
"must outlast the window" property applies to them. `DependencyHardDown` needs a `[2m]` rate
window containing no successful call, then `for: 1m` — roughly **three minutes of complete
failure** before it fires. `ServiceReceivingNoTraffic` needs `rate(...[5m]) == 0` plus
`for: 3m`, so about eight. `pod-kill-postgres` injects for 30 seconds and its recovery is
bounded by pod rescheduling plus a local-path volume reattach, which on kind can finish
inside a minute. If Postgres comes back faster than three minutes, `DependencyHardDown` will
not fire either and the run records "not detected" — the same class of mistake the duration
re-derivation fixed, one alert further along. This has not been measured yet; the first
`pod-kill-postgres` run on a real cluster settles it, and the answer belongs in
`evidence/` whichever way it goes.

There is a related subtlety in the same scenario: `DependencyHardDown` will not see
`catalog → postgres`, because `PostgresStore.ping()` is not wrapped in the `_measured`
context manager that emits `dependency_requests_total`, and once catalog is NotReady it
serves no requests and therefore makes no instrumented database calls. The conjunct
`rate(dependency_requests_total{service="catalog"}[2m]) > 0` goes false. The signal that
survives is `storefront → catalog`, which is still the right one to page on — it just means
the alert names the outer edge of the failure, not the inner one.

**No multi-cluster aggregation.** One Prometheus per cluster, no Thanos or Mimir
(`planning.md` lists Thanos under "deliberately not doing"). kind numbers and EKS numbers
live in separate universes and cannot be summed, compared directly, or continued across a
cluster rebuild. Every row in `evidence/README.md` carries its cluster for that reason.

**A single Prometheus is a single point of failure for its own alerting.**
`prometheus.prometheusSpec.replicas: 1` with `storageSpec: {}` on kind, and Alertmanager
likewise at one replica. If Prometheus dies, the SLIs stop, the recording rules stop, the
alerts stop, and nothing pages about it — the chart's `Watchdog` alert exists precisely to
be the dead-man's switch, and it routes to the `null` receiver here, so it notifies nobody.
The one place this failure mode is safe is the canary: Argo Rollouts queries the same
Prometheus, and the AnalysisTemplate treats `isNaN` as a failure, so a Prometheus outage
during a rollout aborts the canary rather than promoting it blind.

**Steady synthetic traffic.** The load generator uses k6's `constant-arrival-rate` executor
at 8 req/s, deliberately, so a latency injection cannot reduce the offered rate and shrink
the SLI denominator exactly when the numerator grows. That makes the measurements
reproducible and makes them unlike production, where the same absolute error count means
something different at 3am than at noon and where a slow service sheds load on its own.
