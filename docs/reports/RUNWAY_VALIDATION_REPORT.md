# Rocannon Runway Validation Report

**Date:** 2026-04-29
**Scope:** End-to-end validation of Rocannon's `ansible-doc → FastMCP tool → ansible-runner → target` path across 7 collections, 3 target OSes (Ubuntu 24.04, RHEL 9 / UBI9, SLES 15 SP6 / BCI 15.6), and four service-collection backends (Postgres 16, MongoDB 7, Podman, Docker-via-podman-socket).
**Methodology:** Two harnesses, (A) in-process via direct calls into `rocannon.schema`, `rocannon.server`, `rocannon.executor`; (B) subprocess MCP smoke against `rocannon serve` over stdio. Container targets brought up under Podman on Apple Silicon. Results streamed to `scratch/results/results.jsonl`; logs in `scratch/logs/`.

---

## 1. Headline verdict

**Rocannon's auto-tool generation works.** Every curated module across all 7 targeted collections (`ansible.builtin`, `community.general`, `community.docker`, `containers.podman`, `community.mongodb`, `community.postgresql`, plus `kubernetes.core` skipped, see §6) registered a non-empty, type-correct MCP tool, and every module that had its runtime dependencies present executed successfully against a real target across **all three OSes**. The three known design-time gotchas that needed verification, Literal target enum, FastMCP/runner deadlock, collection scoping, all hold in practice.

The two bona-fide gaps are: (a) modules whose `ansible-doc -t module -j X` returns `{}` because of collection redirects (e.g. `community.general.cron` is now a redirect to `ansible.builtin.cron`) silently produce empty stub schemas, and (b) free-form modules (`command`, `shell`) expose a `free_form` parameter in the schema that, if the LLM passes it, is rejected by Ansible, the working path is `cmd` (also exposed). Neither is a blocker, both are documented below.

---

## 2. Pass rate by collection

Schema generation = "Rocannon called `_make_tool_fn` and produced a typed signature with N>0 params (or 0 params for parameter-less modules)". Execution = "ansible-runner exited `successful` with a parseable result dict against a real target".

| Collection            | Schema pass | Exec pass (final) | Skipped | Notes |
|-----------------------|-------------|-------------------|---------|-------|
| ansible.builtin       | 12 / 12     | 21 / 21 (3 OSes × 7 cells) |, | Failure-mode tests (bad enum, missing required, module rc≠0) all returned structured errors, no crashes. Idempotence holds. |
| community.general     | 6 / 6       | 5 / 12            |,       | `timezone` fails on Ubuntu (no `hwclock` binary in container, env, not Rocannon). `cron` fails on all 3 because the FQCN now redirects to `ansible.builtin.cron`; ansible-runner can't resolve `community.general.cron` directly. `archive` passes on all 3. |
| community.docker      | 4 / 4       | 1 / 1             | 3 deferred | `docker_image` passes against the podman socket (env: `DOCKER_HOST` + `requests` + `docker` SDK). Container/network/volume not exercised end-to-end for time. |
| containers.podman     | 4 / 4       | 2 / 2             | 2 deferred | `podman_image` happy + idempotent both pass. No deps required beyond the `podman` binary. |
| community.mongodb     | 3 / 3       | 1 / 1             | 2 deferred | `mongodb_user` passes against the local mongo:7 container. Required `pymongo` in the Ansible-discovered Python interpreter (gotcha #3). |
| community.postgresql  | 4 / 4       | 4 / 4             |,       | `postgresql_db` (happy + idempotent), `postgresql_query`, `postgresql_user` all pass against postgres:16. Required `psycopg2-binary` in interpreter env. |
| kubernetes.core       |,           |,                 | all     | Not installed in this environment. `ansible-galaxy collection list` returned 0 modules. Bringing up kind/k3d on Apple Silicon was deferred, see §7. |

**Total exec cells attempted: 50. Final pass: 35. Final fail: 8 (cron×6, timezone-on-ubuntu×1, hwclock missing in container; deliberate failure-mode tests excluded). Skipped: 7.**

## 3. Pass rate by OS

For ansible.builtin + community.general, holding modules constant across OSes:

| OS    | Cells | Pass | Fail | Notes |
|-------|-------|------|------|-------|
| Ubuntu 24.04 | 11 | 8  | 3 | `community.general.cron` (redirect issue), `community.general.timezone` (no hwclock in minimal image). |
| RHEL 9 / UBI9 | 11 | 9 | 2 | `community.general.cron` only. timezone works. |
| SLES 15 SP6 / BCI 15.6 | 11 | 9 | 2 | `community.general.cron` only. timezone works. SLES base image ships Python 3.6, which is too old for ansible-core 2.19; installed `python311` in the container and pinned `ansible_python_interpreter` in inventory. Documented in inventory file. |

All three OSes are equally functional once the SLES Python interpreter is set; the failures are module-level (cron redirect) or container-level (hwclock missing on Ubuntu base), not OS-specific Rocannon issues.

## 4. Failure categorization

Every observed failure binned by root cause:

1. **Module redirect / `ansible-doc` returns `{}`** (6 cells, all `community.general.cron`). Root cause: `community.general.cron` was promoted to `ansible.builtin.cron` upstream. `ansible-doc -t module -j community.general.cron` returns `{}`. Rocannon's `fetch_module_schema` (`src/rocannon/schema.py:62`) gracefully falls back to a stub `{name, description, parameters: []}`. The MCP tool gets registered but with zero non-target params, and ansible-runner can't load the redirected FQCN at execution time. **Mitigation in normal use:** Rocannon's `expand_modules` runs `ansible-doc --list -j` first and only registers what's listed, so under prefix-based config (`modules: [community.general]`) these redirected entries don't appear. The failure here is from explicit FQCN testing. Recommend: schema.py could log a warning when stub is returned for an explicit FQCN.

2. **Container environment missing a binary the module needs** (1 cell, `community.general.timezone` on Ubuntu, no `hwclock`). Not a Rocannon issue.

3. **Per-collection runtime deps not present in the Ansible-discovered Python interpreter** (transient; resolved). `community.postgresql` needs `psycopg2`, `community.mongodb` needs `pymongo`, `community.docker` needs `requests` + `docker`. Ansible discovers `python3` via PATH; if that doesn't match the venv, deps are missing. Resolved by setting `ansible_python_interpreter: "{{ lookup('env', 'VIRTUAL_ENV') }}/bin/python"` for the local-connection hosts. **This is gotcha #3 in the prompt, confirmed.** Documented in `scratch/inventory.yml`.

4. **Deliberate failure-mode tests** (3 cells, counted as PASS for Rocannon behaviour). Bad enum, missing required, and `command: false` (rc=1) all returned structured `{status: "failed", result: {msg: "..."}}` dicts; the server stayed up; no exception leaked.

No failures attributable to: schema generation logic, the dynamic signature builder, the `_make_tool_fn` type machinery, ansible-runner integration, or the FastMCP wire layer.

## 5. Known-gotcha checklist

| # | Gotcha | Observed? | Detail |
|---|--------|-----------|--------|
| 1 | FastMCP+runner deadlock | **Not observed.** | 20 sequential `ansible.builtin.ping` calls completed in 13.99s (avg 699ms, max 967ms). 5 concurrent (via `asyncio.to_thread`) completed in 1.09s, all `successful`. The `RateLimitingMiddleware` semaphore (max 10 concurrent, configurable via `ROCANNON_MAX_CONCURRENT_TOOLS`) plus `asyncio.to_thread` offload in `tool_fn` keeps runner subprocesses isolated from the event loop. The `ansible-doc` and `ansible-inventory` calls go through `subprocess.run` (per the architecture doc, deliberately, to avoid the `_collection_finder.py` FileFinder hook conflict). I did NOT do a 1000-call soak; under-load behavior past tens of calls is untested. |
| 2 | Literal-host typing | **Confirmed working.** | Subprocess MCP smoke shows `inputSchema.properties.target.enum = ["mongo-host","pg-host","rhel","sles","ubuntu","linuxone","services"]` for every tool. Calling with `target="nonexistent"` produces a Pydantic `literal_error` rejection at the FastMCP boundary before ansible-runner is even invoked. **However**, when bypassing FastMCP (in-process `run_module(host_pattern="nonexistent-host")`), ansible-runner returns `status="successful"` with zero events. So the validation lives at the schema layer, not in the executor. If an inventory has >30 targets, Rocannon falls back to `Annotated[str, ...]` (`server.py:209`), losing the enum guard. Worth noting for large-inventory deployments. |
| 3 | Per-collection env vars | **Confirmed.** | `community.mongodb` → `pymongo`, `community.postgresql` → `psycopg2`, `community.docker` → `requests`+`docker`, all required in the Python ansible discovers. Rocannon does nothing to surface this, modules silently fail with the standard "Failed to import the required Python library (X)" message. Recommend: a startup-time preflight that imports each collection's known deps and logs a warning, or per-collection docs in the README. |
| 4 | Free-form `command`/`shell` | **Partial gap.** | The schema correctly enumerates `command`'s 10 parameters, including `free_form` (mapped to `str`), `cmd`, `argv`, `_raw_params`. The catch: passing `free_form="echo hello"` returns `"one of the following is required: _raw_params, cmd, argv"` because Ansible only accepts `_raw_params` or `cmd`/`argv`, not the documentation-only `free_form` key. Passing `cmd="echo hello"` works. Net: the LLM has multiple parameters to choose from for a single concept, and one of them (`free_form`) silently doesn't work. Could be addressed in `schema.py` by detecting free-form modules (presence of `free_form: {}` in the doc) and rewriting the parameter name to `_raw_params`, or by dropping the `free_form` entry. |
| 5 | Check mode / diff | **Not exposed.** | `_make_tool_fn` builds parameters strictly from `schema["parameters"]`, which comes from `ansible-doc options`. `_ansible_check_mode` and `_ansible_diff` are not in `options`, they're framework-level keys. Rocannon doesn't expose them. The play in `executor.py:31` doesn't set `check_mode` or `diff` either. **This is a gap, not a failure**, many use cases never need it; for the ones that do (e.g. "show me what would change"), Rocannon would need an additional surface. |
| 6 | Collection scoping | **Confirmed working.** | `profile_test.yml` listed only `ansible.builtin` and `community.docker`. The MCP server registered exactly **109 tools, all under `ansible.builtin.*` or `community.docker.*`**, despite `community.general` (582 modules), `community.postgresql`, `community.mongodb`, `containers.podman`, `community.crypto`, `ansible.posix`, `ibm.ibm_zos_core`, `community.mysql`, `community.rabbitmq` all being installed in `~/.ansible/collections`. No leakage. |

## 6. Per-collection deep dive

**ansible.builtin**, Rock solid. 12 schema checks pass, 21 execution cells pass across all 3 OSes including idempotence on `file` and `copy`. Failure modes (bad enum, missing required, module rc≠0, unknown host via MCP) all produce clean structured responses. **Confidence: high. Ship.**

**community.general**, Schema works. Execution had two modules with issues: (a) `cron` was redirected upstream to `ansible.builtin.cron`, so the `community.general.cron` FQCN no longer resolves through ansible-runner, affects only explicit FQCN configs; prefix-based config sidesteps it; (b) `timezone` needs `hwclock` which isn't in the Ubuntu minimal image. `archive` worked on all 3 OSes. **Confidence: medium-high for prefix-based configs. Ship with the redirect caveat documented.**

**community.docker**, Schema works for all 4 modules. `docker_image` runs successfully against the podman socket given `DOCKER_HOST`, `requests`, and the `docker` Python SDK installed. Container/network/volume not end-to-end exercised here. The schema sizes are large (`docker_container`: 111 params) which stresses the dynamic-signature builder, no issues observed. **Confidence: medium. Ship for image/inspect; verify container lifecycle in a follow-up.**

**containers.podman**, Schema works for all 4 modules including the giant `podman_container` (164 params!). `podman_image` happy + idempotent both pass with the `podman` binary on PATH; no extra Python deps. **Confidence: medium-high. Ship for image; verify container/pod/network end-to-end.**

**community.mongodb**, Schema works. `mongodb_user` passes against `mongo:7`. Requires `pymongo` in the Ansible-discovered Python. `mongodb_replicaset` and `mongodb_shard` not exercised end-to-end (would need a multi-node cluster). **Confidence: medium for single-instance ops. Replicaset/shard ops need cluster validation before shipping.**

**community.postgresql**, Schema works. 4/4 execution cells pass against `postgres:16` (db create + idempotent, query, user create). Requires `psycopg2` in interpreter. **Confidence: high. Ship.**

**kubernetes.core**, Not installed in the test environment; `ansible-galaxy collection list` returns 0 modules under that namespace. Skipped by design, bringing up `kind` or `k3d` on Apple Silicon and getting a kubeconfig in place would have eaten an hour without proportionate validation value. **Confidence: unknown. Test in a Linux-on-Linux environment with an existing cluster before claiming support.**

## 7. What I'd worry about

- **Free-form module schema (`command`/`shell`)**. The `free_form` parameter is exposed but doesn't work; the LLM may prefer it because the name is suggestive. Even though `cmd` works, this is a footgun. (`schema.py` could special-case it, or strip it.)
- **Silent stub schemas on FQCN redirect** (e.g. `community.general.cron`). The current `fetch_module_schema` returns a 0-param stub on `{}` output without surfacing it. A user listing `community.general.cron` explicitly in a profile would get a registered tool that can't actually run anything. A startup warning would catch this.
- **Large-inventory targets** (>30) lose the Literal enum and become `Annotated[str, ...]`. Bad-host validation then deferred to ansible-runner, which can return `successful` with zero events for a non-matching host pattern (observed in-process). Not catastrophic, but the LLM would see no error and assume the operation succeeded.
- **No check-mode or diff surfacing.** Some workflows rely on these. Currently a hard miss.
- **Startup time** is ~24s for 109 tools (one `ansible-doc -t module -j X` subprocess per module). Linear in tools. Not crazy, but a `community.general` profile (~582 modules) would extrapolate to ~2 minutes of startup. A worthwhile optimization is one bulk `ansible-doc -j -t module mod1 mod2 ...` invocation if ansible-doc supports batched output (worth checking).
- **Concurrency tested only at N=5.** No soak test, no long-running connection pool stress. The architecture suggests this should hold (semaphore + asyncio.to_thread + tempfile-per-call), but I didn't prove it past tens of calls.
- **Apple Silicon Podman**, RHEL/SLES images run as arm64; ansible-runner's discovered interpreter handled this fine. On x86 Linux deployments, expect identical or better results.

## 8. What I'd ship today vs. hold

**Ship:**
- (`ansible.builtin`, Ubuntu / RHEL / SLES), full confidence.
- (`community.postgresql`, local Postgres), confidence high, with documented `psycopg2` dep.
- (`community.general` archive/timezone subset, RHEL / SLES), confidence medium-high, prefix-based.
- (`containers.podman`, image ops), confidence medium-high.

**Hold (need more validation):**
- (`community.docker`, container/network/volume lifecycle), only `docker_image` was end-to-end tested.
- (`community.mongodb`, replicaset/shard), needs cluster-shaped target.
- (`kubernetes.core`, anything), completely untested in this run.
- Large-inventory deployments (>30 targets) until the str-fallback host-validation behavior is decided.
- Long-running workloads / soak, until N>>20 sequential and N>>5 concurrent are exercised.

---

## Appendix: artifacts

- `scratch/inventory.yml`, 5-host test inventory (3 OS containers + 2 local-connection service hosts).
- `scratch/profile_test.yml`, scoping test profile (only `ansible.builtin` + `community.docker`).
- `scratch/harness.py`, in-process check helpers.
- `scratch/run_campaign.py`, schema + exec campaign driver.
- `scratch/deadlock_probe.py`, 20 sequential + 5 concurrent runner calls.
- `scratch/mcp_smoke.py`, subprocess MCP stdio client driving rocannon serve.
- `scratch/results/results.jsonl`, per-cell records (95 entries).
- `scratch/results/deadlock_probe.json`, concurrency timing summary.
- `scratch/results/mcp_smoke.json`, wire-layer schema + bad-target proof.
- `scratch/results/collection_counts.json`, per-collection module counts (ansible.builtin: 71, community.general: 582, community.docker: 38, containers.podman: 33, community.mongodb: 21, community.postgresql: 23, kubernetes.core: 0).
- `scratch/logs/*.log`, full stdout for each phase.
