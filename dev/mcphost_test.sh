#!/usr/bin/env bash
# End-to-end smoke test: rocannon MCP server driven by mcphost + a local LLM.
#
#   Usage:  dev/mcphost_test.sh [phase]
#           phase = ping | inventory | save | replay | all  (default: all)
#
#   Env knobs:
#     ROCANNON_TEST_MODEL  — mcphost model spec (default: ollama:granite4.1:3b)
#     ROCANNON_TEST_DIR    — keep the temp workspace at this path (default: random)
#     ROCANNON_MAX_STEPS   — agent step budget per phase (default: 10)
#
# Requires: mcphost on PATH; ollama serving the chosen model; uv on PATH.

set -euo pipefail

ROCANNON_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PHASE="${1:-all}"
MODEL="${ROCANNON_TEST_MODEL:-ollama:granite4.1:3b}"
MAX_STEPS="${ROCANNON_MAX_STEPS:-10}"

if [[ -n "${ROCANNON_TEST_DIR:-}" ]]; then
  TEST_DIR="$ROCANNON_TEST_DIR"
  mkdir -p "$TEST_DIR"
else
  TEST_DIR="$(mktemp -d -t rocannon_mcphost.XXXXXX)"
fi

cd "$TEST_DIR"

# Inventory + profile + mcphost config (idempotent — overwrites every run)
cat > hosts <<EOF
[local]
localhost ansible_connection=local
EOF

cat > profile.yml <<EOF
inventories:
  - $TEST_DIR/hosts
modules:
  - ansible.builtin.ping
  - ansible.builtin.debug
  - ansible.builtin.command
EOF

cat > mcp.json <<EOF
{
  "mcpServers": {
    "rocannon": {
      "command": "env",
      "args": [
        "ROCANNON_DATA_DIR=$TEST_DIR",
        "uv", "run", "--directory", "$ROCANNON_ROOT",
        "rocannon", "mcp",
        "--profile", "$TEST_DIR/profile.yml"
      ]
    }
  }
}
EOF

banner() { printf "\n=== %s ===\n" "$1"; }

run_phase() {
  local label="$1" prompt="$2"
  banner "$label"
  mcphost --config mcp.json --model "$MODEL" \
          --max-steps "$MAX_STEPS" --compact \
          --prompt "$prompt" || echo "[phase exited nonzero — continuing]"
}

case "$PHASE" in
  ping|inventory|save|replay|all) ;;
  *) echo "unknown phase: $PHASE" >&2; exit 64 ;;
esac

if [[ "$PHASE" == "ping" || "$PHASE" == "all" ]]; then
  run_phase "Phase: ping" \
    "Use the ansible.builtin.ping tool to ping the host called 'localhost'. Then tell me whether it succeeded."
fi

if [[ "$PHASE" == "inventory" || "$PHASE" == "all" ]]; then
  run_phase "Phase: inventory resource" \
    "Read the resource at URI rocannon://inventory and tell me what hosts and groups are configured."
fi

if [[ "$PHASE" == "save" || "$PHASE" == "all" ]]; then
  run_phase "Phase: save_playbook" \
    "First, use ansible.builtin.ping to ping 'localhost'. Then call save_playbook with name=\"smoke_replay\", description=\"smoke test\", and steps as a list with one object: {\"module\": \"ansible.builtin.ping\", \"target\": \"localhost\", \"args\": {}}. Report the result."
  banner "Files on disk under $TEST_DIR/.rocannon"
  find "$TEST_DIR/.rocannon" -type f 2>/dev/null | xargs -I{} sh -c 'echo "--- {} ---"; cat {}'
fi

if [[ "$PHASE" == "replay" || "$PHASE" == "all" ]]; then
  run_phase "Phase: list prompts (should include playbook_smoke_replay if save worked)" \
    "List all MCP prompts that are available to you, by name."
fi

printf "\nDone. Test workspace: %s\n" "$TEST_DIR"
