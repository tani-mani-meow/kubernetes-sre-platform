#!/usr/bin/env bash
# Cordon and drain one worker node while it is serving traffic.
#
# Deliberately a plain kubectl script rather than a Chaos Mesh experiment: a drain is
# not a fault, it is the routine operation every cluster upgrade performs. Wrapping it
# in a chaos framework would obscure that the thing being tested is whether the pod
# disruption budgets and topology spread constraints actually let normal maintenance
# happen without an incident.
#
# Called by the harness as: node-drain.sh apply | remove

set -euo pipefail

STATE_FILE="${TMPDIR:-/tmp}/k8s-slo-platform-drained-node"

# Never drain a control-plane node. On kind that is where the ingress controller runs,
# so draining it would take down the entry point and the experiment would measure the
# harness losing its own access rather than the platform's resilience.
pick_worker() {
  kubectl get nodes \
    -l '!node-role.kubernetes.io/control-plane' \
    -o jsonpath='{range .items[?(@.spec.unschedulable!=true)]}{.metadata.name}{"\n"}{end}' \
    | head -1
}

case "${1:-}" in
  apply)
    node="$(pick_worker)"
    if [[ -z "$node" ]]; then
      echo "no schedulable worker node found" >&2
      exit 1
    fi
    echo "$node" > "$STATE_FILE"
    echo "  draining node: $node"

    kubectl cordon "$node"
    # --delete-emptydir-data because several platform components use emptyDir for
    # caches; without it the drain refuses to evict them and stalls.
    # --ignore-daemonsets because DaemonSet pods are recreated on the node by design
    # and are never evictable.
    kubectl drain "$node" \
      --ignore-daemonsets \
      --delete-emptydir-data \
      --timeout=180s
    echo "  drained: $node"
    ;;

  remove)
    if [[ -f "$STATE_FILE" ]]; then
      node="$(cat "$STATE_FILE")"
      echo "  uncordoning: $node"
      kubectl uncordon "$node" || true
      rm -f "$STATE_FILE"
    else
      # Belt and braces: if the state file is gone, uncordon everything rather than
      # leaving the cluster permanently short of capacity.
      kubectl get nodes -o jsonpath='{range .items[?(@.spec.unschedulable==true)]}{.metadata.name}{"\n"}{end}' \
        | while read -r node; do
            [[ -n "$node" ]] && kubectl uncordon "$node" || true
          done
    fi
    ;;

  *)
    echo "usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
