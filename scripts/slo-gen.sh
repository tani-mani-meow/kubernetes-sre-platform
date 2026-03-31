#!/usr/bin/env bash
# Compile SLO specs into PrometheusRules.
#
# Sloth runs here, at build time, and its output is committed. The alternative - running
# a Sloth controller in-cluster - would mean the burn-rate rules only exist somewhere a
# reviewer cannot read them, and would add an operator whose failure mode is "alerting
# silently stopped working".
#
# Per-file rather than Sloth's directory mode: directory mode joins the input path onto
# the output path, which produces a broken nested path on Windows.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require sloth

SPECS="${REPO_ROOT}/gitops/slo/specs"
OUT="${REPO_ROOT}/gitops/slo/generated"

step "Compiling SLO specs"
for spec in "$SPECS"/*.yaml; do
  name="$(basename "$spec" .yaml)"
  info "$name"
  sloth generate --no-log -i "$spec" -o "${OUT}/${name}-rules.yaml"
done

ok "wrote $(ls -1 "$OUT"/*-rules.yaml | wc -l) rule files to gitops/slo/generated/"

if ! git -C "$REPO_ROOT" diff --quiet -- "gitops/slo/generated"; then
  warn "generated rules changed - commit them, or CI will flag the drift"
fi
