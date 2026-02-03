# k8s-slo-platform
#
# Targets are thin wrappers around scripts/. The logic lives in shell so that a
# reviewer without GNU make (notably on Windows, where Git for Windows ships no make)
# can run `bash scripts/demo.sh` and get the same result.
#
# All paths are relative on purpose: this repo lives under a directory with a space in
# its name on the author's machine, and $(CURDIR) would break on it.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

CLUSTER  ?= k8s-slo-platform
NAMESPACE ?= demo
SERVICES  ?= storefront catalog recs
REGISTRY  ?= ghcr.io/tani-mani-meow/k8s-slo-platform
TAG       ?= local

# Prefer a project-local virtualenv over whatever `python` happens to be on PATH.
# Developers commonly have several, and picking the wrong one produces a confusing
# ModuleNotFoundError for a dependency that is definitely installed - just not there.
PYTHON := $(shell if [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; \
                  elif [ -x .venv/bin/python ]; then echo .venv/bin/python; \
                  else echo python; fi)

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "; print "\nk8s-slo-platform\n"} \
		/^## ---/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 8); next} \
		/^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

## --- Quick start

.PHONY: demo
demo: ## Full stack on a fresh kind cluster (the reviewer entry point)
	@bash scripts/demo.sh

.PHONY: urls
urls: ## Print UI URLs and credentials, and start port-forwards
	@bash scripts/urls.sh

.PHONY: doctor
doctor: ## Check that required tools are installed and the machine can host the stack
	@bash scripts/doctor.sh

## --- Develop

.PHONY: venv
venv: ## Create .venv and install the Python dependencies
	@python -m venv .venv
	@$(PYTHON) -m pip install --quiet --upgrade pip
	@$(PYTHON) -m pip install --quiet -r app/requirements-dev.txt -r chaos/harness/requirements.txt
	@echo "  .venv ready - 'make test', 'make lint' and 'make chaos' will use it automatically"

.PHONY: test
test: ## Run unit tests (services, shared library and chaos harness)
	@$(PYTHON) -m pytest

.PHONY: lint
lint: ## Lint all Python
	@$(PYTHON) -m ruff check .

.PHONY: validate
validate: ## Render every Kustomize overlay and Helm chart without a cluster
	@bash scripts/validate.sh

.PHONY: build
build: ## Build all service images
	@bash scripts/build-images.sh $(TAG)

.PHONY: deploy-local
deploy-local: ## Build, load into kind and restart (fast inner loop, skips Git)
	@bash scripts/deploy-local.sh

.PHONY: slo-gen
slo-gen: ## Recompile SLO specs into PrometheusRules with Sloth
	@bash scripts/slo-gen.sh

.PHONY: sync-sql
sync-sql: ## Copy catalog migrations into the postgres seed Job's ConfigMap
	@bash scripts/sync-sql.sh

## --- Cluster

.PHONY: kind-up
kind-up: ## Create the local kind cluster
	@bash scripts/kind-up.sh

.PHONY: kind-down
kind-down: ## Delete the local kind cluster
	@kind delete cluster --name $(CLUSTER)

.PHONY: bootstrap
bootstrap: ## Install Argo CD and apply the root app-of-apps (kind)
	@bash scripts/bootstrap.sh kind

.PHONY: bootstrap-eks
bootstrap-eks: ## Install Argo CD and apply the root app-of-apps (EKS)
	@bash scripts/bootstrap.sh eks

.PHONY: sync
sync: ## Force an Argo CD sync of every application
	@kubectl -n argocd get applications.argoproj.io -o name | \
		xargs -r -I{} kubectl -n argocd patch {} --type merge \
		-p '{"operation":{"initiatedBy":{"username":"make-sync"},"sync":{"revision":"HEAD"}}}'

## --- Operate

.PHONY: chaos
chaos: ## Run one chaos scenario and measure MTTR (SCENARIO=<id>)
	@$(PYTHON) chaos/harness/run.py run $(SCENARIO)

.PHONY: chaos-list
chaos-list: ## List available chaos scenarios
	@$(PYTHON) chaos/harness/run.py list

.PHONY: chaos-all
chaos-all: ## Run the whole chaos suite sequentially
	@$(PYTHON) chaos/harness/run.py run-all

.PHONY: rollout-status
rollout-status: ## Watch the storefront canary
	@kubectl argo rollouts get rollout storefront -n $(NAMESPACE) --watch

.PHONY: rollout-retry
rollout-retry: ## Restart a rollout that aborted (Argo CD will not retrigger on its own)
	@kubectl argo rollouts retry rollout storefront -n $(NAMESPACE)

.PHONY: evidence
evidence: ## Rebuild the MTTR summary table from captured runs
	@$(PYTHON) chaos/harness/run.py report

## --- EKS capture sessions

.PHONY: eks-up
eks-up: ## terraform apply the capture cluster
	@cd infra/terraform && terraform init -input=false && terraform apply

.PHONY: eks-down
eks-down: ## Ordered teardown: delete LoadBalancer Services first, then destroy
	@bash scripts/eks-down.sh

.PHONY: clean
clean: ## Remove local build and test artefacts
	@rm -rf app/.pytest_cache app/.ruff_cache chaos/harness/out
	@find app -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
