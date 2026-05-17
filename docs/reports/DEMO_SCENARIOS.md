# Rocannon Demo Scenarios

Five end-to-end demo scenarios captured as full multi-turn agent transcripts: natural-language operator prompts → IBM Granite 4 (3B, local Ollama) → Rocannon-generated MCP tools → real ansible-runner execution → real container fleet → agent's final answer.

**Setup:**
- Model: `granite4:3b-32k` running locally in Ollama (no cloud)
- Rocannon profile: 18 modules across `ansible.builtin`, `ansible.posix`, `containers.podman`
- Targets: Ubuntu 24.04, RHEL 9, SLES 15 SP6 (Podman containers); local Podman for the container scenario
- Inventory: 5 hosts, 2 groups, Literal[...] target enum exposed at the MCP boundary

**Per-scenario transcripts:** `scratch/results/demo_transcripts/<id>.md`, those files are the verbatim demo recordings, suitable to put on slides or screenshare. This top-level document is the demo narrative + scenario summaries + suggested flow.

---

## Quality summary

| # | Scenario | Status | Demo recommendation |
|---|---|---|---|
| 1 | Incident triage on rhel | **Strong** | Lead with this, agent does 4-step investigation, hits a Pydantic error, recovers, finds the planted "too many open files" log entries, diagnoses correctly, suggests fix |
| 2 | Fleet drift detection | **Strong** | Use as the second demo, clean 3-host fact comparison, model correctly identifies Debian/RedHat/Suse families and per-OS package managers |
| 3 | Compliance hardening | **Mixed** | Either skip OR include as the "watch the agent recover from an arg-shape error" beat, sysctl 3/3 succeeded, iptables hit a model param-drop. Agent's final summary is honest about the failure |
| 4 | Container stack deployment | **Strong** | Use as the third demo, 6 sequential calls, postgres + redis pod stand-up works end-to-end |
| 5 | Package version audit | **Weak with 3B** | Either skip OR use as the explicit "this is why we'd run this on Granite-3-3-8b in watsonx" lead-in, the 3B model misattributed results across hosts and miscompared 3.1.4 < 3.0.7 |

**The strong/weak split is itself a productization narrative**: a 3B local model handles diagnostics + drift + container ops well; for higher-stakes reasoning (semver comparison, parallel multi-host correlation) you upgrade to granite-3-3-8b on watsonx. Same Rocannon, no code change.

---

## Scenario 1, Incident triage

**Operator setup:** "Something's wrong with the rhel host, the database is timing out. Investigate."

**What happens:**
1. Agent pings rhel (reachable ✓)
2. Runs `df -h /tmp` (90 GB free, disk not the issue)
3. Tries `find /var/log`, passes `paths='/var/log'` as a string instead of a list, hits Pydantic error
4. **Recovers**: switches to `command: cmd='grep -i error /var/log/app.log'`
5. Finds three "ERROR [database] connection refused: too many open files" entries
6. Diagnoses correctly: file descriptor limit
7. Recommends `ulimit -n`, `/etc/security/limits.conf`, database `max_open_files`

**Tool calls:** 4
**Elapsed:** 33.7s
**Demo punch:** the recovery from the find error is the moment that lands. "Watch the agent see the error, change strategy, find the issue." This is *exactly* the kind of agent-shaped task a regulated-industry audience cares about.

→ Full transcript: `scratch/results/demo_transcripts/1_incident.md`

---

## Scenario 2, Fleet drift detection

**Operator setup:** "Compare ubuntu, rhel, sles. Tell me what OS they are, kernel version, and what's notably different."

**What happens:**
1. Agent calls `ansible.builtin.setup` on each of the three hosts in parallel
2. Receives three distribution+kernel fact bundles
3. Correctly identifies:
   - **Ubuntu** → OS family Debian, package manager apt, distribution version 24.04
   - **RHEL** → OS family RedHat, package manager dnf, distribution 9.7
   - **SLES** → OS family Suse, package manager zypper, distribution 15.6
4. Notes architecture and kernel similarity (all aarch64), package manager differences

**Tool calls:** 3
**Elapsed:** 25s (warm) / 214s (cold first turn)
**Demo punch:** Three real Linux distros, three correct identifications, in one operator instruction. Lands the "fleet-level operations" thesis.

→ Full transcript: `scratch/results/demo_transcripts/2_drift.md`

---

## Scenario 3, Compliance hardening

**Operator setup:** "Apply this baseline to ubuntu/rhel/sles: enable IPv4 forwarding, drop tcp port 9999. Both, on each."

**What happens (when prompt is fully spelled out):**
1. Agent calls `ansible.posix.sysctl` 3× (ubuntu, rhel, sles), all 3 set `net.ipv4.ip_forward=1`, all return changed=false (already set), idempotent. ✓
2. Agent calls `ansible.builtin.iptables` 3×, model omits `protocol: 'tcp'` on the call, all 3 fail with "unknown option --destination-port"
3. Final summary: "Applied baseline … iptables drop 9999: configuration could not be applied due to an unknown option `--destination-port`. Please verify the correct syntax for your iptables version."

**Tool calls:** 6
**Elapsed:** 29.6s
**Demo angle:** Two paths to use this:
- **Path A (skip):** if the demo audience is hostile to live failures, swap this out.
- **Path B (own the failure):** "Watch, the agent dropped a parameter the module needs. The error is structured, the agent reported it cleanly, and we know exactly what to fix. This is the kind of small-model arg-correctness gap that a larger watsonx-hosted model resolves." That positions the limitation as a productization wedge.

→ Full transcript: `scratch/results/demo_transcripts/3_hardening.md`

---

## Scenario 4, Container stack deployment

**Operator setup:** "Deploy a postgres + redis pod for the app team on pg-host."

**What happens:**
1. Agent pulls `docker.io/library/postgres:16` ✓
2. Agent pulls `docker.io/library/redis:7` ✓
3. Creates podman network `demo-net` ✓
4. Creates podman pod `demo-stack` on `demo-net` ✓
5. Starts container `demo-pg` (postgres:16) in the pod with `POSTGRES_PASSWORD=demo` ✓
6. Starts container `demo-redis` (redis:7) in the pod ✓
7. Reports "All steps have been executed successfully"

**Tool calls:** 6
**Elapsed:** 54.8s
**Demo punch:** Six tool calls, six Ansible modules from `containers.podman`, two real images pulled, a real pod stood up with two real containers, all from one natural-language operator instruction. The actual `podman_container` schema has 164 parameters; agent called it with the 5 it needed for each container. Handles the modern container/k8s use case head-on.

(Minor: in its final summary the agent paraphrased `POSTGRES_PASSWORD=demo` as `=hello`. The actual call was correct; this is a known small-model paraphrasing artifact in the summary stage.)

→ Full transcript: `scratch/results/demo_transcripts/4_stack.md`

---

## Scenario 5, Package version audit

**Operator setup:** "Check openssl version on each of ubuntu, rhel, sles. Flag anything below 3.0.7."

**What happens (with 3B model):**
1. Agent calls `command: openssl version` on each of the three hosts
2. Ubuntu returns rc=2 (openssl not on PATH), RHEL returns 3.5.1, SLES returns 3.1.4
3. Agent's summary **misattributes results** (reports rhel's version as ubuntu's, sles's as rhel's) and **incorrectly flags 3.1.4 as below 3.0.7**

**Tool calls:** 3
**Elapsed:** 21s
**Demo angle:** The execution path was perfect, three commands ran, three correct outputs came back. The reasoning failure is purely on the model side. **This scenario is the explicit motivator for an upgraded model.** The slide is: "same Rocannon, same MCP tools, same operator prompt, run on watsonx-hosted Granite 3.3 8B → correct attribution and correct semver reasoning. The substrate doesn't change; the agent does."

If you don't want to lean into the comparison, skip this scenario.

→ Full transcript: `scratch/results/demo_transcripts/5_audit.md`

---

## Suggested 12-minute live demo flow

| Time | Beat | Scenario | What to say |
|---|---|---|---|
| 0:00 | Hook |, | "Every Ansible module, across 2,500+ Galaxy collections, becomes an MCP tool. No per-module code. Watch a 3B model running on this laptop drive Linux infrastructure across three OSes." |
| 0:30 | Substrate |, | Open MCP inspector, show `ansible.builtin.podman_container` schema with its 164 parameters. "This was generated by Rocannon at startup from `ansible-doc -j`. There is no Rocannon code that knows what podman_container is." |
| 1:30 | Demo 1 | Incident triage | Run scenario 1 live. ~35s. Land the recovery beat: "the agent saw the Pydantic error, switched strategy, found the actual issue." |
| 4:00 | Demo 2 | Fleet drift | Run scenario 2 live. ~25s. "Three real distros. One operator question. Correct fact-by-fact comparison." |
| 5:30 | Demo 3 | Container stack | Run scenario 4 live. ~55s. "Six podman modules. Two images, one network, one pod, two containers. End-to-end deploy." |
| 7:30 | Network angle slide |, | Show `vyos.vyos` schema generation: 28 modules registered, BGP/OSPF/firewall tools all become MCP tools for free. Cite Itential / NetBrain / Gluware as proprietary platforms. "Same pattern, every network vendor's collection." |
| 9:00 | IBM slot-in slide |, | Show `ibm.ibm_zos_core` registered, `zos_data_set`, `zos_job_submit`, `zos_copy`. "Wazi/AAP/watsonx Code Assistant Z slots above this. Same substrate." |
| 10:00 | Honest beat | Hardening or audit | Optional: show the small-model failure mode and frame it as the watsonx upgrade narrative. |
| 11:00 | Close |, | The gap (no other generic ansible-doc → MCP exists per the landscape research), the moat (open source = substrate), the ask. |

## Pre-demo checklist

1. **Pre-warm Ollama**, first cold call took 143s in scenario 1; warm calls were 8-35s. Run `ollama run granite4:3b-32k "ready"` 5 minutes before the demo to keep the model resident.
2. **Pre-pull images**, `postgres:16` and `redis:7` should already be on disk to avoid the demo blocking on container registry latency. The harness's setup function can do this:
   ```
   podman pull docker.io/library/postgres:16
   podman pull docker.io/library/redis:7
   ```
3. **Plant the incident**, scenario 1's setup writes the "too many open files" entries to `/var/log/app.log` on rhel. The harness handles this automatically; if running manually, run the `setup_scenario_1_incident()` function or its bash equivalent before the demo.
4. **Test run**, do all 3-4 scenarios you plan to demo end-to-end the morning of. The 5-minute Apple-Silicon-podman-emulation tax adds up.
5. **Have screenshots as backup**, for any scenario where live failure would be embarrassing, keep the recorded transcript open in a second window.

## Artifacts

- `scratch/demo_scenarios.py`, full harness; run with no args for all 5, or `python scratch/demo_scenarios.py 1_incident` for one
- `scratch/results/demo_transcripts/*.md`, verbatim transcripts for each scenario
- `scratch/logs/demo_*.log`, execution logs
- This document, narrative + flow guidance
