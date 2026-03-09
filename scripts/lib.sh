#!/usr/bin/env bash
# Shared helpers. Sourced, never executed.
#
# The variables defined here are consumed by the scripts that source this file, which
# the linter cannot see from where it sits - hence the blanket unused-variable waiver.
# shellcheck disable=SC2034

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${CLUSTER:-k8s-slo-platform}"
NAMESPACE="${NAMESPACE:-demo}"
REGISTRY="${REGISTRY:-ghcr.io/tani-mani-meow/k8s-slo-platform}"
SERVICES=(storefront catalog recs)

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  BLUE=$'\033[34m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; DIM=""; RESET=""
fi

step()  { printf '\n%s==> %s%s\n' "$BOLD$BLUE" "$*" "$RESET"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s%s%s\n' "$GREEN" "$*" "$RESET"; }
warn()  { printf '    %s%s%s\n' "$YELLOW" "$*" "$RESET" >&2; }
die()   { printf '\n%serror:%s %s\n' "$RED$BOLD" "$RESET" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

require() {
  local missing=()
  for tool in "$@"; do
    have "$tool" || missing+=("$tool")
  done
  if (( ${#missing[@]} )); then
    die "missing required tools: ${missing[*]}
    Run 'bash scripts/doctor.sh' for install instructions."
  fi
}

# Poll until a command succeeds. Used instead of `kubectl wait` wherever the resource
# may not exist yet - `kubectl wait` fails immediately on a missing resource, which
# turns a slow CRD registration into a spurious failure.
retry_until() {
  local timeout=$1 interval=$2 description=$3; shift 3
  local deadline=$(( SECONDS + timeout ))
  while (( SECONDS < deadline )); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$interval"
  done
  die "timed out after ${timeout}s waiting for: ${description}"
}

# Progress-reporting variant for the long waits in demo.sh, so a five-minute bootstrap
# does not look like a hang.
wait_with_progress() {
  local timeout=$1 interval=$2 description=$3; shift 3
  local deadline=$(( SECONDS + timeout ))
  local started=$SECONDS
  printf '    waiting for %s ' "$description"
  while (( SECONDS < deadline )); do
    if "$@" >/dev/null 2>&1; then
      printf ' %sok%s (%ds)\n' "$GREEN" "$RESET" "$(( SECONDS - started ))"
      return 0
    fi
    printf '.'
    sleep "$interval"
  done
  printf ' %sfailed%s\n' "$RED" "$RESET"
  die "timed out after ${timeout}s waiting for: ${description}"
}

kube_context() {
  kubectl config current-context 2>/dev/null || echo "<none>"
}

# Refuse to run cluster-mutating scripts against anything that is not the local kind
# cluster. The chaos suite deletes pods and drains nodes; pointing it at the wrong
# context is the one mistake in this repo that could damage something real.
assert_kind_context() {
  local ctx; ctx="$(kube_context)"
  if [[ "$ctx" != "kind-${CLUSTER}" ]]; then
    die "current kubectl context is '${ctx}', expected 'kind-${CLUSTER}'.
    Refusing to continue: this script mutates cluster state.
    Switch with: kubectl config use-context kind-${CLUSTER}"
  fi
}
