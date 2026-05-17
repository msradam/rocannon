#!/usr/bin/env bash
# Build one Ansible target (UBI9 SSH container), pre-warm a Terraform workspace,
# verify a kind cluster, and write three per-cannon profiles + three mcp.json
# files under /tmp/rocannon-demo-env. Idempotent.

set -euo pipefail

ROCANNON_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_DIR="/tmp/rocannon-demo-env"
CONTAINER="rocannon-demo-ubi9"
SSH_PORT=2222
KIND_CLUSTER="rocannon-test"

mkdir -p "$ENV_DIR" "$ENV_DIR/tf-work"
SSH_KEY="$ENV_DIR/id_ed25519"

# ---------- Ansible target ----------

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

# ---------- kind cluster (for the Helm cannon) ----------

if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  echo "Creating kind cluster '$KIND_CLUSTER'..." >&2
  kind create cluster --name "$KIND_CLUSTER" >/dev/null
fi
kubectl --context "kind-$KIND_CLUSTER" wait --for=condition=Ready node --all --timeout=60s >/dev/null

# ---------- profiles + mcp.json (one per cannon) ----------

write_mcp_json() {
  local profile="$1"; local out="$2"
  cat > "$out" <<EOF
{
  "mcpServers": {
    "rocannon": {
      "command": "uv",
      "args": [
        "run", "--directory", "${ROCANNON_ROOT}",
        "rocannon", "mcp", "serve",
        "--profile", "${profile}"
      ]
    }
  }
}
EOF
}

# Ansible cannon: one module
cat > "$ENV_DIR/profile-ansible.yml" <<EOF
inventories:
  - ${ENV_DIR}/hosts.ini
modules:
  - ansible.builtin.command
EOF
write_mcp_json "$ENV_DIR/profile-ansible.yml" "$ENV_DIR/mcp-ansible.json"

# Terraform cannon: just the random provider (no creds needed, fast)
cat > "$ENV_DIR/profile-terraform.yml" <<EOF
terraform:
  workspace: ${ENV_DIR}/tf-work
  providers:
    random:
      source: hashicorp/random
      version: "~> 3.6"
EOF
write_mcp_json "$ENV_DIR/profile-terraform.yml" "$ENV_DIR/mcp-terraform.json"

# Helm cannon: one chart against the kind cluster
cat > "$ENV_DIR/profile-helm.yml" <<EOF
helm:
  charts:
    - name: bitnami/nginx
      version: "21.0.6"
  default_namespace: rocannon-demo
EOF
write_mcp_json "$ENV_DIR/profile-helm.yml" "$ENV_DIR/mcp-helm.json"

# Pre-warm the Terraform workspace so its `tofu init` doesn't run during
# recording. Calling `mcp doctor` constructs the server which triggers init.
( cd "$ENV_DIR" && uv run --directory "$ROCANNON_ROOT" rocannon mcp doctor \
    --profile "$ENV_DIR/profile-terraform.yml" >/dev/null ) || true

echo "Ready under $ENV_DIR:"
ls -1 "$ENV_DIR"/*.yml "$ENV_DIR"/*.json
