# Grafana Assistant: diagnose and fix a host

Grafana Assistant can tell you why a service is down. With Rocannon connected
as one of its MCP servers, it can also check the host and bring the service
back, using your own Ansible modules and inventory.

This example starts one Ubuntu host running nginx, serves Rocannon over HTTP
with a bearer token, and gives you the URL and header to paste into Assistant.

## Run it

```bash
./run.sh            # local only: http://127.0.0.1:8000/mcp
./run.sh --tunnel   # also opens a Cloudflare quick tunnel for a public HTTPS URL
```

Assistant runs in Grafana Cloud and only connects to remote servers it can
reach over the network, so a Grafana Cloud stack needs `--tunnel` (or your own
reverse proxy). The tunnel runs in a container, so there is nothing to install.
Requires docker and uv.

The script prints what to enter under **Assistant settings > MCP servers**:

```
URL:     https://<random>.trycloudflare.com/mcp
Header:  Authorization: Bearer <token>
```

The token is generated once and kept in `/tmp/rocannon-grafana-assistant/token`.
Requests without it get a 401.

## The scenario

Break the host:

```bash
docker exec rocannon-web01 service nginx stop
```

Ask Assistant:

> web01 stopped serving traffic. Find out why and fix it.

A run of the same tool calls over the HTTP endpoint:

| Call | Result |
|---|---|
| `ansible_builtin_uri` `http://localhost/` | failed: connection refused |
| `ansible_builtin_service_facts` | nginx `stopped` |
| `ansible_builtin_service` `name=nginx state=started` | successful, `changed=true` |
| `ansible_builtin_uri` `http://localhost/` | successful, HTTP 200 |

Each call lands in the server's audit log with a request id, target, and
latency.

## Guardrails

[`profile.yml`](profile.yml) exposes six modules: `ping`, `setup`,
`service_facts`, `service`, `command`, and `uri`. There is no shell and no file
write. Narrowing the profile is how you decide what Assistant is allowed to do.

`ROCANNON_APPROVAL` needs an MCP client that supports elicitation. If the client
cannot ask a human, gated calls are refused, so check that before relying on it
with Assistant.

## Teardown

```bash
docker rm -f rocannon-web01 rocannon-tunnel
```
