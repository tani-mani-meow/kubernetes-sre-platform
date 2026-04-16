# Runbooks

Linked from the `runbook_url` annotation on every generated burn-rate alert
(`gitops/slo/specs/*.yaml`), so the filenames are part of the alert contract and cannot be
renamed without regenerating the rules.

| Alert | Source | Runbook |
|---|---|---|
| `StorefrontAvailabilitySLOBurn`, `CatalogAvailabilitySLOBurn`, `RecsAvailabilitySLOBurn` | Sloth (`gitops/slo/generated/`) | [availability-burn.md](availability-burn.md) |
| `ServiceHardDown`, `ServiceReceivingNoTraffic`, `DependencyHardDown`, `SLOBudgetNearlyExhausted` | hand-written (`gitops/slo/handwritten/`) | [availability-burn.md](availability-burn.md) |
| `StorefrontLatencySLOBurn`, `CatalogLatencySLOBurn`, `RecsLatencySLOBurn` | Sloth (`gitops/slo/generated/`) | [latency-burn.md](latency-burn.md) |

Both severities of an SLO share one `alertname`; `severity` carries `page` or `ticket`. The
hand-written rules carry `alert_kind: symptom`, which is the quickest way to tell the two
families apart in Alertmanager.

Before either runbook is useful, `bash scripts/urls.sh` opens the port-forwards to Grafana
(3000), Argo CD (8080), Prometheus (9090), Alertmanager (9093) and the Rollouts dashboard
(3100).

Both runbooks use `kubectl argo rollouts` for the canary paths. That is a kubectl plugin and
is not installed by `make demo`; the Rollouts dashboard on 3100 covers inspection, but
`abort` and `undo` need the plugin. Install it before you need it, not during an incident.

Two things that apply to every alert here and are easy to get wrong:

- **A pod serving injected 500s still reports Ready.** Fault injection exempts the probe
  endpoints on purpose (ADR 0007), so `kubectl get pods` is not a health check in this
  platform.
- **Restarting pods to clear a dependency outage makes the incident longer and erases the
  signal.** Both runbooks say why, in detail.
- **Silence is not health.** A `0/0` SLI — a service with no traffic — cannot cross any
  burn-rate threshold. `ServiceReceivingNoTraffic` is the alert for that case, and it is a
  page, not a curiosity.

The alert thresholds themselves are derived in [`../slo-methodology.md`](../slo-methodology.md).
