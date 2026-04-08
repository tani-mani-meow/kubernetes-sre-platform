#!/usr/bin/env bash
# Simulate losing an availability zone: cordon and drain every node in one zone at once.
#
# This is a simulation, not a real AZ failure. A genuine zone outage also takes out the
# EBS volumes in that zone, and the single-replica Postgres StatefulSet would not come
# back until the zone did. What this does test is capacity: half the nodes vanish at
# once, and whatever had all its replicas in the failed zone goes with them.
#
# Expected to be the ugliest scenario in the suite. That is the point - it is the one
# that shows what single-replica Postgres actually costs.
#
# Called by the harness as: az-loss.sh apply | remove

set -euo pipefail

ZONE_LABEL="topology.kubernetes.io/zone"
STATE_FILE="${TMPDIR:-/tmp}/k8s-slo-platform-lost-zone"

zones() {
  kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
    -o jsonpath="{range .items[*]}{.metadata.labels['${ZONE_LABEL//./\\.}']}{\"\n\"}{end}" \
    | sort -u | grep -v '^$'
}

nodes_in_zone() {
  kubectl get nodes -l "!node-role.kubernetes.io/control-plane,${ZONE_LABEL}=$1" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
}

case "${1:-}" in
  apply)
    mapfile -t all_zones < <(zones)
    if (( ${#all_zones[@]} < 2 )); then
      echo "need at least 2 zones to lose one, found: ${all_zones[*]:-none}" >&2
      echo "on kind, check that infra/kind/kind-config.yaml labels the workers" >&2
      exit 1
    fi

    # Never pick the zone hosting Postgres. Not to protect it - the scenario is
    # supposed to hurt - but because losing the database makes every other signal in
    # the run unreadable, and the postgres-kill scenario already measures that in
    # isolation. Losing a zone that does not hold the database is the more informative
    # experiment: it shows what survives on redundancy alone.
    postgres_zone=$(kubectl -n demo get pod -l app.kubernetes.io/name=postgres \
      -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null \
      | xargs -r -I{} kubectl get node {} \
          -o jsonpath="{.metadata.labels['${ZONE_LABEL//./\\.}']}" 2>/dev/null || true)

    target=""
    for zone in "${all_zones[@]}"; do
      if [[ "$zone" != "$postgres_zone" ]]; then
        target="$zone"
        break
      fi
    done
    target="${target:-${all_zones[0]}}"

    echo "$target" > "$STATE_FILE"
    echo "  losing zone: $target (postgres is in: ${postgres_zone:-unknown})"

    mapfile -t targets < <(nodes_in_zone "$target")
    for node in "${targets[@]}"; do
      [[ -n "$node" ]] || continue
      echo "  cordoning $node"
      kubectl cordon "$node"
    done

    # Cordon everything first, then drain. Draining one node at a time while the others
    # are still schedulable would let pods reschedule onto nodes that are about to go
    # too, which is a slow rolling restart rather than a zone loss.
    for node in "${targets[@]}"; do
      [[ -n "$node" ]] || continue
      echo "  draining $node"
      kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data \
        --timeout=120s --force || echo "  (drain of $node did not complete cleanly)"
    done
    echo "  zone $target is down"
    ;;

  remove)
    if [[ -f "$STATE_FILE" ]]; then
      target="$(cat "$STATE_FILE")"
      echo "  restoring zone: $target"
      nodes_in_zone "$target" | while read -r node; do
        [[ -n "$node" ]] && kubectl uncordon "$node" || true
      done
      rm -f "$STATE_FILE"
    fi
    # Always sweep: a half-finished run must not leave the cluster short of capacity.
    kubectl get nodes -o jsonpath='{range .items[?(@.spec.unschedulable==true)]}{.metadata.name}{"\n"}{end}' \
      | while read -r node; do
          [[ -n "$node" ]] && kubectl uncordon "$node" || true
        done
    ;;

  *)
    echo "usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
