#!/usr/bin/env python3
"""Chaos experiment runner.

    python chaos/harness/run.py list
    python chaos/harness/run.py run pod-kill-catalog
    python chaos/harness/run.py run-all --cluster eks
    python chaos/harness/run.py report

One run is: record the clock, inject a fault, watch for the alert the scenario predicts,
remove the fault, then wait for the SLO burn rate to come back under 1.0 and stay there.
The output is a measured time-to-detect and MTTR plus a pre-filled incident report.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chaos.harness import injectors, observers, report, scenarios
from chaos.harness.observers import Alertmanager, Prometheus
from chaos.harness.scenarios import Scenario

#: A burn rate of 1.0 is spending error budget exactly as fast as the SLO allows, so
#: this is the line between "recovering" and "recovered". How long it has to stay under
#: the line is per-scenario (`settle_s`), because it has to exceed the SLI's rate window.
HEALTHY_BURN_RATE = 1.0
POLL_INTERVAL_S = 5.0


def log(message: str = "") -> None:
    if message:
        stamp = datetime.now(tz=UTC).strftime("%H:%M:%S")
        print(f"  [{stamp}] {message}", flush=True)
    else:
        print(flush=True)


def cmd_list(args: argparse.Namespace) -> int:
    print()
    print(f"  {'ID':<24} {'CLUSTERS':<12} {'EXPECTED ALERT':<32} TITLE")
    print(f"  {'-' * 24} {'-' * 12} {'-' * 32} {'-' * 40}")
    for scenario in scenarios.load():
        alert = scenario.expect.alert or "(none - should be absorbed)"
        print(f"  {scenario.id:<24} {','.join(scenario.clusters):<12} "
              f"{alert:<32} {scenario.title}")
    print()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = report.rebuild_summary()
    runs = len(report.load_runs())
    print(f"  wrote {path.relative_to(report.REPO_ROOT)} from {runs} recorded run(s)")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    scenario = scenarios.get(args.scenario)
    if not scenario.runs_on(args.cluster):
        print(f"  scenario '{scenario.id}' does not run on '{args.cluster}' "
              f"(supported: {', '.join(scenario.clusters)})", file=sys.stderr)
        return 2
    return 0 if execute(scenario, args.cluster) else 1


def cmd_run_all(args: argparse.Namespace) -> int:
    selected = [s for s in scenarios.load() if s.runs_on(args.cluster)]
    log(f"running {len(selected)} scenarios on {args.cluster}")

    failures = []
    for index, scenario in enumerate(selected, start=1):
        print()
        print(f"{'=' * 78}")
        print(f"  [{index}/{len(selected)}] {scenario.id}")
        print(f"{'=' * 78}")
        if not execute(scenario, args.cluster):
            failures.append(scenario.id)
        if index < len(selected):
            # Scenarios must not overlap: a residual burn from the previous fault would
            # be attributed to the next one.
            log(f"cooling down for {args.cooldown}s before the next scenario")
            time.sleep(args.cooldown)

    report.rebuild_summary()
    print()
    if failures:
        log(f"hypotheses that did not hold: {', '.join(failures)}")
        log("that is a result, not an error - read the reports")
    return 0


def execute(scenario: Scenario, cluster: str) -> bool:
    """Run one scenario end to end. Returns whether the hypothesis held."""
    result = report.RunResult(
        scenario_id=scenario.id,
        title=scenario.title,
        hypothesis=scenario.hypothesis,
        cluster=cluster,
        started_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        injected="",
        expected_alert=scenario.expect.alert,
    )

    print()
    log(f"scenario: {scenario.title}")
    log(f"hypothesis: {scenario.hypothesis}")
    print()

    try:
        with observers.port_forwards() as endpoints:
            prometheus = Prometheus(endpoints.prometheus)
            alertmanager = Alertmanager(endpoints.alertmanager)
            try:
                _run_measurement(scenario, prometheus, alertmanager, result)
            finally:
                prometheus.close()
                alertmanager.close()
    except Exception as exc:
        # A run can take twenty minutes. Losing all of it because a port-forward died
        # during a pod-kill - which is a routine thing for a pod-kill to do - would mean
        # re-running on a cluster that is billing by the hour. The partial record is
        # written and clearly marked instead.
        result.notes.append(f"Run aborted by an error: {exc!r}")
        result.record("run aborted", "harness")
        result.outcome = "aborted"
        json_path = report.write_json(result)
        report_path = report.write_report(result)
        log(f"run aborted: {exc}")
        _summarise(result, json_path, report_path)
        raise

    result.outcome = "hypothesis held" if result.hypothesis_held else "hypothesis failed"
    json_path = report.write_json(result)
    report_path = report.write_report(result)

    _summarise(result, json_path, report_path)
    return result.hypothesis_held


def _run_measurement(scenario: Scenario, prometheus: Prometheus,
                     alertmanager: Alertmanager, result: report.RunResult) -> None:
    _check_baseline(scenario, prometheus)

    if scenario.expect.sli:
        result.budget_remaining_before = prometheus.budget_remaining(scenario.expect.sli)
        baseline_burn = prometheus.burn_rate(scenario.expect.sli)
        result.burn_rate_before = baseline_burn
        if baseline_burn is not None and baseline_burn >= HEALTHY_BURN_RATE:
            result.notes.append(
                f"{scenario.expect.sli} was already burning at {baseline_burn:.2f} "
                f"before injection. Any recovery time measured from here is really the "
                f"tail of a previous incident. Let the cluster settle and re-run."
            )
            log(f"WARNING: {scenario.expect.sli} is already unhealthy "
                f"(burn rate {baseline_burn:.2f})")

    # Snapshot what is already firing. Sloth emits the page and ticket rules under one
    # alertname, and the ticket windows stay true for hours - so an alert left over from
    # the previous scenario would otherwise be matched on the first poll and recorded as
    # a sub-second detection. A fabricated detection time is exactly what this project
    # exists not to produce.
    pre_existing = alertmanager.fingerprints()
    if pre_existing:
        log(f"{len(pre_existing)} alert(s) already firing; they will not count as "
            f"detections for this run")

    injector = injectors.for_scenario(scenario)
    t0 = time.monotonic()

    with injector.active() as description:
        result.injected = description
        result.record("fault injected (T0)", "harness")
        log(f"injected: {description}")

        deadline = t0 + max(scenario.duration_s, 1)
        if scenario.expects_alert:
            _watch_for_alert(scenario, alertmanager, result, t0, pre_existing, deadline)
        else:
            _watch_for_unexpected_alert(scenario, alertmanager, result, t0,
                                        pre_existing, deadline)

        remaining = deadline - time.monotonic()
        if remaining > 0:
            log(f"holding the fault for another {remaining:.0f}s")
            time.sleep(remaining)

    result.fault_duration_s = time.monotonic() - t0

    if injector.cleanup_failed:
        # Do not record "fault removed" when it was not. A recovery measured with the
        # fault still live is worse than no measurement at all.
        result.notes.append(
            f"Fault removal FAILED: {injector.cleanup_failed}. Everything measured "
            f"after this point is suspect, and the cluster may still be faulted - "
            f"check before running anything else."
        )
        result.record("fault removal FAILED", "harness")
        log(f"WARNING: fault removal failed: {injector.cleanup_failed}")
    else:
        result.record("fault removed", "harness")
        log("fault removed")

    _await_recovery(scenario, prometheus, result, t0)

    if scenario.expect.sli:
        result.budget_remaining_after = prometheus.budget_remaining(scenario.expect.sli)


def _check_baseline(scenario: Scenario, prometheus: Prometheus) -> None:
    """Refuse to start without traffic.

    With no requests flowing, every ratio SLI is 0/0. The experiment would inject a
    fault, observe nothing, and report a flawless recovery - the most misleading
    possible result.

    Measured against `traffic_probe`, not `target`: a fault aimed at Postgres or at a
    node has to be observed through a service that actually emits HTTP metrics.
    """
    probe = scenario.traffic_probe
    if probe in {"cluster", "", "postgres"}:
        return
    rate = prometheus.request_rate(probe)
    if rate is None or rate < 0.1:
        raise SystemExit(
            f"  refusing to run: {probe} is serving "
            f"{'no' if rate is None else f'{rate:.2f} req/s of'} traffic.\n"
            f"  Burn-rate SLIs need a baseline. Check the load generator:\n"
            f"    kubectl -n demo get deploy trafficgen"
        )
    log(f"baseline traffic on {probe}: {rate:.1f} req/s")


def _watch_for_alert(scenario: Scenario, alertmanager: Alertmanager,
                     result: report.RunResult, t0: float,
                     pre_existing: set[str], hold_until: float) -> None:
    alert_name = scenario.expect.alert
    log(f"watching Alertmanager for {alert_name}")
    # Never stop watching before the fault is due to be removed: a detect timeout
    # shorter than the fault would hold the fault past its declared duration while
    # waiting, which is how `duration_s` silently became a floor rather than a bound.
    deadline = max(t0 + scenario.detect_timeout_s, hold_until)

    while time.monotonic() < deadline:
        alert = alertmanager.is_firing(alert_name, exclude=pre_existing)
        if alert:
            result.time_to_detect_s = time.monotonic() - t0
            result.alert_fired = alert_name
            severity = alert.get("labels", {}).get("severity", "?")
            state = Alertmanager.state_of(alert)
            result.alert_severity = severity
            result.record(f"{alert_name} firing ({severity}, {state})", "Alertmanager")
            log(f"{alert_name} fired after "
                f"{report.duration(result.time_to_detect_s)} (severity: {severity})")
            return
        time.sleep(POLL_INTERVAL_S)

    result.record(f"{alert_name} did not fire", "Alertmanager")
    result.notes.append(
        f"Expected {alert_name} to fire and it did not. Check the arithmetic before "
        f"assuming the alert is broken: a page needs BOTH windows of a tier over the "
        f"threshold, and the long window is the binding one. At an observed error ratio "
        f"e held for d minutes the 1h window reads e*d/60, which must exceed "
        f"14.4*(1-objective). Verify directly with: "
        f'slo:current_burn_rate:ratio{{sloth_id="{scenario.expect.sli}"}}'
    )
    log(f"{alert_name} did NOT fire")


def _watch_for_unexpected_alert(scenario: Scenario, alertmanager: Alertmanager,
                                result: report.RunResult, t0: float,
                                pre_existing: set[str], hold_until: float) -> None:
    """Watch a scenario predicted to be absorbed, and notice if it pages anyway.

    Watches for the whole fault duration. An earlier version watched 60s regardless,
    which no burn-rate alert can cross from silent to firing inside - so every
    "absorbed" verdict was guaranteed before the run started. That asymmetry (minutes
    to confirm an expected alert, one minute to rule out an unexpected one) biased the
    evidence toward the flattering answer.
    """
    log("expecting no page; watching for one anyway")

    while time.monotonic() < hold_until:
        for alert in alertmanager.firing():
            labels = alert.get("labels", {})
            name = labels.get("alertname", "")
            fingerprint = alert.get("fingerprint", "")
            if fingerprint in pre_existing:
                continue
            # Only alerts about this scenario's blast radius count. A stale alert for an
            # unrelated service must not flip an absorbed verdict.
            relevant = labels.get("service") in {scenario.target,
                                                 scenario.traffic_probe}
            if not relevant and scenario.target != "cluster":
                continue
            if labels.get("severity") != "page":
                continue

            result.alert_fired = name
            result.alert_severity = labels.get("severity")
            result.time_to_detect_s = time.monotonic() - t0
            result.record(f"unexpected alert: {name}", "Alertmanager")
            result.notes.append(
                f"{name} fired during a scenario predicted to be absorbed. Either the "
                f"redundancy is not doing what the hypothesis assumed, or the alert "
                f"threshold is tighter than intended. Both are findings."
            )
            log(f"unexpected alert fired: {name}")
            return
        time.sleep(POLL_INTERVAL_S)

    log("no page fired, as predicted")


def _await_recovery(scenario: Scenario, prometheus: Prometheus,
                    result: report.RunResult, t0: float) -> None:
    if not scenario.expect.sli:
        _await_drift_revert(scenario, result, t0)
        return

    log(f"waiting for {scenario.expect.sli} burn rate to settle under "
        f"{HEALTHY_BURN_RATE} for {scenario.settle_s}s")

    deadline = time.monotonic() + scenario.recovery_timeout_s
    healthy_since: float | None = None
    peak_burn = result.burn_rate_peak or 0.0

    while time.monotonic() < deadline:
        burn = prometheus.burn_rate(scenario.expect.sli)

        if burn is not None:
            peak_burn = max(peak_burn, burn)

        if burn is None:
            # No data is not health. A service with no traffic produces no SLI, and
            # calling that "recovered" would be the harness lying to itself.
            healthy_since = None
        elif burn < HEALTHY_BURN_RATE:
            if healthy_since is None:
                healthy_since = time.monotonic()
                log(f"burn rate back under {HEALTHY_BURN_RATE} ({burn:.2f}), "
                    f"confirming for {scenario.settle_s}s")
            elif time.monotonic() - healthy_since >= scenario.settle_s:
                result.burn_rate_peak = peak_burn
                # Only call it a recovery if there was something to recover from.
                # Otherwise the first poll sets healthy_since, settle_s later this writes
                # a "MTTR" roughly equal to the fault duration - a measured-looking
                # number for a service that never broke. That is the single most
                # misleading thing this harness could publish.
                if peak_burn >= HEALTHY_BURN_RATE:
                    result.time_to_recover_s = healthy_since - t0
                    result.record("SLI healthy and holding", "Prometheus")
                    log(f"recovered: MTTR {report.duration(result.time_to_recover_s)}")
                else:
                    result.record(
                        f"SLI never left healthy (peak burn {peak_burn:.2f})",
                        "Prometheus")
                    result.notes.append(
                        f"{scenario.expect.sli} never exceeded a burn rate of 1.0 during "
                        f"this run (peak {peak_burn:.2f}), so there was no recovery to "
                        f"measure and no MTTR is reported. Either the fault did not "
                        f"affect this SLI - check that expect.sli names the service the "
                        f"fault actually reaches - or the platform absorbed it entirely."
                    )
                    log(f"SLI never broke (peak burn {peak_burn:.2f}); no MTTR recorded")
                return
        else:
            if healthy_since is not None:
                log(f"burn rate rose again ({burn:.2f}), restarting the settle window")
            healthy_since = None

        time.sleep(POLL_INTERVAL_S)

    result.burn_rate_peak = peak_burn
    result.notes.append(
        f"Burn rate did not settle below {HEALTHY_BURN_RATE} within "
        f"{scenario.recovery_timeout_s}s (peak {peak_burn:.2f}). The system may not have "
        f"recovered, or the 30-day burn rate is still carrying earlier damage."
    )
    result.record("recovery not observed within timeout", "Prometheus")
    log("did not observe a clean recovery within the timeout")


def _await_drift_revert(scenario: Scenario, result: report.RunResult,
                        t0: float) -> None:
    """Recovery for the GitOps scenario is 'Git won', not an SLI."""
    verify = scenario.inject.get("verify_reverted")
    if not verify:
        return

    log("waiting for Argo CD to revert the change")
    deadline = time.monotonic() + scenario.recovery_timeout_s

    while time.monotonic() < deadline:
        if injectors.resource_matches(
            verify["namespace"], verify["resource"],
            verify["jsonpath"], str(verify["expected"])
        ):
            result.time_to_recover_s = time.monotonic() - t0
            result.record("Argo CD reverted the change", "kubectl")
            log(f"reverted after {report.duration(result.time_to_recover_s)}")
            return
        time.sleep(2.0)

    result.recovery_failed = True
    result.record("change was NOT reverted", "kubectl")
    result.notes.append(
        "Argo CD did not revert the manual change within "
        f"{scenario.recovery_timeout_s}s. Check that the Application has "
        "syncPolicy.automated.selfHeal enabled - this scenario's entire hypothesis is "
        "about self-healing, so this is a failed hypothesis, not a slow one."
    )
    log("the change was NOT reverted")


def _summarise(result: report.RunResult, json_path: Path, report_path: Path) -> None:
    print()
    print(f"  {'-' * 60}")
    print(f"  {result.title}")
    print(f"  {'-' * 60}")
    print(f"  time to detect     {report.duration(result.time_to_detect_s)}")
    print(f"  time to recover    {report.duration(result.time_to_recover_s)}")
    print(f"  alert              {result.alert_fired or 'none'}")
    consumed = result.budget_consumed_pct
    if consumed is not None:
        print(f"  budget consumed    {consumed:.2f}%")
    print(f"  hypothesis         {'held' if result.hypothesis_held else 'DID NOT HOLD'}")
    print()
    for note in result.notes:
        print(f"  note: {note}")
    if result.notes:
        print()
    print(f"  report  {report_path.relative_to(report.REPO_ROOT)}")
    print(f"  data    {json_path.relative_to(report.REPO_ROOT)}")
    print()
    print("  Add screenshots to the report's img/ directory, then run 'make evidence'.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster", default="kind",
                        help="which cluster this is running against (default: kind)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list scenarios").set_defaults(func=cmd_list)
    sub.add_parser("report", help="rebuild evidence/README.md").set_defaults(
        func=cmd_report)

    run_parser = sub.add_parser("run", help="run one scenario")
    run_parser.add_argument("scenario")
    run_parser.set_defaults(func=cmd_run)

    all_parser = sub.add_parser("run-all", help="run every scenario for the cluster")
    all_parser.add_argument("--cooldown", type=int, default=300,
                            help="seconds between scenarios (default: 300)")
    all_parser.set_defaults(func=cmd_run_all)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted - injectors have been cleaned up", file=sys.stderr)
        return 130
    except (scenarios.ScenarioError, injectors.InjectionError,
            observers.ObserverError) as exc:
        print(f"\n  error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
