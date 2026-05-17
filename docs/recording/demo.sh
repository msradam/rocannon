#!/usr/bin/env bash
# Driver for the README demo. Three mcphost invocations against real targets:
# UBI9 SSH container (Ansible), OpenTofu workspace + Docker daemon (Terraform),
# kind Kubernetes cluster with bitnami/nginx pre-deployed (Helm).

set -e

ENV_DIR="/tmp/rocannon-demo-env"
MODEL="${ROCANNON_DEMO_MODEL:-ollama:granite4.1:3b}"

if [[ ! -f "$ENV_DIR/mcp-ansible.json" ]]; then
  echo "Run docs/recording/setup-demo-env.sh first." >&2
  exit 1
fi

# Camel #C19A6B for the wordmark and section headers (gilt-on-cloth lettering).
GOLD=$'\033[38;2;193;154;107m'; RESET=$'\033[0m'

printf '%s' "$GOLD"
cat <<'SPLASH'

88d888b. .d8888b. .d8888b. .d8888b. 88d888b. 88d888b. .d8888b. 88d888b.
88'  `88 88'  `88 88'  `"" 88'  `88 88'  `88 88'  `88 88'  `88 88'  `88
88       88.  .88 88.  ... 88.  .88 88    88 88    88 88.  .88 88    88
dP       `88888P' `88888P' `88888P8 dP    dP dP    dP `88888P' dP    dP

SPLASH
printf '%s  Ansible, Terraform, Helm as typed MCP tools.\n' "$RESET"
sleep 1.5

ask() {
  local label="$1"; local cfg="$2"; local prompt="$3"; local steps="${4:-5}"
  printf '\n%s== %s ==%s\n' "$GOLD" "$label" "$RESET"
  sleep 0.4
  mcphost --config "$cfg" --model "$MODEL" --max-steps "$steps" --compact -p "$prompt"
  sleep 1
}

ask "ansible: how much memory is free on ubi9?" \
    "$ENV_DIR/mcp-ansible.json" \
    "Use ansible.builtin.command on the ubi9 host to run 'free -h'. Report the total memory and how much is available, in one sentence."

ask "terraform: provision a docker network for the app" \
    "$ENV_DIR/mcp-terraform.json" \
    "Use tf_docker_network with instance=app and name=rocannon-app-net to create a bridge network. Then tell me what network ID and IPv4 subnet were assigned."

ask "helm: what's deployed in the cluster?" \
    "$ENV_DIR/mcp-helm.json" \
    "Use helm_list with namespace=rocannon-demo to see what releases are deployed. Report just the release name, chart version, and status in one short line."

sleep 2
