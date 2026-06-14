#!/usr/bin/env bash
# Reproduce the case study in this directory's README: rocannon against a real
# Red Hat UBI9 (RHEL 9) node over SSH, using ansible.builtin + ansible.posix.
#
# Requires: docker, the `ansible` extra, and the posix collection:
#   ansible-galaxy collection install ansible.posix
# Teardown when done: docker rm -f rocannon-demo-ubi9

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV=/tmp/rocannon-demo-env
INV=(--inventory "$ENV/hosts.ini")
run() { uv run --directory "$ROOT" "$@"; }
status() { python3 -c "import sys,json; r=json.load(sys.stdin); print('  status=%s changed=%s%s' % (r['status'], r['changed'], '' if 'check_mode' not in r else ' check_mode=%s' % r['check_mode']))"; }

# Build the UBI9 SSH node + inventory (reuses the recording scaffold).
bash "$ROOT/docs/recording/setup-demo-env.sh" >/dev/null
cat > "$ENV/profile-casestudy.yml" <<EOF
inventories:
  - $ENV/hosts.ini
modules:
  - ansible.builtin
  - ansible.posix
EOF

echo "== 1. reflection: your collections become tools =="
run rocannon mcp doctor --profile "$ENV/profile-casestudy.yml" | grep -E "create_server|tools:|resources"

echo "== 2. connectivity =="
run rocannon ansible.builtin.ping --target ubi9 "${INV[@]}" | status

echo "== 3. real facts =="
run rocannon ansible.builtin.setup --target ubi9 "${INV[@]}" 2>/dev/null | python3 -c "import sys,json; f=json.load(sys.stdin)['result']['ansible_facts']; print('  '+'  '.join('%s=%s'%(k,f[k]) for k in ['ansible_distribution','ansible_distribution_version','ansible_pkg_mgr','ansible_architecture']))"

echo "== 4. idempotent change =="
run rocannon ansible.builtin.copy --target ubi9 "${INV[@]}" --content "Managed by rocannon" --dest /etc/motd | status
run rocannon ansible.builtin.copy --target ubi9 "${INV[@]}" --content "Managed by rocannon" --dest /etc/motd | status

echo "== 5. dry-run (--check) then apply =="
run rocannon ansible.builtin.lineinfile --target ubi9 "${INV[@]}" --path /etc/rocannon-demo.conf --line "feature.enabled=1" --create --check | status
run rocannon ansible.builtin.stat --target ubi9 "${INV[@]}" --path /etc/rocannon-demo.conf 2>/dev/null | python3 -c "import sys,json; print('  after preview -> exists:', json.load(sys.stdin)['result']['stat']['exists'])"
run rocannon ansible.builtin.lineinfile --target ubi9 "${INV[@]}" --path /etc/rocannon-demo.conf --line "feature.enabled=1" --create | status

echo "== 6. third-party collection (ansible.posix) =="
ssh-keygen -t ed25519 -N "" -f "$ENV/throwaway" -C rocannon-casestudy >/dev/null 2>&1 || true
run rocannon ansible.posix.authorized_key --target ubi9 "${INV[@]}" --user root --key "$(cat "$ENV/throwaway.pub")" | status

echo "== 7. record a session, replay as vanilla ansible-playbook =="
rm -f "$ENV/runbook.yml"
run rocannon ansible.builtin.copy --target ubi9 "${INV[@]}" --content "Managed by rocannon" --dest /etc/motd --record "$ENV/runbook.yml" >/dev/null
run rocannon ansible.builtin.lineinfile --target ubi9 "${INV[@]}" --path /etc/rocannon-demo.conf --line "feature.enabled=1" --create --record "$ENV/runbook.yml" >/dev/null
run ansible-playbook -i "$ENV/hosts.ini" "$ENV/runbook.yml" | tail -3
