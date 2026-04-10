"""Applying and removing faults.

Four strategies, chosen per scenario by `inject.kind`:

  chaos-mesh  a CRD applied to the cluster; Chaos Mesh does the work
  fault-api   PUT /internal/faults on the service itself
  kubectl     an arbitrary kubectl command (used for the GitOps drift scenario)
  script      a shell script, for node-level faults kubectl cannot express in one call

Every injector is a context manager, so the fault is removed on the way out whether the
run succeeded, failed, or was interrupted. An experiment that leaves 500ms of latency
injected because someone pressed Ctrl-C would poison every measurement after it.
"""

from __future__ import annotations

import json
import subprocess
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager

import httpx

from .scenarios import Scenario, ScenarioError

APP_NAMESPACE = "demo"


class InjectionError(Exception):
    pass


def run_kubectl(args: list[str], check: bool = True,
                capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=capture, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise InjectionError(
            f"kubectl {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


class Injector(ABC):
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        # Set here as well as in active() so the attribute always exists: run.py reads it
        # after the context manager exits, including on the path where apply() raised.
        self.cleanup_failed: str | None = None

    @abstractmethod
    def apply(self) -> str:
        """Apply the fault. Returns a human-readable description of what was done."""

    @abstractmethod
    def remove(self) -> None:
        """Undo the fault. Must be safe to call even if apply() failed part way."""

    @contextmanager
    def active(self):
        """Apply the fault, hand control back, and always attempt removal.

        apply() is inside the try on purpose. FaultApiInjector configures each replica in
        turn, so a PUT that fails against the second pod leaves the first one faulted; if
        apply() raised outside the try, remove() would never run and every subsequent
        scenario would measure a service with a permanent injected fault. Each remove()
        is written to tolerate a half-applied state.

        Removal failure is reported through `cleanup_failed` rather than swallowed - the
        caller has to know, because a recovery measured while the fault is still live is
        worse than no measurement.
        """
        self.cleanup_failed: str | None = None
        try:
            description = self.apply()
            yield description
        finally:
            try:
                self.remove()
            except Exception as exc:  # cleanup must not mask the original failure
                self.cleanup_failed = str(exc)


class ChaosMeshInjector(Injector):
    """Applies a Chaos Mesh custom resource.

    Chaos Mesh CRs carry their own duration, but the harness deletes them explicitly
    anyway: relying on the controller to expire an experiment means the recovery clock
    starts at a time the harness never observed.
    """

    def apply(self) -> str:
        self._manifest = self.scenario.resolve_path("manifest")
        run_kubectl(["apply", "-f", str(self._manifest)])
        return f"applied Chaos Mesh experiment {self._manifest.name}"

    def remove(self) -> None:
        manifest = getattr(self, "_manifest", None)
        if manifest is not None:
            run_kubectl(["delete", "-f", str(manifest), "--ignore-not-found"],
                        check=False)


class FaultApiInjector(Injector):
    """Sets fault config on a service through its cluster-internal endpoint.

    Used where the fault is application behaviour rather than infrastructure. It is the
    same mechanism the broken canary uses, so a scenario and a bad deploy produce
    genuinely comparable signals.
    """

    def apply(self) -> str:
        service = self.scenario.inject["service"]
        faults = self.scenario.inject.get("faults", {})
        self._service = service
        self._forward = _ServicePortForward(service)
        # Assigned before start() so that a port-forward which comes up and then fails
        # readiness is still torn down by remove().
        self._forward.start()
        self._put(faults)
        return f"set faults on {service}: {json.dumps(faults, sort_keys=True)}"

    def remove(self) -> None:
        forward = getattr(self, "_forward", None)
        if forward is None:
            return
        try:
            self._put({"reset": True})
        finally:
            forward.stop()

    def _put(self, payload: dict) -> None:
        # Every replica has its own in-process fault config, and a port-forward to a
        # Service load-balances across pods - so one PUT would configure one random pod.
        # Address each pod directly instead.
        #
        # Every pod is attempted even if one fails, so a partial application does not
        # leave the remaining replicas untouched while the caller believes the whole
        # service is faulted. The first failure is re-raised afterwards.
        failures = []
        for pod_port in self._forward.pod_ports:
            try:
                response = httpx.put(f"http://127.0.0.1:{pod_port}/internal/faults",
                                     json=payload, timeout=5.0)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                failures.append(f"port {pod_port}: {exc}")
        if failures:
            raise InjectionError(
                f"could not set faults on every {self._service} replica: "
                + "; ".join(failures)
            )


class _ServicePortForward:
    """Port-forward to every pod backing a service, so faults hit all replicas."""

    def __init__(self, service: str, base_port: int = 18100) -> None:
        self.service = service
        self.base_port = base_port
        self.pod_ports: list[int] = []
        self._processes: list[subprocess.Popen] = []

    def start(self) -> None:
        result = run_kubectl([
            "-n", APP_NAMESPACE, "get", "pods",
            "-l", f"app.kubernetes.io/name={self.service}",
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[*].metadata.name}",
        ])
        pods = result.stdout.split()
        if not pods:
            raise InjectionError(f"no running pods for service '{self.service}'")

        for index, pod in enumerate(pods):
            port = self.base_port + index
            self._processes.append(subprocess.Popen(
                ["kubectl", "-n", APP_NAMESPACE, "port-forward", f"pod/{pod}",
                 f"{port}:8000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            self.pod_ports.append(port)

        self._await_ready()

    def _await_ready(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        pending = list(self.pod_ports)
        while pending and time.monotonic() < deadline:
            for port in list(pending):
                try:
                    httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
                    pending.remove(port)
                except httpx.HTTPError:
                    time.sleep(0.5)
        if pending:
            # Tear down what did come up. Without this the subprocesses outlive the run
            # and hold their local ports, so the next scenario's port-forward silently
            # binds elsewhere or fails.
            self.stop()
            raise InjectionError(
                f"port-forward to {self.service} did not become ready on {pending}")

    def stop(self) -> None:
        for process in self._processes:
            process.terminate()
        for process in self._processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


class KubectlInjector(Injector):
    """Runs a kubectl command. Removal is a no-op by design.

    The GitOps drift scenario uses this: the whole point is that nothing undoes the
    change except Argo CD, so an injector that reverted it would be testing itself.
    """

    def apply(self) -> str:
        command = self.scenario.inject["command"]
        run_kubectl(command)
        return f"ran kubectl {' '.join(command)}"

    def remove(self) -> None:
        return None


class ScriptInjector(Injector):
    """Runs a shell script that applies a fault and can undo it.

    The script is called with `apply` and later `remove`, so node drains and canary
    rollouts keep their setup and teardown in one readable file.
    """

    def apply(self) -> str:
        self._script = self.scenario.resolve_path("script")
        self._run("apply")
        return f"ran {self._script.name} apply"

    def remove(self) -> None:
        script = getattr(self, "_script", None)
        if script is not None:
            self._run("remove", check=False)

    def _run(self, phase: str, check: bool = True) -> None:
        result = subprocess.run(
            ["bash", str(self._script), phase],
            text=True, check=False,
        )
        if check and result.returncode != 0:
            raise InjectionError(
                f"{self._script.name} {phase} exited {result.returncode}")


_INJECTORS: dict[str, type[Injector]] = {
    "chaos-mesh": ChaosMeshInjector,
    "fault-api": FaultApiInjector,
    "kubectl": KubectlInjector,
    "script": ScriptInjector,
}


def for_scenario(scenario: Scenario) -> Injector:
    kind = scenario.inject.get("kind")
    try:
        return _INJECTORS[kind](scenario)
    except KeyError:
        raise ScenarioError(
            f"{scenario.id}: unknown inject.kind '{kind}'. "
            f"Known: {', '.join(sorted(_INJECTORS))}"
        ) from None


def resource_matches(namespace: str, resource: str, jsonpath: str,
                     expected: str) -> bool:
    """Used by the drift scenario to detect that Argo CD has reverted a change."""
    result = run_kubectl(
        ["-n", namespace, "get", resource, "-o", f"jsonpath={jsonpath}"],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == expected
