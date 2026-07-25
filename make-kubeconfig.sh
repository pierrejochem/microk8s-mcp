#!/usr/bin/env bash
# Build a scoped kubeconfig for the claude-mcp ServiceAccount.
#
# Run this ON the MicroK8s node, after applying rbac.yaml:
#   microk8s kubectl apply -f rbac.yaml
#   ./make-kubeconfig.sh > ~/claude-mcp.kubeconfig
#
# Then copy the file to whichever machine runs the MCP server and point
# MICROK8S_MCP_KUBECONFIG at it.
set -euo pipefail

NAMESPACE="${NAMESPACE:-mcp-system}"
SECRET="${SECRET:-claude-mcp-token}"
KUBECTL="${KUBECTL:-microk8s kubectl}"

# Address the API server is reachable at from the client machine. MicroK8s
# serves the API on port 16443. Override if you reach the node by hostname:
#   APISERVER=https://k8s.lan:16443 ./make-kubeconfig.sh
if [[ -z "${APISERVER:-}" ]]; then
  NODE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
  APISERVER="https://${NODE_IP:-127.0.0.1}:16443"
fi

if ! $KUBECTL -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1; then
  echo "Secret $NAMESPACE/$SECRET not found. Apply rbac.yaml first." >&2
  exit 1
fi

# The token controller populates the secret asynchronously; wait briefly.
for _ in $(seq 1 15); do
  TOKEN="$($KUBECTL -n "$NAMESPACE" get secret "$SECRET" \
    -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
  [[ -n "$TOKEN" ]] && break
  sleep 1
done

if [[ -z "${TOKEN:-}" ]]; then
  echo "Token was never populated in $NAMESPACE/$SECRET." >&2
  exit 1
fi

CA="$($KUBECTL -n "$NAMESPACE" get secret "$SECRET" \
  -o jsonpath='{.data.ca\.crt}')"

cat <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: microk8s
    cluster:
      server: ${APISERVER}
      certificate-authority-data: ${CA}
contexts:
  - name: claude-mcp
    context:
      cluster: microk8s
      user: claude-mcp
current-context: claude-mcp
users:
  - name: claude-mcp
    user:
      token: ${TOKEN}
EOF
