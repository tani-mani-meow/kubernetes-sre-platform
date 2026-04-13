"""Harness logic that can be checked without a cluster.

The parts that matter most here are the ones that decide whether a run counts as a
success. A harness that quietly reports a good MTTR for a broken experiment would make
every number in evidence/ worthless, so those rules get tested directly.
"""

from __future__ import annotations

import json

import pytest
import yaml

from chaos.harness import report, scenarios


def test_registry_loads_and_every_scenario_is_complete():
    loaded = scenarios.load()
    assert loaded, "registry should not be empty"
    for scenario in loaded:
        assert scenario.title, f"{scenario.id} has no title"
        assert scenario.hypothesis, f"{scenario.id} has no hypothesis"
        assert scenario.clusters, f"{scenario.id} declares no clusters"
        assert scenario.inject.get("kind"), f"{scenario.id} has no inject.kind"


def test_every_expected_alert_exists_in_the_generated_rules():
    # The registry names alerts as plain strings. If a Sloth spec is renamed and the
    # registry is not, the harness would wait ten minutes for an alert that can never
    # fire and report it as a detection failure.
    rules_dir = report.REPO_ROOT / "gitops" / "slo"
    defined = set()
    for path in rules_dir.rglob("*-rules.yaml"):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not doc:
                continue
            for group in doc.get("spec", {}).get("groups", []):
                for rule in group.get("rules", []):
                    if "alert" in rule:
                        defined.add(rule["alert"])

    expected = {s.expect.alert for s in scenarios.load() if s.expect.alert}
    assert expected <= defined, (
        f"alerts named in scenarios.yaml but never generated: {expected - defined}"
    )


def test_every_sli_reference_matches_a_generated_sloth_id():
    rules_dir = report.REPO_ROOT / "gitops" / "slo"
    sloth_ids = set()
    for path in rules_dir.rglob("*-rules.yaml"):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not doc:
                continue
            for group in doc.get("spec", {}).get("groups", []):
                for rule in group.get("rules", []):
                    sloth_id = rule.get("labels", {}).get("sloth_id")
                    if sloth_id:
                        sloth_ids.add(sloth_id)

    referenced = {s.expect.sli for s in scenarios.load() if s.expect.sli}
    assert referenced <= sloth_ids, (
        f"unknown sloth_id references: {referenced - sloth_ids}"
    )


def test_injector_kinds_are_all_implemented():
    from chaos.harness import injectors

    for scenario in scenarios.load():
        assert scenario.inject["kind"] in injectors._INJECTORS, (
            f"{scenario.id} uses inject.kind '{scenario.inject['kind']}' "
            f"which has no injector"
        )


def test_every_referenced_manifest_and_script_exists():
    # A missing file surfaces only when the scenario runs, which on a paid EKS capture
    # day means discovering it after the cluster is already billing.
    for scenario in scenarios.load():
        for key in ("manifest", "script"):
            if key in scenario.inject:
                scenario.resolve_path(key)


def test_unknown_scenario_lists_the_known_ones():
    with pytest.raises(scenarios.ScenarioError, match="pod-kill-catalog"):
        scenarios.get("no-such-scenario")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "n/a"), (0, "0s"), (45, "45s"), (60, "1m 00s"), (185, "3m 05s")],
)
def test_duration_formatting(seconds, expected):
    assert report.duration(seconds) == expected


def test_duration_is_ascii_safe():
    # This string is printed to a Windows console, where cp1252 cannot encode an em dash.
    for value in (None, 0, 3661):
        report.duration(value).encode("ascii")


def result(**overrides) -> report.RunResult:
    base = {
        "scenario_id": "test", "title": "Test", "hypothesis": "h", "cluster": "kind",
        "started_at": "2026-08-31T00:00:00+00:00", "injected": "nothing",
        "expected_alert": None,
    }
    base.update(overrides)
    return report.RunResult(**base)


def test_hypothesis_holds_when_an_expected_alert_fires():
    assert result(expected_alert="A", alert_fired="A").hypothesis_held


def test_hypothesis_holds_when_no_alert_was_expected_and_none_fired():
    assert result(expected_alert=None, alert_fired=None).hypothesis_held


def test_hypothesis_fails_when_an_expected_alert_does_not_fire():
    assert not result(expected_alert="A", alert_fired=None).hypothesis_held


def test_hypothesis_fails_on_an_unexpected_page():
    # A scenario predicted to be absorbed that pages anyway is a finding, not a pass.
    assert not result(expected_alert=None, alert_fired="Surprise").hypothesis_held


def test_a_scenario_that_never_recovered_is_not_reported_as_held():
    # gitops-drift predicts that Argo CD reverts a hand-edited replica count and
    # predicts nothing about alerts. An alerting-only check would call a run where the
    # revert never happened a success, and "GitOps self-healing works" would go into the
    # evidence directory on the strength of a run that showed the opposite.
    run = result(expected_alert=None, alert_fired=None, recovery_failed=True)
    assert not run.hypothesis_held
    assert run.alerting_as_predicted, "the alerting half did match; recovery is what failed"


def test_every_alert_expecting_scenario_can_actually_fire_its_alert():
    """The arithmetic that an earlier version of the registry got wrong.

    A Sloth page alert is `short_window AND long_window`, so the 1h window governs. At an
    observed error ratio e held for d seconds it reads e*d/3600, which must exceed
    14.4*(1-objective). Scenarios injecting 30% for 300s could never satisfy that, and
    every run would have recorded "not detected" with nothing actually wrong.
    """
    budgets = {
        "storefront-availability": 0.005,
        "catalog-availability": 0.001,
        "recs-availability": 0.010,
        "storefront-latency": 0.010,
        "catalog-latency": 0.010,
        "recs-latency": 0.010,
    }
    # Observed error ratio each scenario drives on its SLI. Latency and network faults
    # push essentially every request over the threshold.
    observed = {
        "latency-catalog": 1.0,
        "error-spike-recs": 0.8,
        "error-spike-storefront": 0.5,
    }

    for scenario in scenarios.load():
        if not scenario.expects_alert or scenario.id not in observed:
            continue
        budget = budgets[scenario.expect.sli]
        threshold = 14.4 * budget
        one_hour_ratio = observed[scenario.id] * scenario.duration_s / 3600
        assert one_hour_ratio > threshold, (
            f"{scenario.id}: at an observed ratio of {observed[scenario.id]} held for "
            f"{scenario.duration_s}s the 1h window reads {one_hour_ratio:.4f}, which "
            f"does not exceed the {threshold:.4f} its page alert requires. The alert "
            f"cannot fire, so the scenario would record a false 'not detected'."
        )


def test_budget_consumed_is_none_without_both_readings():
    assert result(budget_remaining_before=0.9).budget_consumed_pct is None
    assert result(budget_remaining_after=0.8).budget_consumed_pct is None


def test_budget_consumed_percentage():
    consumed = result(budget_remaining_before=0.90,
                      budget_remaining_after=0.85).budget_consumed_pct
    assert consumed == pytest.approx(5.0)


def test_report_records_a_missing_alert_prominently():
    run = result(expected_alert="CatalogAvailabilitySLOBurn")
    rendered = report._render(run, "2026-08-31")
    assert "was expected but did not fire" in rendered
    assert "hypothesis did not hold" in rendered.lower()


def test_report_includes_the_hypothesis_verbatim():
    run = result(hypothesis="Two replicas absorb one pod kill.")
    assert "Two replicas absorb one pod kill." in report._render(run, "2026-08-31")


def test_summary_keeps_only_the_latest_run_per_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(report, "EVIDENCE_DIR", tmp_path / "evidence")
    report.OUT_DIR.mkdir(parents=True)

    for index, mttr in enumerate([99.0, 42.0]):
        payload = {
            "scenario_id": "pod-kill-catalog", "title": "Catalog pod killed",
            "cluster": "kind", "started_at": "2026-08-31T00:00:00+00:00",
            "expected_alert": None, "alert_fired": None,
            "time_to_detect_s": None, "time_to_recover_s": mttr,
        }
        (report.OUT_DIR / f"run-{index}.json").write_text(json.dumps(payload))

    body = report.rebuild_summary().read_text(encoding="utf-8")
    assert "42s" in body
    assert "1m 39s" not in body, "an older run should not survive alongside the newer one"
