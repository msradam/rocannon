#!/usr/bin/env bash
# Headline demo: Claude Haiku (via the Claude Agent SDK) driving Rocannon's
# typed Ansible-module MCP tools against a real RHEL 9 (UBI9) container, in
# natural language. Build the demo env first:
#   bash docs/recording/setup-demo-env.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV=/tmp/rocannon-demo-env
run() { uv run --directory "$ROOT" "$@"; }

clear
printf '\033[1mrocannon\033[0m  every installed Ansible module as a typed MCP tool\n'
printf 'Claude Haiku driving a real RHEL 9 host in natural language\n\n'
sleep 2

printf '\033[2m$ rocannon mcp doctor --profile profile-agent.yml\033[0m\n'
run rocannon mcp doctor --profile "$ENV/profile-agent.yml" | grep -E "tools:|resources:"
sleep 2
printf '\n'

printf '\033[2m$ python agent_demo.py\033[0m\n'
run python "$ROOT/examples/case-study/agent_demo.py" "$ENV/profile-agent.yml"
sleep 1
