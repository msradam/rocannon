#!/usr/bin/env bash
# Stand up a demo web host and serve Rocannon over authenticated HTTP so
# Grafana Assistant can call it as a remote MCP server.
#
#   ./run.sh            local only (http://127.0.0.1:8000/mcp)
#   ./run.sh --tunnel   also open a Cloudflare quick tunnel (public HTTPS URL)
#
# Requires: docker, uv. Teardown: docker rm -f rocannon-web01 rocannon-tunnel

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV=/tmp/rocannon-grafana-assistant
HOST=web01
SSH_PORT=2230
PORT=8000

docker rm -f "rocannon-$HOST" rocannon-tunnel >/dev/null 2>&1 || true
mkdir -p "$ENV"
[[ -f "$ENV/id_ed25519" ]] || ssh-keygen -t ed25519 -N "" -f "$ENV/id_ed25519" -C rocannon-demo >/dev/null
[[ -f "$ENV/token" ]] || python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$ENV/token"

# Ubuntu rather than UBI: its nginx ships an init script, so the service
# module can stop and start it without systemd inside the container.
docker build -q -t "rocannon-$HOST" -f - "$ENV" >/dev/null <<'EOF'
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server python3 nginx curl procps \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh
COPY id_ed25519.pub /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys
CMD ["sh", "-c", "service nginx start && exec /usr/sbin/sshd -D -e"]
EOF
docker run -d --name "rocannon-$HOST" --hostname "$HOST" -p "127.0.0.1:$SSH_PORT:22" "rocannon-$HOST" >/dev/null

cat > "$ENV/hosts.ini" <<EOF
[web]
$HOST ansible_host=127.0.0.1 ansible_port=$SSH_PORT ansible_user=root ansible_ssh_private_key_file=$ENV/id_ed25519 ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
EOF
sed "s#__INVENTORY__#$ENV/hosts.ini#" "$ROOT/examples/grafana-assistant/profile.yml" > "$ENV/profile.yml"

for _ in {1..20}; do
  ssh -q -i "$ENV/id_ed25519" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=2 -p "$SSH_PORT" root@127.0.0.1 true 2>/dev/null && break
  sleep 1
done

URL="http://127.0.0.1:$PORT/mcp"
if [[ "${1:-}" == "--tunnel" ]]; then
  docker run -d --name rocannon-tunnel cloudflare/cloudflared:latest \
      tunnel --no-autoupdate --url "http://host.docker.internal:$PORT" >/dev/null
  for _ in {1..30}; do
    PUBLIC=$(docker logs rocannon-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1 || true)
    [[ -n "$PUBLIC" ]] && break
    sleep 1
  done
  URL="${PUBLIC:?tunnel did not come up, see: docker logs rocannon-tunnel}/mcp"
fi

cat <<EOF

Grafana Assistant > Settings > MCP servers > Add:
  URL:     $URL
  Header:  Authorization: Bearer $(cat "$ENV/token")

Break the host:  docker exec rocannon-$HOST service nginx stop
Then ask:        "web01 stopped serving traffic. Find out why and fix it."

Serving Rocannon on 127.0.0.1:$PORT (Ctrl-C to stop).
EOF

ROCANNON_HTTP_TOKEN="$(cat "$ENV/token")" exec uv run --directory "$ROOT" \
    rocannon mcp serve --transport http --port "$PORT" --profile "$ENV/profile.yml"
