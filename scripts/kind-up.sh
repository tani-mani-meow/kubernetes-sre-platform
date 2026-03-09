#!/usr/bin/env bash
# Create the local cluster.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require kind kubectl docker

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  warn "cluster '${CLUSTER}' already exists, reusing it"
else
  step "Creating kind cluster '${CLUSTER}'"
  kind create cluster --name "$CLUSTER" \
    --config "${REPO_ROOT}/infra/kind/kind-config.yaml" \
    --wait 120s
fi

kubectl config use-context "kind-${CLUSTER}" >/dev/null

step "Cluster"
kubectl get nodes -o wide

# The observability stack opens a lot of files. On multi-node kind the default inotify
# limits are per-host, shared across every node container, and Grafana/Loki hit them
# first - the symptom is a crash-loop with "too many open files" that looks like a
# broken chart rather than a host limit.
step "Checking host inotify limits"
current_watches=$(docker run --rm --privileged --pid=host alpine \
  sh -c 'cat /proc/sys/fs/inotify/max_user_watches' 2>/dev/null || echo unknown)
current_instances=$(docker run --rm --privileged --pid=host alpine \
  sh -c 'cat /proc/sys/fs/inotify/max_user_instances' 2>/dev/null || echo unknown)
info "max_user_watches=${current_watches} max_user_instances=${current_instances}"
if [[ "$current_watches" != unknown ]] && (( current_watches < 262144 )); then
  warn "max_user_watches is low; see docs/prerequisites.md for how to raise it"
fi

ok "cluster ready (context: kind-${CLUSTER})"
