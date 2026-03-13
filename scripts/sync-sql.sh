#!/usr/bin/env bash
# Keep the seed Job's SQL in step with the application's migrations.
#
# gitops/services/postgres/base/sql/ is a copy of app/services/catalog/migrations/.
# The copy exists because configMapGenerator refuses paths outside its kustomization
# root, and both workarounds are worse: --load-restrictor LoadRestrictionsNone has to be
# enabled for every Argo CD Application at once, and a symlink hits the same restrictor
# after resolution while also not surviving a Windows checkout.
#
# app/services/catalog/migrations/ is the source of truth. Run this after changing it.
# `--check` verifies without writing, which is what CI runs: drift here is silent
# otherwise, surfacing as runtime 500s when catalog queries a column the cluster's
# database has never heard of.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SOURCE="${REPO_ROOT}/app/services/catalog/migrations"
COPY="${REPO_ROOT}/gitops/services/postgres/base/sql"

if [[ "${1:-}" == "--check" ]]; then
  if diff -r "$SOURCE" "$COPY" >/dev/null 2>&1; then
    ok "seed SQL matches app/services/catalog/migrations/"
    exit 0
  fi
  step "SQL drift detected"
  diff -r "$SOURCE" "$COPY" || true
  die "gitops/services/postgres/base/sql/ is out of date. Run: make sync-sql"
fi

step "Syncing seed SQL"
mkdir -p "$COPY"
# Delete first: a migration removed from the source must disappear from the copy too,
# or the seed Job keeps applying a file nobody maintains any more.
rm -f "$COPY"/*.sql
cp "$SOURCE"/*.sql "$COPY"/

for file in "$COPY"/*.sql; do
  info "$(basename "$file")"
done
ok "copied $(ls -1 "$COPY"/*.sql | wc -l) file(s)"
