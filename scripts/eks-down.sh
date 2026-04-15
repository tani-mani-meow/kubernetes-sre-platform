#!/usr/bin/env bash
# Ordered teardown of a capture cluster.
#
# The order is the whole point. A Service of type LoadBalancer makes the AWS cloud
# controller create an NLB that Terraform does not know about. Running `terraform
# destroy` first tears down the cluster (and the controller) while the NLB survives -
# orphaned, unmanaged, and still billing. Deleting the Services first lets the
# controller clean up its own load balancers while it is still alive to do it.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require kubectl terraform

ctx="$(kube_context)"
step "Teardown"
info "kubectl context: ${ctx}"

if [[ "$ctx" == kind-* ]]; then
  die "context is a kind cluster. Use 'make kind-down' instead."
fi

cat <<EOF

  ${YELLOW}${BOLD}This destroys the EKS capture cluster and everything in it.${RESET}

  Before continuing, confirm the evidence is already on disk:
    - screenshots copied into evidence/<date>-<scenario>/img/
    - chaos harness JSON copied out of chaos/harness/out/
  Grafana has no persistence in this deployment. Anything not exported dies here.

EOF
read -r -p "  Type the cluster name to confirm: " confirm
expected=$(cd "${REPO_ROOT}/infra/terraform" && terraform output -raw cluster_name 2>/dev/null || echo "")
if [[ -z "$expected" || "$confirm" != "$expected" ]]; then
  die "confirmation did not match '${expected:-<unknown>}' - nothing was deleted"
fi

step "Deleting LoadBalancer Services so their NLBs are cleaned up"
mapfile -t lb_services < <(kubectl get svc --all-namespaces \
  -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' \
  2>/dev/null || true)

if (( ${#lb_services[@]} == 0 )); then
  info "none found"
else
  for entry in "${lb_services[@]}"; do
    [[ -n "$entry" ]] || continue
    read -r ns name <<<"$entry"
    info "deleting svc/${name} in ${ns}"
    kubectl -n "$ns" delete svc "$name" --wait=true --timeout=180s || \
      warn "delete of ${ns}/${name} did not complete - check the console for a leaked NLB"
  done
  info "giving the cloud controller time to finish"
  sleep 30
fi

step "terraform destroy"
cd "${REPO_ROOT}/infra/terraform" || die "infra/terraform not found"
terraform destroy

step "Manual sweep"
cat <<EOF

  Terraform is done. Check the console for anything it did not own:

    - EC2 > Load Balancers      (should be empty)
    - EC2 > Volumes             (unattached gp3 volumes from PVCs)
    - EC2 > Elastic IPs         (released?)
    - CloudWatch > Log groups   (/aws/eks/... retains and bills)

  Filter on the project tag to find strays quickly.

EOF
