#!/usr/bin/env bash
# Render everything that can be rendered without a cluster.
#
# This is the cheap half of verification: it catches a mistyped Kustomize path, an
# invalid Helm value, a Sloth spec that no longer compiles, and generated rules that
# have drifted from their spec. It cannot catch anything that depends on the API server
# (admission, CRD schemas, RBAC) - the CI e2e job on kind covers that.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require kustomize

FAILED=0
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

check_overlay() {
  local path=$1
  if kustomize build "$path" --enable-helm > "${RENDER_DIR}/$(echo "$path" | tr '/' '_').yaml" 2>"${RENDER_DIR}/err"; then
    local kinds
    kinds=$(grep -c '^kind:' "${RENDER_DIR}/$(echo "$path" | tr '/' '_').yaml" || true)
    ok "$(printf '%-52s %s objects' "$path" "$kinds")"
  else
    warn "$(printf '%-52s FAILED' "$path")"
    sed 's/^/        /' "${RENDER_DIR}/err" >&2
    FAILED=1
  fi
}

step "Line endings"
# Windows checkouts with core.autocrlf=true, plus any editor that does not honour
# .gitattributes, can leave CRLF in the working tree. The failure is silent and specific:
# sloth rejects a CRLF spec with "invalid spec, could not load with any of the supported
# spec types" and says nothing about line endings, and a CRLF shell script fails inside a
# Linux container with "bad interpreter: /usr/bin/env bash^M".
# `|| true` because grep exits 1 when it finds nothing, and finding nothing is the
# success case here - under `set -o pipefail` that exit code would end the script.
crlf=$( cd "$REPO_ROOT" && git ls-files -z \
        | xargs -0 grep -lU "$(printf '\r')" 2>/dev/null | head -20 || true )
if [[ -n "$crlf" ]]; then
  warn "these tracked files have CRLF line endings:"
  printf '        %s\n' $crlf >&2
  warn "fix with: git add --renormalize . && git checkout -- ."
  FAILED=1
else
  ok "all tracked files are LF"
fi

step "Kustomize overlays"
while IFS= read -r kustomization; do
  check_overlay "$(dirname "$kustomization")"
done < <(find "${REPO_ROOT}/gitops" -name kustomization.yaml \
           -not -path '*/base/*' | sort)

step "Kustomize bases"
while IFS= read -r kustomization; do
  check_overlay "$(dirname "$kustomization")"
done < <(find "${REPO_ROOT}/gitops" -name kustomization.yaml \
           -path '*/base/*' | sort)

step "Argo CD Application manifests"
for app in "${REPO_ROOT}"/gitops/clusters/*/apps/*.yaml "${REPO_ROOT}"/gitops/bootstrap/*.yaml; do
  [[ -e "$app" ]] || continue
  if kubectl apply --dry-run=client --validate=false -f "$app" >/dev/null 2>&1 \
     || python -c "import sys,yaml; list(yaml.safe_load_all(open(sys.argv[1])))" "$app" 2>/dev/null; then
    ok "$(printf '%-52s parses' "${app#"$REPO_ROOT"/}")"
  else
    warn "$(printf '%-52s FAILED to parse' "${app#"$REPO_ROOT"/}")"
    FAILED=1
  fi
done

step "Rendered workloads are complete"
# Guards a failure mode that renders cleanly and deploys nothing usable. kustomize has
# no schema for a CRD, so a strategic-merge patch against the storefront Rollout
# replaces the `containers` list instead of merging into it - producing a container with
# a name and resources and no image, ports, env or probes. `kustomize build` is perfectly
# happy with that; the cluster is not. The overlays use JSON6902 patches for exactly this
# reason, and this check is what stops someone switching them back.
for service in storefront catalog recs; do
  for env in kind eks ci; do
    overlay="gitops/services/${service}/overlays/${env}"
    rendered=$(kustomize build "$overlay" 2>/dev/null)
    problems=""
    grep -q "image: .*/${service}:" <<<"$rendered"   || problems+=" image"
    grep -q 'containerPort: 8000' <<<"$rendered"     || problems+=" port"
    grep -q 'configMapRef'        <<<"$rendered"     || problems+=" env"
    grep -q 'path: /readyz'       <<<"$rendered"     || problems+=" readinessProbe"
    grep -q 'mountPath: /tmp'     <<<"$rendered"     || problems+=" writable-tmp"
    if [[ -n "$problems" ]]; then
      warn "$(printf '%-52s missing:%s' "$overlay" "$problems")"
      FAILED=1
    else
      ok "$(printf '%-52s complete' "$overlay")"
    fi
  done
done

step "Dashboards reference recording rules that exist"
# Renaming an SLO is a one-line edit in gitops/slo/specs/. Every panel keyed on the old
# sloth_id then reads "No data" - which looks exactly like a healthy service with no
# traffic, and is the failure a dashboard is least likely to be blamed for.
generated_rules=$(grep -ho 'record: slo:[a-z_:0-9]*' "${REPO_ROOT}"/gitops/slo/generated/*-rules.yaml \
                  | sed 's/record: //' | sort -u)
generated_ids=$(grep -ho 'sloth_id: [a-z-]*' "${REPO_ROOT}"/gitops/slo/generated/*-rules.yaml \
                | sed 's/sloth_id: //' | sort -u)

for dashboard in "${REPO_ROOT}"/gitops/platform/dashboards/*.json; do
  [[ -e "$dashboard" ]] || continue
  missing=""
  for rule in $(grep -o 'slo:[a-z_]*:ratio[a-z0-9]*' "$dashboard" | sed 's/_rate[0-9]*[a-z]*$//' | sort -u); do
    grep -qx "$rule" <<<"$generated_rules" || \
      grep -q "^${rule}_rate" <<<"$generated_rules" || \
      grep -q "^${rule}" <<<"$generated_rules" || missing+=" $rule"
  done
  # sloth_id appears two ways. The per-service dashboards template it as
  # `$service-availability`, so the Grafana variable has to be resolved before the id
  # can be checked; slo-overview.json writes them out literally.
  service_of_dashboard="$(basename "$dashboard" .json)"
  for suffix in $(grep -o 'sloth_id=\\"\$service-[a-z]*\\"' "$dashboard" \
                  | sed 's/.*\$service-//; s/\\"//' | sort -u); do
    grep -qx "${service_of_dashboard}-${suffix}" <<<"$generated_ids" \
      || missing+=" \$service-${suffix}(=${service_of_dashboard}-${suffix})"
  done
  for sloth_id in $(grep -o 'sloth_id=\\"[a-z][a-z-]*\\"' "$dashboard" \
                    | sed 's/sloth_id=\\"//; s/\\"//' | sort -u); do
    grep -qx "$sloth_id" <<<"$generated_ids" || missing+=" sloth_id:$sloth_id"
  done
  if [[ -n "$missing" ]]; then
    warn "$(printf '%-52s unknown:%s' "$(basename "$dashboard")" "$missing")"
    FAILED=1
  else
    ok "$(printf '%-52s references resolve' "$(basename "$dashboard")")"
  fi
done

step "Seed SQL matches the catalog migrations"
bash "${REPO_ROOT}/scripts/sync-sql.sh" --check || FAILED=1

step "SLO rules match their specs"
if have sloth; then
  tmp="$(mktemp -d)"
  for spec in "${REPO_ROOT}"/gitops/slo/specs/*.yaml; do
    name="$(basename "$spec" .yaml)"
    sloth generate --no-log -i "$spec" -o "${tmp}/${name}-rules.yaml" 2>/dev/null
    if diff -q "${tmp}/${name}-rules.yaml" \
              "${REPO_ROOT}/gitops/slo/generated/${name}-rules.yaml" >/dev/null 2>&1; then
      ok "$(printf '%-52s up to date' "$name")"
    else
      warn "$(printf '%-52s DRIFTED - run make slo-gen' "$name")"
      FAILED=1
    fi
  done
  rm -rf "$tmp"
else
  warn "sloth not installed, skipping SLO drift check"
fi

step "Result"
if (( FAILED )); then
  die "validation failed (see above)"
fi
ok "everything renders"
