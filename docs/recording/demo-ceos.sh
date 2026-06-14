#!/usr/bin/env bash
# Network clip: Claude Haiku (via the Claude Agent SDK) driving Rocannon's
# arista.eos MCP tools against a two-node Arista cEOS fabric under containerlab.
#
# Rocannon runs where the lab's management network is reachable. Point the agent
# at it over SSH:
#   export ROCANNON_SSH=user@labhost
#   export ROCANNON_SSH_CMD="cd /path/to/rocannon && uv run rocannon mcp serve --profile /path/to/ceos-profile.yml"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
: "${ROCANNON_SSH:?set ROCANNON_SSH=user@labhost}"

clear
printf '\033[1mrocannon\033[0m  your Ansible collections as typed MCP tools\n'
printf 'Claude Haiku driving a 2-node Arista cEOS fabric, in natural language\n\n'
sleep 2

uv run --directory "$ROOT" python "$ROOT/examples/containerlab/agent_demo.py"
sleep 1
