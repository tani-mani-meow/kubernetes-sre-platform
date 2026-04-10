"""The scenario registry.

Scenarios live in chaos/scenarios.yaml so they can be reviewed as a set: the hypothesis
for each one is written before it runs, which is the difference between an experiment
and a demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CHAOS_DIR = Path(__file__).resolve().parent.parent
REGISTRY = CHAOS_DIR / "scenarios.yaml"


class ScenarioError(Exception):
    pass


@dataclass(frozen=True)
class Expectation:
    alert: str | None
    sli: str | None


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    hypothesis: str
    clusters: tuple[str, ...]
    target: str
    traffic_probe: str
    inject: dict[str, Any]
    duration_s: int
    expect: Expectation
    settle_s: int
    recovery_timeout_s: int
    detect_timeout_s: int

    @property
    def expects_alert(self) -> bool:
        return self.expect.alert is not None

    def runs_on(self, cluster: str) -> bool:
        return cluster in self.clusters

    def resolve_path(self, key: str) -> Path:
        """Resolve a manifest/script path recorded in the registry."""
        value = self.inject.get(key)
        if not value:
            raise ScenarioError(f"{self.id}: inject.{key} is not set")
        path = CHAOS_DIR / value
        if not path.exists():
            raise ScenarioError(f"{self.id}: {key} not found at {path}")
        return path


def load(path: Path | None = None) -> list[Scenario]:
    raw = yaml.safe_load((path or REGISTRY).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})

    scenarios = []
    seen: set[str] = set()
    for entry in raw.get("scenarios", []):
        scenario_id = entry["id"]
        if scenario_id in seen:
            raise ScenarioError(f"duplicate scenario id: {scenario_id}")
        seen.add(scenario_id)

        expect = entry.get("expect") or {}
        target = entry.get("target", "")
        scenarios.append(
            Scenario(
                id=scenario_id,
                title=entry["title"],
                hypothesis=" ".join(entry.get("hypothesis", "").split()),
                clusters=tuple(entry.get("clusters", ["kind"])),
                target=target,
                # Defaults to the target, but a fault aimed at Postgres or at a node has
                # to be measured through a service that actually emits HTTP metrics.
                traffic_probe=entry.get("traffic_probe", target),
                inject=entry["inject"],
                duration_s=int(entry.get("duration_s", 0)),
                expect=Expectation(alert=expect.get("alert"), sli=expect.get("sli")),
                settle_s=int(entry.get("settle_s", defaults.get("settle_s", 180))),
                recovery_timeout_s=int(
                    entry.get("recovery_timeout_s",
                              defaults.get("recovery_timeout_s", 900))),
                detect_timeout_s=int(
                    entry.get("detect_timeout_s", defaults.get("detect_timeout_s", 900))),
            )
        )
    return scenarios


def get(scenario_id: str) -> Scenario:
    scenarios = load()
    for scenario in scenarios:
        if scenario.id == scenario_id:
            return scenario
    known = ", ".join(s.id for s in scenarios)
    raise ScenarioError(f"unknown scenario '{scenario_id}'. Known: {known}")
