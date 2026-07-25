# microk8s-mcp — project setup
#
# Every target is overridable from the command line, e.g.
#   make install PYTHON=python3.12
#   make register SSH_HOST=pierre@k8s.lan
#
# If MicroK8s runs on another machine (the normal case), do the whole thing
# from your workstation:
#
#   make install
#   make node-setup SSH_HOST=pierre@k8s.lan
#   make register   SSH_HOST=pierre@k8s.lan
#
# The node-* targets reach the cluster over SSH; nothing is installed on the
# node. The plain rbac/kubeconfig targets are the same steps run locally, for
# when you are already sitting on the node.
#
# See install.txt for the narrative version of all this.

SHELL := /bin/bash

# --- toolchain ------------------------------------------------------------
PYTHON  ?= python3
VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
ENTRY   := $(abspath $(VENV))/bin/microk8s-mcp

# EDITABLE=1 (default) installs with `pip install -e .`, so the venv points back
# at this checkout and edits take effect immediately. Right for a .venv living
# inside the repo.
#
# EDITABLE=0 copies the package into the venv, which is what you want for a venv
# outside the repo (e.g. VENV=/opt/microk8s-mcp/venv): the install then survives
# the checkout being moved or deleted. Edits need a re-run of `make install`.
EDITABLE ?= 1

# --- cluster identity -----------------------------------------------------
KUBECTL     ?= microk8s kubectl
NAMESPACE   ?= mcp-system
APISERVER   ?=
KUBECONFIG_OUT ?= $(HOME)/.kube/claude-mcp.kubeconfig

# --- claude CLI registration ----------------------------------------------
SERVER_NAME ?= microk8s
KUBECONFIG_PATH ?= $(KUBECONFIG_OUT)
SSH_HOST    ?=
SSH_KEY     ?=
NAMESPACES  ?=
SCOPE       ?=

CLAUDE_ENV := --env MICROK8S_MCP_KUBECONFIG=$(KUBECONFIG_PATH)
ifneq ($(strip $(SSH_HOST)),)
CLAUDE_ENV += --env MICROK8S_MCP_SSH_HOST=$(SSH_HOST)
endif
ifneq ($(strip $(SSH_KEY)),)
CLAUDE_ENV += --env MICROK8S_MCP_SSH_KEY=$(SSH_KEY)
endif
ifneq ($(strip $(NAMESPACES)),)
CLAUDE_ENV += --env MICROK8S_MCP_NAMESPACES=$(NAMESPACES)
endif

# Absolute path to the local kubectl, baked into the registration. Claude Code
# launches the server with its own environment, so a kubectl in ~/.local/bin
# may be on your PATH and not on the server's - in which case it would silently
# fall back to `microk8s kubectl` on the node under that node's admin
# credential, quietly bypassing the scoped identity. Set KUBECTL_BIN= to skip.
KUBECTL_BIN ?= $(shell command -v kubectl 2>/dev/null)
ifneq ($(strip $(KUBECTL_BIN)),)
CLAUDE_ENV += --env MICROK8S_MCP_KUBECTL=$(KUBECTL_BIN)
endif

# --- read-write registration ----------------------------------------------
# These are what the SERVER is willing to do. They are not enforced by the API
# server -- RBAC in rbac.yaml is. Set both layers; see install.txt §3d.
#
# By default register-rw registers alongside the read-only entry as
# `microk8s-rw`. Pass RW_SERVER_NAME=microk8s to replace the read-only one
# instead of running the two side by side.
RW_SERVER_NAME         ?= $(if $(filter microk8s,$(SERVER_NAME)),microk8s-rw,$(SERVER_NAME))
ALLOW_ADDON_CHANGES    ?= true
ALLOW_NODE_OPS         ?= false
ALLOW_PROTECTED_WRITES ?= false

CLAUDE_RW_ENV := --env MICROK8S_MCP_ALLOW_ADDON_CHANGES=$(ALLOW_ADDON_CHANGES)
CLAUDE_RW_ENV += --env MICROK8S_MCP_ALLOW_NODE_OPS=$(ALLOW_NODE_OPS)
CLAUDE_RW_ENV += --env MICROK8S_MCP_ALLOW_PROTECTED_WRITES=$(ALLOW_PROTECTED_WRITES)

CLAUDE_SCOPE :=
ifneq ($(strip $(SCOPE)),)
CLAUDE_SCOPE := --scope $(SCOPE)
endif

# --- ssh to the node ------------------------------------------------------
# SSH_HOST is user@host. SSH_OPTS is appended to, not replaced, when SSH_KEY
# is set, so you can still override it wholesale for a jump host etc.
SSH_OPTS ?= -o ConnectTimeout=10
ifneq ($(strip $(SSH_KEY)),)
SSH_OPTS += -i $(SSH_KEY)
endif
SSH := ssh $(SSH_OPTS) $(SSH_HOST)

# The address the SERVER machine reaches the API server at.
#
# Explicit APISERVER always wins. Otherwise assume the node answers on the host
# you SSH to — but resolve it through `ssh -G` first, because SSH_HOST is often
# an alias from ~/.ssh/config ("k8s-node" -> 10.0.0.5) and kubectl does not
# read that file. Without this, an alias yields a kubeconfig that ssh can reach
# and kubectl cannot. `ssh -G` only parses config, it does not connect.
#
# `ssh -G` also strips any user@ prefix for us. The $(lastword $(subst ...))
# fallback covers ssh being absent or -G unsupported (very old OpenSSH).
ifneq ($(strip $(APISERVER)),)
NODE_HOST      := $(lastword $(subst @, ,$(SSH_HOST)))
NODE_APISERVER := $(strip $(APISERVER))
else ifneq ($(strip $(SSH_HOST)),)
NODE_HOST      := $(or \
  $(shell ssh -G $(SSH_OPTS) $(SSH_HOST) 2>/dev/null | awk '/^hostname /{print $$2; exit}'),\
  $(lastword $(subst @, ,$(SSH_HOST))))
NODE_APISERVER := https://$(NODE_HOST):16443
else
NODE_HOST      :=
NODE_APISERVER :=
endif

.DEFAULT_GOAL := help
.PHONY: help venv install dev check doctor require-ssh node-check check-apiserver \
        node-setup node-rbac node-kubeconfig node-status node-teardown \
        rbac kubeconfig register register-rw unregister list clean distclean

# --------------------------------------------------------------------------

help: ## Show this help
	@echo "microk8s-mcp — setup targets"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Remote node? Set SSH_HOST on the node-* and register targets:"
	@echo "  make node-setup SSH_HOST=user@k8s.lan"
	@echo
	@echo "Server entry point once installed:"
	@echo "  $(ENTRY)"

# --- install --------------------------------------------------------------

$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

venv: $(PY) ## Create the virtualenv only

install: $(PY) ## Install the server into VENV (EDITABLE=0 for a copy install)
ifeq ($(EDITABLE),0)
	$(PIP) install .
	# pip treats an already-installed same-version package as satisfied, so a
	# plain re-run would silently keep stale code. Force just our package back
	# in; --no-deps keeps it fast since dependencies are already resolved above.
	$(PIP) install --force-reinstall --no-deps .
else
	$(PIP) install -e .
endif
	@echo
	@echo "Installed ($(if $(filter 0,$(EDITABLE)),copy,editable)). Entry point: $(ENTRY)"

dev: install ## Install plus lint/format tooling
	$(PIP) install ruff

check: ## Verify the package imports and the entry point resolves
	@test -x "$(ENTRY)" || { echo "Not installed — run 'make install' first." >&2; exit 1; }
	@$(PY) -c "from microk8s_mcp.server import main; print('import ok: microk8s_mcp.server.main')"
	@echo "entry point ok: $(ENTRY)"

doctor: ## Report on the host prerequisites
	@echo "python:   $$($(PYTHON) --version 2>&1)"
	@echo "venv:     $$(test -x "$(PY)" && echo "$(VENV) present" || echo "missing — run 'make install'")"
	@echo "kubectl:  $$(command -v kubectl || echo 'not on PATH (falls back to microk8s kubectl over the node backend)')"
	@echo "claude:   $$(command -v claude || echo 'not on PATH')"
	@echo "ssh:      $$(command -v ssh || echo 'not on PATH')"
	@echo "kubeconfig: $$(test -f "$(KUBECONFIG_PATH)" && echo "$(KUBECONFIG_PATH)" || echo "absent at $(KUBECONFIG_PATH)")"
ifneq ($(strip $(SSH_HOST)),)
	@echo "node:     $$($(SSH) 'echo reachable; command -v microk8s >/dev/null && echo "  microk8s present" || echo "  microk8s NOT on PATH"' 2>/dev/null || echo "$(SSH_HOST) unreachable over ssh")"
	@echo "apiserver: $(NODE_APISERVER)"
else
	@echo "node:     set SSH_HOST=user@host to probe the MicroK8s node"
endif

# --- node setup over SSH (run these FROM the workstation) -----------------

require-ssh:
	@test -n "$(strip $(SSH_HOST))" || { \
	  echo "Set SSH_HOST, e.g. make $(MAKECMDGOALS) SSH_HOST=pierre@k8s.lan" >&2; exit 1; }

node-check: require-ssh check-apiserver ## Test SSH reachability and MicroK8s readiness on the node
	@echo "ssh:       $(SSH_HOST)"
	@$(SSH) true 2>/dev/null || { echo "  cannot reach $(SSH_HOST) over ssh" >&2; exit 1; }
	@echo "  reachable"
	@$(SSH) 'command -v microk8s >/dev/null' 2>/dev/null \
	  || { echo "  microk8s not on PATH in a non-interactive shell — try KUBECTL='/snap/bin/microk8s kubectl'" >&2; exit 1; }
	@echo "  microk8s present"
	@$(SSH) '$(KUBECTL) version --client >/dev/null 2>&1' \
	  || { echo "  '$(KUBECTL)' did not run on the node" >&2; exit 1; }
	@echo "  kubectl ok: $(KUBECTL)"

# The kubeconfig is only useful if the SERVER machine can reach the address
# baked into it. An SSH alias that ssh resolves and kubectl cannot is the easy
# mistake here, so refuse to mint one rather than hand over a dead credential.
# A prerequisite of both node-check and node-kubeconfig; phony, so one run.
check-apiserver: ## Check the API server address resolves and answers from here
	@echo "apiserver: $(NODE_APISERVER)$(if $(strip $(APISERVER)), (explicit), (derived from $(SSH_HOST)))"
	@test -n "$(strip $(NODE_APISERVER))" || { echo "No APISERVER and no SSH_HOST." >&2; exit 1; }
	@host=$$(echo '$(NODE_APISERVER)' | sed -E 's#^[a-z]+://##; s#/.*##; s#:[0-9]+$$##'); \
	port=$$(echo '$(NODE_APISERVER)' | sed -nE 's#.*:([0-9]+)/?$$#\1#p'); \
	[ -n "$$port" ] || port=16443; \
	if ! getent hosts "$$host" >/dev/null 2>&1; then \
	  echo "  '$$host' does not resolve on this machine." >&2; \
	  echo "  kubectl does not read ~/.ssh/config, so an ssh alias will not work here." >&2; \
	  echo "  Pass a real address: make $(firstword $(MAKECMDGOALS)) SSH_HOST=$(SSH_HOST) APISERVER=https://<ip-or-dns>:16443" >&2; \
	  exit 1; \
	fi; \
	echo "  resolves ok: $$host"; \
	if timeout 5 bash -c "cat </dev/null >/dev/tcp/$$host/$$port" 2>/dev/null; then \
	  echo "  reachable from here: $$host:$$port"; \
	else \
	  echo "  WARNING: cannot open $$host:$$port from this machine." >&2; \
	  echo "  The kubeconfig will be written, but the server will not be able to use it" >&2; \
	  echo "  until that address is reachable and present in the API server cert SANs." >&2; \
	fi

node-setup: node-check node-rbac node-kubeconfig ## Full node-side setup over SSH (rbac + kubeconfig)
	@echo
	@echo "Node setup complete. Next:"
	@echo "  make register SSH_HOST=$(SSH_HOST)"

node-rbac: require-ssh ## Apply rbac.yaml on the node over SSH
	@echo "Applying rbac.yaml on $(SSH_HOST)"
	@$(SSH) '$(KUBECTL) apply -f -' < rbac.yaml

# Runs make-kubeconfig.sh on the node via 'bash -s' and captures its stdout
# here. Written to a temp file and validated first, so a failed SSH can never
# leave a truncated kubeconfig in place.
node-kubeconfig: require-ssh check-apiserver ## Mint the kubeconfig on the node and pull it back over SSH
	@mkdir -p $(dir $(KUBECONFIG_OUT))
	@tmp="$(KUBECONFIG_OUT).partial.$$$$"; \
	trap 'rm -f "$$tmp"' EXIT; \
	echo "Minting kubeconfig on $(SSH_HOST) for $(NODE_APISERVER)"; \
	$(SSH) "APISERVER='$(NODE_APISERVER)' NAMESPACE='$(NAMESPACE)' KUBECTL='$(KUBECTL)' bash -s" \
	  < make-kubeconfig.sh > "$$tmp" || { echo "Remote script failed; $(KUBECONFIG_OUT) left untouched." >&2; exit 1; }; \
	grep -q '^kind: Config' "$$tmp" || { echo "Output is not a kubeconfig; $(KUBECONFIG_OUT) left untouched." >&2; exit 1; }; \
	grep -q 'token: ..*' "$$tmp" || { echo "Kubeconfig carries no token; $(KUBECONFIG_OUT) left untouched." >&2; exit 1; }; \
	install -m 600 "$$tmp" "$(KUBECONFIG_OUT)"
	@echo "Wrote $(KUBECONFIG_OUT) (mode 600)"

node-status: require-ssh ## Run 'microk8s status' on the node
	@$(SSH) 'microk8s status'

node-teardown: require-ssh ## Delete the claude-mcp identity from the cluster
	@$(SSH) '$(KUBECTL) delete -f -' < rbac.yaml

# --- cluster identity (run these ON the MicroK8s node) --------------------

rbac: ## Apply the scoped RBAC role locally
	$(KUBECTL) apply -f rbac.yaml

kubeconfig: ## Mint a scoped kubeconfig locally — needs APISERVER=
	@test -n "$(strip $(APISERVER))" || { \
	  echo "Set APISERVER, e.g. make kubeconfig APISERVER=https://k8s.lan:16443" >&2; exit 1; }
	@mkdir -p $(dir $(KUBECONFIG_OUT))
	APISERVER=$(APISERVER) NAMESPACE=$(NAMESPACE) KUBECTL="$(KUBECTL)" \
	  bash make-kubeconfig.sh > $(KUBECONFIG_OUT)
	chmod 600 $(KUBECONFIG_OUT)
	@echo "Wrote $(KUBECONFIG_OUT)"

# --- claude CLI registration ----------------------------------------------

register: check ## Register a read-only server with the claude CLI
	claude mcp add --transport stdio $(CLAUDE_SCOPE) $(SERVER_NAME) \
	  $(CLAUDE_ENV) \
	  -- $(ENTRY)

register-rw: check ## Register a read-write server (name defaults to microk8s-rw)
	@echo "Registering '$(RW_SERVER_NAME)' read-write:"
	@echo "  namespaces:        $(if $(strip $(NAMESPACES)),$(NAMESPACES),* (all))"
	@echo "  addon changes:     $(ALLOW_ADDON_CHANGES)"
	@echo "  node cordon/drain: $(ALLOW_NODE_OPS)"
	@echo "  protected writes:  $(ALLOW_PROTECTED_WRITES)"
	claude mcp add --transport stdio $(CLAUDE_SCOPE) $(RW_SERVER_NAME) \
	  $(CLAUDE_ENV) \
	  --env MICROK8S_MCP_MODE=read-write \
	  $(CLAUDE_RW_ENV) \
	  -- $(ENTRY)

unregister: ## Remove SERVER_NAME from the claude CLI
	claude mcp remove $(CLAUDE_SCOPE) $(SERVER_NAME)

list: ## Show registered MCP servers
	claude mcp list

# --- housekeeping ---------------------------------------------------------

clean: ## Remove build artefacts and caches
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)