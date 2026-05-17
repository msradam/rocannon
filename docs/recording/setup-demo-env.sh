#!/usr/bin/env bash
# Build a UBI9 SSH container, an OpenTofu workspace with the docker provider,
# and a kind cluster with bitnami/nginx pre-deployed. Writes three per-cannon
# profiles + matching mcp.json files under /tmp/rocannon-demo-env. Idempotent;
# tears down prior state first.

set -euo pipefail

ROCANNON_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_DIR="/tmp/rocannon-demo-env"
CONTAINER="rocannon-demo-ubi9"
SSH_PORT=2222
KIND_CLUSTER="rocannon-test"
APP_NET="rocannon-app-net"
HELM_RELEASE="rc-nginx"
HELM_NS="rocannon-demo"

# ---------- pick the right docker socket (Colima vs Docker Desktop) ----------

if [[ -S "$HOME/.colima/default/docker.sock" ]]; then
  DOCKER_SOCK="unix://$HOME/.colima/default/docker.sock"
elif [[ -S /var/run/docker.sock ]]; then
  DOCKER_SOCK="unix:///var/run/docker.sock"
else
  echo "No docker socket found. Start Colima or Docker Desktop first." >&2
  exit 1
fi

# ---------- tear down anything from a previous run ----------

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker network rm "$APP_NET" >/dev/null 2>&1 || true
if helm --kube-context "kind-$KIND_CLUSTER" status "$HELM_RELEASE" -n "$HELM_NS" >/dev/null 2>&1; then
  helm --kube-context "kind-$KIND_CLUSTER" uninstall "$HELM_RELEASE" -n "$HELM_NS" --wait >/dev/null 2>&1 || true
fi
rm -rf "$ENV_DIR/tf-work"
mkdir -p "$ENV_DIR" "$ENV_DIR/tf-work"

# ---------- SSH key + UBI9 container ----------

SSH_KEY="$ENV_DIR/id_ed25519"
if [[ ! -f "$SSH_KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -C "rocannon-demo" >/dev/null
fi

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

# ---------- per-cannon profiles ----------

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

cat > "$ENV_DIR/profile-ansible.yml" <<EOF
inventories:
  - ${ENV_DIR}/hosts.ini
modules:
  - ansible.builtin.command
EOF
write_mcp_json "$ENV_DIR/profile-ansible.yml" "$ENV_DIR/mcp-ansible.json"

# Terraform: docker provider, only docker_network exposed so the tool surface
# stays small enough for Granite 3B to pick the right tool reliably.
cat > "$ENV_DIR/profile-terraform.yml" <<EOF
terraform:
  workspace: ${ENV_DIR}/tf-work
  providers:
    docker:
      source: kreuzwerker/docker
      version: "~> 3.0"
  provider_config:
    docker:
      host: ${DOCKER_SOCK}
  expose_resources:
    - docker_network
EOF
write_mcp_json "$ENV_DIR/profile-terraform.yml" "$ENV_DIR/mcp-terraform.json"

cat > "$ENV_DIR/profile-helm.yml" <<EOF
helm:
  charts:
    - name: bitnami/nginx
      version: "21.0.6"
  default_namespace: ${HELM_NS}
EOF
write_mcp_json "$ENV_DIR/profile-helm.yml" "$ENV_DIR/mcp-helm.json"

# ---------- pre-warm + pre-deploy ----------

# Warm the Terraform workspace so `tofu init` doesn't run during recording.
( cd "$ENV_DIR" && uv run --directory "$ROCANNON_ROOT" rocannon mcp doctor \
    --profile "$ENV_DIR/profile-terraform.yml" >/dev/null ) || true

# Pre-deploy nginx so the Helm demo has a release to inspect.
echo "Deploying $HELM_RELEASE into kind..." >&2
helm --kube-context "kind-$KIND_CLUSTER" repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
helm --kube-context "kind-$KIND_CLUSTER" repo update bitnami >/dev/null 2>&1
helm --kube-context "kind-$KIND_CLUSTER" upgrade --install "$HELM_RELEASE" bitnami/nginx \
  --version 21.0.6 \
  --namespace "$HELM_NS" --create-namespace \
  --set replicaCount=2 --set service.type=ClusterIP \
  >/dev/null

echo "Ready under $ENV_DIR:"
ls -1 "$ENV_DIR"/*.yml "$ENV_DIR"/*.json
echo
echo "Helm release:"
helm --kube-context "kind-$KIND_CLUSTER" list -n "$HELM_NS"
