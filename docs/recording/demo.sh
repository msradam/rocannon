#!/usr/bin/env bash
# Driver for the README demo. Runs once setup-demo-env.sh has built the UBI9
# container and written /tmp/rocannon-demo-env/{profile,mcp.json,hosts.ini}.
# Recorded with asciinema; see docs/recording/README.md.

set -e

ENV_DIR="/tmp/rocannon-demo-env"
MODEL="${ROCANNON_DEMO_MODEL:-ollama:granite4.1:3b}"

if [[ ! -f "$ENV_DIR/mcp.json" ]]; then
  echo "Run docs/recording/setup-demo-env.sh first." >&2
  exit 1
fi

# Title card
printf '\033[36m'
cat <<'SPLASH'

'||'''|,
 ||   ||
 ||...|' .|''|, .|'',  '''|.  `||''|,  `||''|,  .|''|, `||''|,
 || \\   ||  || ||    .|''||   ||  ||   ||  ||  ||  ||  ||  ||
.||  \\. `|..|' `|..' `|..||. .||  ||. .||  ||. `|..|' .||  ||.

SPLASH
printf '\033[0m  Ansible, Terraform, Helm as typed MCP tools.\n'
sleep 2

printf '\n\033[1;36m$\033[0m docker ps --filter name=rocannon-demo --format "{{.Names}} {{.Image}} {{.Status}}"\n'
sleep 0.3
docker ps --filter name=rocannon-demo --format "{{.Names}}  {{.Image}}  {{.Status}}"
sleep 1.5

printf '\n\033[1;36m$\033[0m cat /tmp/rocannon-demo-env/profile.yml\n'
sleep 0.3
cat "$ENV_DIR/profile.yml"
sleep 1.5

printf '\n\033[1;36m$\033[0m mcphost --config mcp.json --model %s --compact \\\n' "$MODEL"
printf '       -p "On ubi9, run cat /etc/os-release via ansible.builtin.command.\n'
printf '           Tell me what Linux it is."\n'
sleep 0.8

mcphost --config "$ENV_DIR/mcp.json" --model "$MODEL" --max-steps 5 --compact \
  -p "On the ubi9 host, use ansible.builtin.command to run 'cat /etc/os-release | head -5'. Then tell me what Linux distribution it is in one sentence."

sleep 1
