#!/usr/bin/env bash
# Print the UIs and, unless --no-forward, hold open a port-forward for each.
#
# Port-forwards rather than Ingress routes for the platform UIs: exposing Grafana and
# the Argo CD API through the same Ingress the demo traffic uses would put them on the
# storefront's latency SLI, and a reviewer clicking around a dashboard would show up as
# error-budget burn.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require kubectl

NO_FORWARD=0
[[ "${1:-}" == "--no-forward" ]] && NO_FORWARD=1

argocd_password=$(kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || echo "<already rotated>")
grafana_password=$(kubectl -n monitoring get secret kube-prometheus-stack-grafana \
  -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || echo "admin")

cat <<EOF

  ${BOLD}Application${RESET}
    storefront          http://localhost/products
    one product         http://localhost/products/1

  ${BOLD}Platform UIs${RESET}    ${DIM}(port-forwarded below)${RESET}
    Grafana             http://localhost:3000        admin / ${grafana_password}
    Argo CD             http://localhost:8080        admin / ${argocd_password}
    Prometheus          http://localhost:9090
    Alertmanager        http://localhost:9093
    Argo Rollouts       http://localhost:3100

  ${BOLD}Start here${RESET}
    Grafana > Dashboards > "storefront" for the RED + error-budget view.
    Argo CD shows the whole cluster as it is defined in Git.

EOF

if (( NO_FORWARD )); then
  info "run 'bash scripts/urls.sh' to start the port-forwards"
  exit 0
fi

declare -a PIDS=()
forward() {
  local ns=$1 target=$2 ports=$3 label=$4
  if kubectl -n "$ns" get "${target%%/*}" "${target#*/}" >/dev/null 2>&1; then
    kubectl -n "$ns" port-forward "$target" "$ports" >/dev/null 2>&1 &
    PIDS+=($!)
    ok "$(printf '%-16s %s' "$label" "localhost:${ports%%:*}")"
  else
    warn "$(printf '%-16s not installed yet' "$label")"
  fi
}

step "Port-forwards"
forward monitoring svc/kube-prometheus-stack-grafana      3000:80   "Grafana"
forward argocd     svc/argocd-server                      8080:80   "Argo CD"
forward monitoring svc/kube-prometheus-stack-prometheus   9090:9090 "Prometheus"
forward monitoring svc/kube-prometheus-stack-alertmanager 9093:9093 "Alertmanager"
forward argo-rollouts svc/argo-rollouts-dashboard         3100:3100 "Rollouts"

if (( ${#PIDS[@]} == 0 )); then
  die "nothing to forward - is the cluster up?"
fi

trap 'echo; info "closing port-forwards"; kill "${PIDS[@]}" 2>/dev/null || true' EXIT INT TERM
info ""
info "Ctrl-C to stop."
wait
