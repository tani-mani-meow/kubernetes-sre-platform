#!/usr/bin/env bash
# The reviewer entry point: an empty machine to a running, observable, breakable
# platform in one command.
#
# Ordering matters. Images are built and loaded into the kind nodes *before* Argo CD
# starts reconciling, because the kind overlay pins tag `local` with
# imagePullPolicy: IfNotPresent - if the services synced first they would sit in
# ImagePullBackOff waiting for an image that only ever exists locally.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

START=$SECONDS

step "Preflight"
bash "${REPO_ROOT}/scripts/doctor.sh"

bash "${REPO_ROOT}/scripts/kind-up.sh"
bash "${REPO_ROOT}/scripts/build-images.sh" local

step "Loading images into the cluster"
for svc in "${SERVICES[@]}"; do
  info "loading ${svc}"
  kind load docker-image "${REGISTRY}/${svc}:local" --name "$CLUSTER"
done

bash "${REPO_ROOT}/scripts/bootstrap.sh" kind

step "Waiting for Argo CD to reconcile the platform"
info "this pulls several Helm charts; five to ten minutes on a first run is normal"

# Applications appear as the root app-of-apps expands, so wait for the set to exist
# before judging its health - an empty list is trivially "all healthy".
wait_with_progress 300 5 "applications to be created" \
  bash -c 'test "$(kubectl -n argocd get applications.argoproj.io \
      --no-headers 2>/dev/null | wc -l)" -ge 5'

wait_with_progress 900 10 "all applications to be Healthy" \
  bash -c '! kubectl -n argocd get applications.argoproj.io \
      -o jsonpath="{range .items[*]}{.status.health.status}{\"\n\"}{end}" 2>/dev/null \
      | grep -qv "^Healthy$"'

step "Waiting for the demo workloads"
wait_with_progress 300 5 "storefront to serve traffic" \
  bash -c "kubectl -n ${NAMESPACE} get endpoints storefront \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -q ."

step "Smoke test"
if curl -fsS --max-time 10 http://localhost/products >/dev/null 2>&1; then
  ok "http://localhost/products responds"
else
  warn "storefront did not answer through the Ingress yet - it may need another minute"
  warn "check with: kubectl -n ${NAMESPACE} get pods"
fi

step "Ready in $(( SECONDS - START ))s"
bash "${REPO_ROOT}/scripts/urls.sh" --no-forward

cat <<EOF

  ${BOLD}What to try next${RESET}

    ${DIM}# start the UI port-forwards${RESET}
    bash scripts/urls.sh

    ${DIM}# watch Argo CD revert a hand-edited deployment${RESET}
    kubectl -n ${NAMESPACE} scale deployment/recs --replicas=5

    ${DIM}# break something on purpose and measure the recovery${RESET}
    python chaos/harness/run.py list
    python chaos/harness/run.py run pod-kill-catalog

  A guided ten-minute tour is in ${BOLD}docs/demo-script.md${RESET}.

EOF
