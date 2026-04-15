#!/usr/bin/env bash
# Preflight check. Run this before `make demo` - it is faster to be told a tool is
# missing than to watch a bootstrap fail eight minutes in.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MISSING=0

check() {
  local tool=$1 install=$2
  if have "$tool"; then
    ok "$(printf '%-12s %s' "$tool" "$( "$tool" version --short 2>/dev/null \
        || "$tool" --version 2>/dev/null | head -1 || echo present )")"
  else
    warn "$(printf '%-12s MISSING - %s' "$tool" "$install")"
    MISSING=1
  fi
}

step "Required tools"
check docker    "https://docs.docker.com/desktop/  (needs an elevated install on Windows)"
check kind      "go install sigs.k8s.io/kind@latest  |  brew install kind"
check kubectl   "https://kubernetes.io/docs/tasks/tools/"
check helm      "https://helm.sh/docs/intro/install/"
check kustomize "https://kubectl.docs.kubernetes.io/installation/kustomize/"
check python    "https://www.python.org/downloads/  (3.11+)"

step "Optional tools"
have sloth   || warn "sloth       missing - only needed for 'make slo-gen' (https://sloth.dev)"
have make    || warn "make        missing - run scripts/*.sh directly instead"
have jq      || warn "jq          missing - some scripts print raw JSON without it"
have terraform || warn "terraform   missing - only needed for the EKS capture phase"

step "Docker"
if have docker; then
  if docker info >/dev/null 2>&1; then
    ok "daemon reachable"
    total_kb=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
    total_gb=$(( total_kb / 1024 / 1024 / 1024 ))
    if (( total_gb > 0 && total_gb < 8 )); then
      warn "Docker has ${total_gb}GiB of memory. The full stack needs ~8GiB;"
      warn "raise it in Docker Desktop > Settings > Resources, or expect OOMKilled pods."
    else
      ok "memory available to Docker: ${total_gb}GiB"
    fi
  else
    warn "docker is installed but the daemon is not reachable - is Docker Desktop running?"
    MISSING=1
  fi
fi

step "Cluster"
info "current kubectl context: $(kube_context)"

if (( MISSING )); then
  step "Result"
  die "some required tools are missing (see above)"
fi

step "Result"
ok "ready - run 'make demo' (or 'bash scripts/demo.sh')"
