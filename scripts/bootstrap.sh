#!/usr/bin/env bash
# Install Argo CD and hand it the root application.
#
# Argo CD is the one component installed imperatively. Something has to apply the first
# manifest, and a GitOps controller cannot bootstrap itself from the repo it has not
# been told about yet. Everything after this point is declarative.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require helm kubectl

ENV="${1:-kind}"
case "$ENV" in
  kind|eks) ;;
  *) die "usage: bootstrap.sh [kind|eks]" ;;
esac

[[ "$ENV" == "kind" ]] && assert_kind_context

ARGOCD_CHART_VERSION="${ARGOCD_CHART_VERSION:-7.7.11}"

step "Installing Argo CD (chart ${ARGOCD_CHART_VERSION})"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version "$ARGOCD_CHART_VERSION" \
  --values "${REPO_ROOT}/gitops/bootstrap/argocd-values.yaml" \
  --wait --timeout 8m

step "Waiting for the Argo CD API"
wait_with_progress 300 5 "argocd-server" \
  kubectl -n argocd get deploy argocd-server \
    -o jsonpath='{.status.readyReplicas}' --ignore-not-found

step "Applying the root application"
kubectl apply -f "${REPO_ROOT}/gitops/bootstrap/root-${ENV}.yaml"

info "Argo CD will now reconcile the cluster from Git."
info "Watch it with: kubectl -n argocd get applications.argoproj.io -w"

step "Admin password"
password=$(kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if [[ -n "$password" ]]; then
  ok "user: admin   password: ${password}"
else
  warn "initial admin secret not found (it is deleted after first login)"
fi

ok "bootstrap complete"
