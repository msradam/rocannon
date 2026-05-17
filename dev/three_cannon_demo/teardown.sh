#!/usr/bin/env bash
# Tear down everything setup.sh created. Idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$HERE/_work"
UBI_NAME="rocannon-demo-ubi9"
KIND_CLUSTER="rocannon-test"

# Helm releases first (so kubectl-finalizers don't trip up cluster delete).
if kubectl --context "kind-$KIND_CLUSTER" get ns 2>/dev/null | grep -q rocannon-demo; then
  helm --kube-context "kind-$KIND_CLUSTER" list --all-namespaces -q \
    | xargs -r -I{} helm --kube-context "kind-$KIND_CLUSTER" uninstall {} --namespace rocannon-demo 2>/dev/null || true
fi

# Terraform workspace
if [[ -d "$WORK/tf" ]]; then
  (cd "$WORK/tf" && tofu destroy -auto-approve -no-color >/dev/null 2>&1) || true
fi

# Containers
docker rm -f "$UBI_NAME" 2>/dev/null || true
docker ps --filter "name=rocannon_demo_" -q | xargs -r docker rm -f >/dev/null 2>&1 || true

# Kind cluster
kind delete cluster --name "$KIND_CLUSTER" 2>/dev/null || true

# Files (keep ssh key for reuse on next setup, but blow away the rest)
rm -rf "$WORK/tf" "$WORK/hosts.ini" 2>/dev/null || true

echo "[teardown] done"
