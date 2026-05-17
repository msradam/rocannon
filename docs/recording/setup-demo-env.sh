#!/usr/bin/env bash
# Build a UBI9 SSH container and write a profile + mcp.json that target it.
# All ephemeral state lives under /tmp/rocannon-demo-env. Idempotent.

set -euo pipefail

ROCANNON_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_DIR="/tmp/rocannon-demo-env"
CONTAINER="rocannon-demo-ubi9"
SSH_PORT=2222

mkdir -p "$ENV_DIR"
SSH_KEY="$ENV_DIR/id_ed25519"

if [[ ! -f "$SSH_KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -C "rocannon-demo" >/dev/null
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker build -t "${CONTAINER}:latest" -f - "$ENV_DIR" >/dev/null <<EOF
FROM redhat/ubi9-minimal
RUN microdnf install -y openssh-server openssh-clients python3 procps-ng \
        iproute net-tools iputils which sudo \
    && microdnf clean all \
    && ssh-keygen -A \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY id_ed25519.pub /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
EOF

docker run -d --name "$CONTAINER" -p "127.0.0.1:${SSH_PORT}:22" "${CONTAINER}:latest" >/dev/null

for _ in {1..20}; do
  if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
         -o ConnectTimeout=2 -p "$SSH_PORT" root@127.0.0.1 'echo ok' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

cat > "$ENV_DIR/hosts.ini" <<EOF
[demo]
ubi9 ansible_host=127.0.0.1 ansible_port=${SSH_PORT} ansible_user=root ansible_ssh_private_key_file=${SSH_KEY} ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
EOF

cat > "$ENV_DIR/profile.yml" <<EOF
inventories:
  - ${ENV_DIR}/hosts.ini
modules:
  - ansible.builtin.command
  - ansible.builtin.dnf
EOF

cat > "$ENV_DIR/mcp.json" <<EOF
{
  "mcpServers": {
    "rocannon": {
      "command": "uv",
      "args": [
        "run", "--directory", "${ROCANNON_ROOT}",
        "rocannon", "mcp", "serve",
        "--profile", "${ENV_DIR}/profile.yml"
      ]
    }
  }
}
EOF

echo "Container running:  $CONTAINER on 127.0.0.1:${SSH_PORT}"
echo "SSH key:            $SSH_KEY"
echo "Profile:            $ENV_DIR/profile.yml"
echo "MCP config:         $ENV_DIR/mcp.json"
