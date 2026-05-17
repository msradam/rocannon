#!/usr/bin/env bash
# Driver for the README demo. Recorded with asciinema; see docs/recording/README.md.

set -e
cd "$(dirname "$0")/../../examples/quickstart"

step() {
  printf '\n\033[1;36m$\033[0m %s\n' "$1"
  sleep 0.4
}

# Title card: fender wordmark + tagline
printf '\033[36m'
cat <<'SPLASH'

'||'''|,
 ||   ||
 ||...|' .|''|, .|'',  '''|.  `||''|,  `||''|,  .|''|, `||''|,
 || \\   ||  || ||    .|''||   ||  ||   ||  ||  ||  ||  ||  ||
.||  \\. `|..|' `|..' `|..||. .||  ||. .||  ||. `|..|' .||  ||.

SPLASH
printf '\033[0m  Ansible, Terraform, Helm as typed MCP tools.\n'
sleep 2.5

step "rocannon mcp doctor --profile profile.yml"
uv run rocannon mcp doctor --profile profile.yml
sleep 2

step "rocannon doc ansible.builtin.ping"
uv run rocannon doc ansible.builtin.ping
sleep 2

step "rocannon run ansible.builtin.ping --target localhost -i hosts --pretty"
uv run rocannon run ansible.builtin.ping --target localhost -i hosts --pretty
sleep 2

step "rocannon ls modules --profile profile.yml | head"
uv run rocannon ls modules --profile profile.yml | head
sleep 2

printf '\n'
