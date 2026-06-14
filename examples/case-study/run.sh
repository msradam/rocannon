#!/usr/bin/env bash
# Reproduce the case study: Claude Haiku driving Rocannon's MCP tools (natural
# language -> ad-hoc Ansible) against a real Red Hat UBI9 (RHEL 9) node, plus
# the reflection and no-lock-in checks.
#
# Requires: docker; the `ansible` extra; the posix collection
#   (ansible-galaxy collection install ansible.posix); the Agent SDK
#   (uv pip install claude-agent-sdk); and a logged-in `claude` CLI (the SDK
#   reuses that session, so no API key is needed).
# Teardown: docker rm -f rocannon-demo-ubi9

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV=/tmp/rocannon-demo-env
run() { uv run --directory "$ROOT" "$@"; }

# Build the UBI9 SSH node + inventory (reuses the recording scaffold).
bash "$ROOT/docs/recording/setup-demo-env.sh" >/dev/null

# A wide profile for the reflection count, a focused one for the agent.
cat > "$ENV/profile-casestudy.yml" <<EOF
inventories: [$ENV/hosts.ini]
modules: [ansible.builtin, ansible.posix]
EOF
cat > "$ENV/profile-agent.yml" <<EOF
inventories: [$ENV/hosts.ini]
modules:
  - ansible.builtin.command
  - ansible.builtin.copy
  - ansible.builtin.setup
  - ansible.builtin.file
  - ansible.builtin.lineinfile
  - ansible.builtin.stat
  - ansible.builtin.service
  - ansible.builtin.ping
EOF

echo "== reflection: your collections become typed tools =="
run rocannon mcp doctor --profile "$ENV/profile-casestudy.yml" | grep -E "tools:|resources:"

echo "== natural language -> ad-hoc Ansible (Claude Haiku via the Agent SDK) =="
run python "$ROOT/examples/case-study/agent_demo.py" "$ENV/profile-agent.yml"

echo "== no lock-in: record a session, replay as vanilla ansible-playbook =="
INV=(--inventory "$ENV/hosts.ini")
rm -f "$ENV/runbook.yml"
run rocannon ansible.builtin.copy --target ubi9 "${INV[@]}" --content "Managed by Rocannon" --dest /etc/motd --record "$ENV/runbook.yml" >/dev/null
run rocannon ansible.builtin.lineinfile --target ubi9 "${INV[@]}" --path /etc/rocannon-demo.conf --line "feature.enabled=1" --create --record "$ENV/runbook.yml" >/dev/null
run ansible-playbook -i "$ENV/hosts.ini" "$ENV/runbook.yml" | tail -3
