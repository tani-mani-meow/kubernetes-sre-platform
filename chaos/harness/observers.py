"""Reading the platform's own signals.

The harness measures recovery using the same queries the alerts use. That is
deliberate: a harness with its own private definition of "healthy" would happily report
a two-minute MTTR for an incident the on-call dashboard still showed as burning.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

PROMETHEUS_SERVICE = "svc/kube-prometheus-stack-prometheus"
ALERTMANAGER_SERVICE = "svc/kube-prometheus-stack-alertmanager"
MONITORING_NS = "monitoring"


class ObserverError(Exception):
    pass


@dataclass
class Endpoints:
    prometheus: str
    alertmanager: str


@contextmanager
def port_forwards(prometheus_port: int = 19090, alertmanager_port: int = 19093):
    """Hold open port-forwards to Prometheus and Alertmanager.

    Non-default local ports so a running `scripts/urls.sh` does not collide - the
    harness is usually run while the dashboards are already being watched.
    """
    processes = []
    try:
        for service, local, remote in (
            (PROMETHEUS_SERVICE, prometheus_port, 9090),
            (ALERTMANAGER_SERVICE, alertmanager_port, 9093),
        ):
            processes.append(subprocess.Popen(
                ["kubectl", "-n", MONITORING_NS, "port-forward", service,
                 f"{local}:{remote}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))

        endpoints = Endpoints(
            prometheus=f"http://127.0.0.1:{prometheus_port}",
            alertmanager=f"http://127.0.0.1:{alertmanager_port}",
        )
        _await_ready(endpoints)
        yield endpoints
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _await_ready(endpoints: Endpoints, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{endpoints.prometheus}/-/ready", timeout=2.0).raise_for_status()
            httpx.get(f"{endpoints.alertmanager}/-/ready", timeout=2.0).raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(1.0)
    raise ObserverError(
        "could not reach Prometheus and Alertmanager through kubectl port-forward. "
        "Is the monitoring stack running? (kubectl -n monitoring get pods)"
    )


class Prometheus:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=10.0)

    def query(self, promql: str) -> list[dict]:
        response = self._client.get("/api/v1/query", params={"query": promql})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ObserverError(f"query failed: {promql}: {payload.get('error')}")
        return payload["data"]["result"]

    def scalar(self, promql: str) -> float | None:
        """First sample value, or None when the query returns nothing.

        None and 0.0 mean very different things here: 0.0 is "measured, healthy",
        None is "no data" - usually a service with no traffic, which must never be
        reported as recovered.
        """
        results = self.query(promql)
        if not results:
            return None
        return float(results[0]["value"][1])

    def burn_rate(self, sloth_id: str) -> float | None:
        """Current burn rate for an SLO: >1 means the budget is being spent too fast."""
        return self.scalar(
            f'slo:current_burn_rate:ratio{{sloth_id="{sloth_id}"}}'
        )

    def budget_remaining(self, sloth_id: str) -> float | None:
        return self.scalar(
            f'slo:period_error_budget_remaining:ratio{{sloth_id="{sloth_id}"}}'
        )

    def request_rate(self, service: str) -> float | None:
        return self.scalar(
            f'sum(rate(http_requests_total{{service="{service}"}}[1m]))'
        )

    def close(self) -> None:
        self._client.close()


class Alertmanager:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=10.0)

    #: An inhibited alert has still fired - Alertmanager is only declining to notify on
    #: it. Treating "suppressed" as "did not fire" would report a detection failure for
    #: an alert that is sitting right there in the UI.
    #:
    #: This is not hypothetical for Sloth output: it emits the page and ticket alerts
    #: under one alertname, so any inhibit rule pairing their severities would suppress
    #: the ticket. kube-prometheus-stack's default rules match severity
    #: critical/warning/info and Sloth uses page/ticket, so nothing is inhibited today -
    #: but the harness should not quietly depend on that.
    FIRING_STATES = frozenset({"active", "suppressed"})

    def firing(self) -> list[dict]:
        response = self._client.get(
            "/api/v2/alerts",
            params={"active": "true", "inhibited": "true", "silenced": "false"},
        )
        response.raise_for_status()
        return [
            alert for alert in response.json()
            if alert.get("status", {}).get("state") in self.FIRING_STATES
        ]

    def fingerprints(self) -> set[str]:
        """Identities of everything firing right now.

        Taken before a fault is injected so an alert left over from an earlier scenario
        cannot be counted as this run's detection. Sloth's ticket tiers use 24h and 72h
        windows, so one stays true for hours after the incident that caused it - and
        `run-all` starts the next scenario five minutes later.
        """
        return {alert.get("fingerprint", "") for alert in self.firing()}

    def is_firing(self, alert_name: str,
                  exclude: set[str] | None = None) -> dict | None:
        for alert in self.firing():
            if alert.get("labels", {}).get("alertname") != alert_name:
                continue
            if exclude and alert.get("fingerprint", "") in exclude:
                continue
            return alert
        return None

    @staticmethod
    def state_of(alert: dict) -> str:
        return alert.get("status", {}).get("state", "unknown")

    def close(self) -> None:
        self._client.close()
