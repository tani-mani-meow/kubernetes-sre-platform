#!/usr/bin/env bash
# Build all three service images from the single parameterised Dockerfile.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require docker

TAG="${1:-local}"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)"

step "Building images (tag: ${TAG}, revision: ${GIT_SHA})"
for svc in "${SERVICES[@]}"; do
  info "building ${svc}"
  docker build \
    --build-arg "SERVICE=${svc}" \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    --tag "${REGISTRY}/${svc}:${TAG}" \
    --file "${REPO_ROOT}/app/Dockerfile" \
    "${REPO_ROOT}/app"
done

ok "built: ${SERVICES[*]}"
