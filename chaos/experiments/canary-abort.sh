#!/usr/bin/env bash
# Ship a deliberately broken version and prove the canary analysis rolls it back.
#
# The broken version is not a different image. It is the same image with
# FAULT_ERROR_RATE=0.5, which changes the pod template, which starts a canary. One
# mechanism serves both the chaos suite and this demo, so the signals are comparable.
#
# Two ways to deliver that change:
#
#   CANARY_VIA_GIT=1  commit it to gitops/ and push, letting Argo CD deliver it. This is
#                     the real path and the one used for the committed evidence. It needs
#                     push access to the repository.
#
#   default           patch the Rollout directly, with the Application's automated sync
#                     suspended for the duration. A reviewer cloning this repo has no push
#                     access, and without suspending sync, selfHeal reverts the patch in
#                     seconds and the canary never starts. The rollback being demonstrated
#                     is the Rollouts analysis either way; only the delivery differs.
#
# Called by the harness as: canary-abort.sh apply | remove

set -euo pipefail

NAMESPACE="${NAMESPACE:-demo}"
ROLLOUT="storefront"
APP="storefront"
BROKEN_RATE="${BROKEN_RATE:-0.5}"
STATE_FILE="${TMPDIR:-/tmp}/k8s-slo-platform-canary-state"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The base, not an overlay: FAULT_ERROR_RATE is set once in the base configMapGenerator
# and inherited by kind and eks alike. Pointing this at the kind overlay made the sed a
# no-op, so `git add` staged nothing and `git commit` exited non-zero under `set -e`.
CONFIG_PATCH="${REPO_ROOT}/gitops/services/storefront/base/kustomization.yaml"

suspend_auto_sync() {
  kubectl -n argocd get application "$APP" \
    -o jsonpath='{.spec.syncPolicy.automated}' > "${STATE_FILE}.sync" 2>/dev/null || true
  kubectl -n argocd patch application "$APP" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":null}}}'
  echo "  suspended automated sync on the storefront Application"
}

restore_auto_sync() {
  kubectl -n argocd patch application "$APP" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' || true
  rm -f "${STATE_FILE}.sync"
  echo "  restored automated sync"
}

case "${1:-}" in
  apply)
    if [[ "${CANARY_VIA_GIT:-0}" == "1" ]]; then
      echo "  delivering the broken version through Git"
      sed -i.bak "s/FAULT_ERROR_RATE=0.0/FAULT_ERROR_RATE=${BROKEN_RATE}/" "$CONFIG_PATCH"
      git -C "$REPO_ROOT" add "$CONFIG_PATCH"
      git -C "$REPO_ROOT" commit -m "Ship a deliberately broken storefront canary

Sets FAULT_ERROR_RATE=${BROKEN_RATE}. The Rollouts analysis should catch this
and roll back without a human. Reverted in the next commit."
      git -C "$REPO_ROOT" push
      echo "git" > "$STATE_FILE"
      # Argo CD polls every ~3 minutes by default; ask for the sync now rather than
      # spending that on a demo.
      kubectl -n argocd patch application "$APP" --type merge \
        -p '{"operation":{"initiatedBy":{"username":"canary-demo"},"sync":{"revision":"HEAD"}}}' || true
    else
      echo "  delivering the broken version by patching the Rollout directly"
      echo "  (set CANARY_VIA_GIT=1 to deliver it through Git instead)"
      suspend_auto_sync
      echo "patch" > "$STATE_FILE"
      kubectl -n "$NAMESPACE" patch rollout "$ROLLOUT" --type merge -p "$(cat <<JSON
{"spec":{"template":{"spec":{"containers":[{"name":"storefront","env":[
  {"name":"FAULT_ERROR_RATE","value":"${BROKEN_RATE}"}
]}]}}}}
JSON
)"
    fi

    echo "  canary starting; watch it with:"
    echo "    kubectl argo rollouts get rollout ${ROLLOUT} -n ${NAMESPACE} --watch"
    ;;

  remove)
    mode="$(cat "$STATE_FILE" 2>/dev/null || echo patch)"

    # Abort explicitly rather than waiting. If the analysis already aborted this is a
    # no-op; if it has not, the demo is over and the cluster should not keep serving a
    # knowingly broken canary while the next scenario runs.
    kubectl argo rollouts abort "$ROLLOUT" -n "$NAMESPACE" 2>/dev/null || true

    if [[ "$mode" == "git" ]]; then
      echo "  reverting the broken version in Git"
      sed -i "s/FAULT_ERROR_RATE=${BROKEN_RATE}/FAULT_ERROR_RATE=0.0/" "$CONFIG_PATCH"
      rm -f "${CONFIG_PATCH}.bak"
      git -C "$REPO_ROOT" add "$CONFIG_PATCH"
      git -C "$REPO_ROOT" commit -m "Revert the deliberately broken canary

The rollback is recorded in evidence/; this returns main to a deployable state."
      git -C "$REPO_ROOT" push
    else
      kubectl -n "$NAMESPACE" patch rollout "$ROLLOUT" --type merge -p "$(cat <<'JSON'
{"spec":{"template":{"spec":{"containers":[{"name":"storefront","env":[
  {"name":"FAULT_ERROR_RATE","value":"0.0"}
]}]}}}}
JSON
)" || true
      restore_auto_sync
      # An aborted Rollout stays Degraded until a new template change arrives, so Argo
      # CD would show the Application unhealthy indefinitely. Promoting the restored
      # (known-good) template clears it.
      kubectl argo rollouts promote "$ROLLOUT" -n "$NAMESPACE" --full 2>/dev/null || true
    fi

    rm -f "$STATE_FILE"
    ;;

  *)
    echo "usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
