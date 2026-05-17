#!/usr/bin/env bash
# Three-cannon demo prep: builds the UBI9 SSH target container, ensures kind
# cluster is up, prints the connection info. Idempotent.
#
# Tears down with `teardown.sh`.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$HERE/_work"
mkdir -p "$WORK"

# Ansible target, UBI9 container with sshd, exposed on host port 2222.
SSH_KEY="$WORK/id_rsa"
UBI_IMAGE="rocannon-demo-ubi9:latest"
UBI_NAME="rocannon-demo-ubi9"
KIND_CLUSTER="rocannon-test"

# 1. SSH keypair (per-demo, never re-used outside it).
if [[ ! -f "$SSH_KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -C "rocannon-demo" >/dev/null
  echo "[setup] generated ssh key at $SSH_KEY"
fi

# 2. Build the UBI9-with-sshd image (cheap; uses microdnf, no systemd).
echo "[setup] building $UBI_IMAGE..."
docker build -t "$UBI_IMAGE" -f - "$WORK" <<EOF >/dev/null
FROM redhat/ubi9-minimal
RUN microdnf install -y openssh-server openssh-clients python3 \
        procps-ng iproute net-tools iputils which sudo \
    && microdnf clean all \
    && ssh-keygen -A \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY id_rsa.pub /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
EOF

# 3. Run (recreate if exists).
docker rm -f "$UBI_NAME" >/dev/null 2>&1 || true
docker run -d --name "$UBI_NAME" -p 127.0.0.1:2222:22 "$UBI_IMAGE" >/dev/null
echo "[setup] $UBI_NAME running, ssh on 127.0.0.1:2222"

# 4. Wait for sshd to accept connections.
for i in {1..20}; do
  if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
         -p 2222 root@127.0.0.1 -o ConnectTimeout=2 'echo ok' >/dev/null 2>&1; then
    echo "[setup] sshd ready after ${i}s"
    break
  fi
  sleep 1
done

# 5. kind cluster (assumes brew install kind already done).
if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER"; then
  echo "[setup] creating kind cluster '$KIND_CLUSTER'..."
  kind create cluster --name "$KIND_CLUSTER" >/dev/null
fi
kubectl --context "kind-$KIND_CLUSTER" wait --for=condition=Ready node --all --timeout=60s >/dev/null
echo "[setup] kind cluster '$KIND_CLUSTER' ready"

# 6. Write the ansible inventory.
cat > "$WORK/hosts.ini" <<EOF
[ubi]
ubi9 ansible_host=127.0.0.1 ansible_port=2222 ansible_user=root ansible_ssh_private_key_file=$SSH_KEY ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
EOF
echo "[setup] inventory at $WORK/hosts.ini"

# 7. Quick ansible ping to prove the target is fully ready.
if ansible -i "$WORK/hosts.ini" -m ping ubi9 >/dev/null 2>&1; then
  echo "[setup] ansible ping → ubi9 OK"
else
  echo "[setup] WARN: ansible ping failed; the demo may not work" >&2
fi

cat <<EOF

Ready. Test workspace: $WORK
  Ansible target: ubi9 (root@127.0.0.1:2222)
  Terraform workspace: $WORK/tf
  Kind cluster:   $KIND_CLUSTER (context kind-$KIND_CLUSTER)

Run the demo:    python3 $HERE/run.py
Tear down:       $HERE/teardown.sh
EOF
