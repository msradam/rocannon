#!/usr/bin/env bash
# Driver for the README demo. Runs once setup-demo-env.sh has built the demo
# infrastructure. Three mcphost invocations, one per cannon.

set -e

ENV_DIR="/tmp/rocannon-demo-env"
MODEL="${ROCANNON_DEMO_MODEL:-ollama:granite4.1:3b}"

if [[ ! -f "$ENV_DIR/mcp-ansible.json" ]]; then
  echo "Run docs/recording/setup-demo-env.sh first." >&2
  exit 1
fi

# Title card
# Camel #C19A6B — warm, gilt-on-cloth lettering. Matches the gryphon glyph.
printf '\033[38;2;193;154;107m'
cat <<'SPLASH'

88d888b. .d8888b. .d8888b. .d8888b. 88d888b. 88d888b. .d8888b. 88d888b.
88'  `88 88'  `88 88'  `"" 88'  `88 88'  `88 88'  `88 88'  `88 88'  `88
88       88.  .88 88.  ... 88.  .88 88    88 88    88 88.  .88 88    88
dP       `88888P' `88888P' `88888P8 dP    dP dP    dP `88888P' dP    dP

SPLASH
printf '\033[0m  Ansible, Terraform, Helm as typed MCP tools.\n'
sleep 1.5

ask() {
  local label="$1"; local cfg="$2"; local prompt="$3"; local steps="${4:-4}"
  printf '\n\033[38;2;193;154;107m== %s ==\033[0m\n' "$label"
  sleep 0.4
  mcphost --config "$cfg" --model "$MODEL" --max-steps "$steps" --compact -p "$prompt"
  sleep 1
}

ask "ansible: what OS is ubi9 running?" \
    "$ENV_DIR/mcp-ansible.json" \
    "On the ubi9 host, use ansible.builtin.command to run 'cat /etc/os-release | head -5'. Then tell me what Linux distribution it is in one sentence."

ask "terraform: generate a random id" \
    "$ENV_DIR/mcp-terraform.json" \
    "Use tf_random_string with instance=demo, length=16, special=false. Tell me what string the result has."

ask "helm: what's deployed in the kind cluster?" \
    "$ENV_DIR/mcp-helm.json" \
    "Use helm_list with namespace=rocannon-demo. Report the release names you see or say the namespace is empty." \
    6

sleep 2
