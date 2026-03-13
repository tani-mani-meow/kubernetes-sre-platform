#!/usr/bin/env bash
# Fast inner loop: build, push into the kind nodes, restart.
#
# This deliberately bypasses Git and Argo CD. Committing and waiting for a sync is the
# right path for anything that should end up in the evidence trail; it is the wrong path
# for the twentieth iteration on a log line. Argo CD will not fight this - the image tag
# in Git is unchanged, so the manifest still matches.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require docker kind kubectl
assert_kind_context

bash "${REPO_ROOT}/scripts/build-images.sh" local

step "Loading images into kind nodes"
for svc in "${SERVICES[@]}"; do
  info "loading ${svc}"
  kind load docker-image "${REGISTRY}/${svc}:local" --name "$CLUSTER"
done

step "Restarting workloads"
# The tag never changes, so containerd keeps serving the layer it already has and a
# plain re-apply is a no-op. The restart is what actually picks up the new image.
for svc in "${SERVICES[@]}"; do
  if kubectl -n "$NAMESPACE" get rollout "$svc" >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" patch rollout "$svc" --type merge \
      -p "{\"spec\":{\"restartAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
  else
    kubectl -n "$NAMESPACE" rollout restart "deployment/${svc}"
  fi
done

for svc in "${SERVICES[@]}"; do
  kubectl -n "$NAMESPACE" rollout status "deployment/${svc}" --timeout=120s 2>/dev/null \
    || kubectl argo rollouts status "$svc" -n "$NAMESPACE" --timeout 120s
done

ok "deployed"
