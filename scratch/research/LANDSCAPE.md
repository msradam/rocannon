# Ansible Landscape Analysis for an LLM/MCP Layer

**Date:** 2026-04-29
**Author:** Research pass for Rocannon (IBM internal)
**Purpose:** Identify the highest-leverage Ansible use cases that benefit from an LLM-accessible "every module as an MCP tool" layer, with a shortlist of 5–7 demo candidates that can be exercised against Linux containers.

> Method: live web research via WebSearch / WebFetch on 2026-04-29. Where a claim is not directly cited, it is flagged as inference. The Rocannon source was deliberately not consulted.

---

## 1. Where Ansible actually gets used in 2026

### Posture and reach
- Ansible remains the most widely used configuration management tool per Stack Overflow's 2024 developer survey, and the upstream project sits near 66k GitHub stars with a "vibrant ecosystem on Ansible Galaxy" ([Medium roundup of community signals](https://medium.com/@adeyemijoshua/ansible-the-future-of-configuration-management-1d6c9f3cdf94)).
- Galaxy hosts roughly **12,000 roles and 2,500 collections** as of 2026, per a Red Hat-sourced summary ([cyberpanel.net Ansible Galaxy 2026 guide](https://cyberpanel.net/blog/ansible-galaxy)).
- AAP mindshare in the Configuration Management category is reported at **11.9% in March 2026, down from 18.3% YoY** on PeerSpot — a signal that the broader category is fragmenting (Terraform/Pulumi/Salt/SaltProject siphoning some share), not that Ansible usage is shrinking absolutely ([PeerSpot AAP 2026 reviews](https://www.peerspot.com/products/red-hat-ansible-automation-platform-reviews)).
- **Hard revenue/seat numbers from Red Hat are not public** in the search results. Red Hat does not break out Ansible separately from RHEL/OpenShift in IBM's financial disclosures; a marketing claim of **668% 3-year ROI** appears repeatedly in Red Hat material ([Red Hat IT modernization blog](https://www.redhat.com/en/blog/it-modernization-red-hat-enterprise-linux-and-ansible-automation-platform-building-foundation-ready-future)).

### Verticals where Ansible is concretely deployed
Drawing on Red Hat marketing and case studies ([Red Hat blog: 2025 year in review](https://www.redhat.com/en/blog/2025-red-hat-ansible-automation-platform-year-review), [Red Hat APAC verticals overview via Medium](https://medium.com/@sachin28/how-industries-are-solving-challenges-using-ansible-ec89fad34125)):

| Vertical | Anchor use case | Named examples |
|---|---|---|
| BFSI (banking/insurance) | Drift remediation, audit/compliance, network change windows | Barclays "Project Joy" enterprise automation rollout |
| Telco / Service Providers | Network device config, NFV provisioning, day-2 ops | Generic Red Hat telco vertical, also drives EDA adoption |
| Government / Defence | STIG/CIS hardening, patch cadence | Common Red Hat federal motion |
| Healthcare | Server provisioning, software lifecycle, compliance | "MedTech Solutions" reference |
| Airlines / Transport | Network upgrades, store/branch automation | Southwest Airlines: 30 switches upgraded in 30 min vs a full maintenance window |
| Manufacturing / Pharma | Linux fleet config, edge provisioning | Red Hat APAC reports BFSI/Telco/Manufacturing/Defence/Govt/Pharma as primary AAP verticals |

### Scale anchor points
- AAP is reported as scaling from a handful to thousands of managed nodes, with several PeerSpot reviewers reporting **failures past ~500 nodes per controller** without proper sharding ([PeerSpot](https://www.peerspot.com/products/red-hat-ansible-automation-platform-reviews)). This matters for an MCP demo: the LLM-driven workflow needs to look believable at fleet scale, not just 1–2 hosts.
- Gartner forecasts that **by 2026, 30% of enterprises will automate >50% of network activities (up from <10% in mid-2023)** and **50% of enterprises will use AI to automate day-2 network operations by 2026 (up from <10% in 2023)** ([armadalabs.tech summary](https://training.armadalabs.tech/network-automation-arista-avd/)). These are the strongest tailwinds for an LLM-in-the-loop pitch.

### Team types
Ansible is no longer just sysadmin/DevOps. Per the AAP 2.6 launch, Red Hat is explicitly courting "subject matter experts" via a self-service portal — i.e. NetOps engineers, app teams, security teams who don't write playbooks but want to invoke them ([What's new in AAP 2.6](https://www.redhat.com/en/blog/whats-new-in-ansible-automation-platform-2.6)). That self-service ambition is exactly the gap an MCP layer addresses.

---

## 2. Most-used Ansible Galaxy collections in 2026

> Galaxy stopped surfacing public download counters in the new UI, and search did not return a fetchable leaderboard. The list below is qualitative — derived from which collections appear in Red Hat documentation, certified-content listings, and community roadmaps — not from raw download numbers. **This is a known gap; verify with a `galaxy.ansible.com` API pull before sizing demos.**

### Tier 1 — universal, ship-with-Ansible-package
- `ansible.builtin` — copy, file, template, command, shell, package, service, user, systemd, etc. Every playbook touches this.
- `ansible.posix` — mount, sysctl, firewalld, selinux.
- `community.general` — the dumping ground; ~700 modules across packaging (snap, flatpak, homebrew, pacman), monitoring, identity, and SaaS. Heavy long-tail.
- `community.crypto` — TLS/x509, openssl_*. Indispensable in regulated environments.

### Tier 2 — cloud and platform
- `amazon.aws` + `community.aws` — by far the most-cited cloud collection in Ansible docs and tutorials.
- `azure.azcollection` — large, certified by Microsoft.
- `google.cloud` — smaller but growing.
- `kubernetes.core` (`k8s` module) — deployment/manifest application against k8s clusters.
- `community.docker` and `containers.podman` — direct container lifecycle. **Especially relevant** for the Linux container demo.
- `community.vmware` — still huge in enterprise on-prem despite Broadcom-driven churn.

### Tier 3 — network (see Section 3 for detail)
- `cisco.ios`, `cisco.nxos`, `cisco.iosxr`, `cisco.aci`, `cisco.meraki`
- `arista.eos`
- `juniper.junos`
- `paloaltonetworks.panos`
- `fortinet.fortios`, `fortinet.fortimanager`
- `vyos.vyos`, `frr.frr`

### Tier 4 — security and compliance
- `community.crypto`, `community.hashi_vault`
- `ibm.qradar`, `splunk.es`, `trendmicro.deepsec` (security automation collections featured in AAP security use-case docs — [Red Hat docs](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/implementing_security_automation/aap-security-use-cases))
- `ansible.posix` for SCAP/STIG-style hardening; CIS-CAT often invoked from `ansible.builtin.command`.

### Tier 5 — long-tail nobody else will MCP-wrap
This is the strategic moat for Rocannon. Examples:
- `community.zabbix`, `community.grafana`, `community.mysql`, `community.postgresql`, `community.mongodb`, `community.rabbitmq`
- `dellemc.openmanage`, `dellemc.powerstore`, `purestorage.flasharray`, `netapp.ontap`
- `f5networks.f5_modules`, `check_point.mgmt`, `cyberark.conjur`
- `ovirt.ovirt`, `theforeman.foreman`, `awx.awx`
- IBM-namespace collections (Section 6).

The "everything-becomes-a-tool" pitch is most defensible in Tier 4 and Tier 5: the major hyperscaler and network vendors will eventually ship their own bespoke MCPs, but nobody is going to hand-build an MCP server for `community.zabbix` or `dellemc.powerstore`.

---

## 3. Network automation specifically

### Who uses what at scale
- **cisco.ios / cisco.nxos / cisco.iosxr** — the dominant enterprise install base. Ansible is the de-facto IaC layer for Cisco config push in Fortune 500 NetOps. Southwest Airlines case study above is one public reference ([Red Hat](https://www.redhat.com/en/blog/2025-red-hat-ansible-automation-platform-year-review)).
- **arista.eos** — datacenter / cloud-builder install base; Arista's own AVD framework sits on top of `arista.eos` + Jinja templates ([armadalabs.tech](https://training.armadalabs.tech/network-automation-arista-avd/)).
- **juniper.junos** — service-provider strongholds and enterprise WAN.
- **paloaltonetworks.panos** — security policy automation; collection is vendor-maintained, on Galaxy, requires Ansible 2.16+ and Python 3.10+ ([pan-os-ansible](https://github.com/PaloAltoNetworks/pan-os-ansible)).
- **fortinet.fortios / fortinet.fortimanager** — large mid-market and SMB footprint; common in MSP automation stacks.

### Competitive landscape (network automation platforms)
Per [PyNet Labs top-10 list](https://www.pynetlabs.com/network-automation-companies/) and [Gartner Peer Insights compares](https://www.gartner.com/reviews/market/network-automation-platforms/compare/itential-vs-netbrain):

| Vendor | Positioning in 2026 | LLM/AI angle |
|---|---|---|
| **Itential** | "Agentic operations platform" for NetOps; orchestration-first | Explicitly agentic in 2026 messaging |
| **NetBrain** | Operations and incident response; "Co-Pilot Chatbot" using LLM + backend automation ([NetBrain blog](https://www.netbraintech.com/blog/ai-in-network-operations/)) | LLM-driven chat over network state |
| **Gluware** | Configuration drift and intent; **Gluware Titan** announced Nov 2025 as "governed, verifiable agentic AI in enterprise networking" ([PRNewswire 2026 CUBEd award](https://www.prnewswire.com/news-releases/gluware-wins-most-innovative-automation-product-in-siliconangle-medias-2026-tech-innovation-cubed-awards-302695123.html)) | Strongest agentic-AI marketing in the segment |
| **Forward Networks** | Network digital twin, validation-first | AI-assisted query / search |
| **BackBox** | Backups and vulnerability remediation | Less LLM messaging |
| **Cisco NSO** | Heavy YANG-based orchestration | Vendor-locked; weaker LLM story |
| **IP Fabric** | Assurance and inventory | Adding AI assistant |

Gartner is quoted as projecting **60% of network operations personnel will rely on GenAI for Day-2 management by 2026, up from <5% in early 2024** ([NetBrain](https://www.netbraintech.com/blog/ai-in-network-operations/)).

### Implication for Rocannon
- Itential/NetBrain/Gluware are all converging on "chat the network." None of them ship a generic MCP layer; they ship vertical AI features bolted onto their proprietary platforms.
- **Rocannon's wedge is "every Ansible network module is a typed tool that any MCP-capable LLM can call."** That's strictly more open than the vendor offerings, and it directly piggybacks on the install base of `cisco.ios`, `arista.eos`, `juniper.junos`, `paloaltonetworks.panos`.
- For a demo against Linux containers, network automation is awkward (no real switch). However, **`paloaltonetworks.panos`, `cisco.nxos`, and `vyos.vyos` all have container or VM images** that can be stood up locally — VyOS in particular runs as a Linux container and is the natural network demo target. (Inferred — needs confirmation.)

---

## 4. Existing MCP servers for Ansible / infrastructure automation

### What's already shipped
| Project | Scope | Approach | Gap |
|---|---|---|---|
| **`ansible/aap-mcp-server`** (official) | AAP Controller, EDA, Gateway, Galaxy APIs | OpenAPI → MCP tool generation; write ops gated by `ALLOW_WRITE_OPERATIONS` ([repo](https://github.com/ansible/aap-mcp-server)) | Wraps the *control plane*, not modules. Useless without an AAP install. |
| **`ansible-collections/ansible.mcp`** | Three modules: `server_info`, `tools_info`, `run_tool` + a connection plugin | Lets a *playbook* call out to an MCP server | Backwards of what we want — Ansible-as-MCP-client, not modules-as-MCP-tools |
| **`redhat-cop/ansible.mcp_builder`** | Installs MCP servers into Execution Environments | Packaging glue | Not a server itself |
| **`sibilleb/AAP-Enterprise-MCP-Server`** | Third-party AAP + EDA wrapper | Similar to the official, less polished | Same control-plane scope |
| **`bsahane/mcp-ansible`** | "Advanced" MCP exposing inventories, playbooks, roles, project workflows | Bespoke per-feature handlers | Not module-level; has to be hand-extended per workflow |
| **`tarnover/mcp-sysoperator`** | Ansible **and** Terraform; runs playbooks/plans, manages cloud resources | Coarse-grained (playbook execution + cloud provider calls) | No per-module typing |
| **`bjeans/homelab-mcp`** | Hobby-grade: Docker/Podman, Ollama, Pi-hole, Unifi, Ansible inventory | Per-domain handlers | Not enterprise-shaped |
| Ansible VS Code extension MCP docs ([repo](https://github.com/ansible/vscode-ansible/tree/main/docs/mcp)) | IDE-side dev tooling MCP | Authoring-time, not runtime | Different audience |

Sources for the inventory: [Ansible MCP collection](https://github.com/ansible-collections/ansible.mcp), [aap-mcp-server](https://github.com/ansible/aap-mcp-server), [redhat-cop builder](https://github.com/redhat-cop/ansible.mcp_builder), [sibilleb enterprise server](https://github.com/sibilleb/AAP-Enterprise-MCP-Server), [bsahane](https://github.com/bsahane/mcp-ansible), [tarnover sysoperator](https://github.com/tarnover/mcp-sysoperator).

### Gap Rocannon fills
**Nobody is shipping a generic `ansible-doc → MCP tool` layer.** Every existing project is either:
1. Wrapping the AAP REST API (control-plane), or
2. Hand-writing handlers for a small fixed set of Ansible workflows, or
3. Treating Ansible as the *consumer* of MCP rather than the *provider* of tools.

The "every module is a tool, with parameters reflected from `ansible-doc`, runnable on any inventory" pitch appears unoccupied as of April 2026.

---

## 5. Top recurring use case archetypes

Synthesizing across Sections 1–4 and Red Hat's own use-case taxonomy ([Red Hat security and compliance use cases](https://www.ansible.com/use-cases/security-and-compliance), [AAP 2.6 security automation docs](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/implementing_security_automation/aap-security-use-cases), [Red Hat configuration management page](https://www.redhat.com/en/technologies/management/ansible/configuration-management)):

| # | Archetype | Modules touched | LLM leverage | Blast radius (1=safe, 5=scary) |
|---|---|---|---|---|
| 1 | **Linux server provisioning / configuration** | `ansible.builtin.{package,user,service,template,copy,systemd}`, `ansible.posix.{mount,sysctl,selinux,firewalld}` | Medium — well-documented, but LLM helps non-experts | 2 |
| 2 | **Patch management & vulnerability remediation** | `ansible.builtin.{dnf,apt,yum,reboot}`, `community.general.{snap,flatpak}` | High — natural-language CVE → patch translation | 3 |
| 3 | **Compliance / hardening (CIS, STIG, SCAP)** | `ansible.posix`, `community.crypto`, `ansible.builtin.{lineinfile,blockinfile,sysctl}` | High — turn audit findings into playbook calls | 2 |
| 4 | **Drift detection & idempotent remediation** | Anything in check mode | High — LLM mediates "what changed and why" | 1 |
| 5 | **Container / podman lifecycle** | `containers.podman.*`, `community.docker.*` | High — fits the container-only demo perfectly | 1 |
| 6 | **Network device config (NetOps)** | `cisco.ios`, `arista.eos`, `juniper.junos`, `paloaltonetworks.panos`, `vyos.vyos` | Very high — gnarly CLIs, vendor sprawl, exactly LLM-shaped | 4 |
| 7 | **Diagnostics / fact gathering / log fetch** | `ansible.builtin.{setup,gather_facts,fetch,find,slurp}` | Very high — read-only, perfect first call | 1 |
| 8 | **DB administration** | `community.{mysql,postgresql,mongodb}` | Medium-high — chat-driven DBA tasks | 3 |
| 9 | **Cloud provisioning** | `amazon.aws`, `azure.azcollection`, `google.cloud` | Medium — competing with cloud-vendor MCPs | 4 |
| 10 | **z/OS and Power day-2 ops** | `ibm.zos_core`, `ibm.power_ibmi`, `ibm.power_aix` | Very high — IBM-shaped, see Section 6 | 3 |

### Demo-friendliness ranking (against Linux containers)
- **Best for safe, clear demo:** #4 drift, #5 container, #7 diagnostics. All readable, idempotent, low blast radius, run cleanly inside a podman/docker network of disposable Linux containers.
- **Best for "wow" factor:** #2 patching, #3 compliance, #6 network. These are where customers feel pain. Network is the highest pain-to-LLM-fit ratio but hardest to demo on bare Linux containers (you'd need VyOS or `cisco.nxos` containerlab images).
- **Best IBM-internal resonance:** #10 z/OS and Power. Hard to demo against vanilla Linux containers, but IBM ZD&T or a Power VSI alternative may exist.

---

## 6. IBM-shaped Ansible footprint

Per [IBM Z Content Solutions](https://www.ibm.com/support/z-content-solutions/ansible/), [galaxy.ansible.com/ibm](https://galaxy.ansible.com/ibm), [IBM Power Systems certified content](https://www.ansible.com/integrations/infrastructure/ibm-power-systems), and [the v1.16 GA announcement](https://community.ibm.com/community/user/blogs/ketan-kelkar/2026/02/23/ibm-zos-core-1-16-0-ga):

### Certified `ibm.*` collections (non-exhaustive)
| Collection | Purpose | Notes |
|---|---|---|
| `ibm.zos_core` | z/OS data sets, USS files, jobs, encodings | v1.16 GA Feb 2026; v2.0 in dev. Flagship z/OS automation surface. |
| `ibm.zos_ims` | IMS automation on z/OS | |
| `ibm.zos_cics` | CICS region/resource automation | |
| `ibm.zhmc` | Hardware Management Console | LPAR, partition, Z hardware operations |
| `ibm.zos_sysauto` | Z system automation | |
| `ibm.power_ibmi` | IBM i (AS/400 lineage) automation | Maintained at [ibm.github.io/ansible-for-i](https://ibm.github.io/ansible-for-i/) |
| `ibm.power_aix` | AIX system administration | |
| `ibm.power_hmc` | Power HMC | |
| `ibm.power_vios` | VIOS | |
| `ibm.cloudcollection` (`ibm.cloud`) | IBM Cloud resource management | [Galaxy entry](https://galaxy.ansible.com/ibm/cloudcollection) |
| `ibm.storage_virtualize` | FlashSystem / SVC / Storwize | [docs](https://docs.ansible.com/projects/ansible/latest/collections/ibm/storage_virtualize/) |
| `ibm.qradar` | Security automation, QRadar offenses | Featured in AAP security use-case docs |

There are also IBM-published collections under non-`ibm.*` namespaces (e.g. CP4S Cases collection at [IBM/cp4s-ansible-collection](https://github.com/IBM/cp4s-ansible-collection)).

### Why this matters for the IBM internal audience
1. **`ibm.zos_core` is the obvious crown jewel.** A demo that lets a JCL-illiterate user say "submit a job that lists data sets under HLQ.X and tail the SYSPRINT" via natural language → MCP → `ibm.zos_core` modules is a viscerally on-brand IBM Z story. Constraint: hard to run inside a Linux container; needs ZD&T or Wazi sandbox.
2. **`ibm.power_ibmi` and `ibm.power_aix`** are differentiated — competitors aren't building LLM tooling for them at all. Same demo problem (no container substrate) unless a Power VSI or AIX VM is allocated.
3. **`ibm.storage_virtualize` and `ibm.qradar`** are the easiest IBM-resonant demos that *can* be containerized (sim/mock backends, REST APIs).
4. The **Wazi profiles already in the repo** (`profiles/wazi-full.yml`, `profiles/wazi-slim.yml`) suggest the team has already chosen z/OS as a target — so the Linux-container demo is likely the *generic* showcase, with z/OS as a separate track.

---

## Top use case shortlist

Ranked for: clarity of value × LLM-in-the-loop benefit × demo safety on Linux containers.

| Rank | Name | What it is | Who the user is | Modules touched | Blast radius | Why it's a good (or weak) demo |
|---|---|---|---|---|---|---|
| **1** | **Read-only diagnostics & triage** | "Why is service X failing on these 5 hosts?" → LLM gathers facts, fetches logs, inspects systemd, summarizes. | SRE / on-call / L1 support | `ansible.builtin.{setup,service_facts,systemd,command,find,slurp,fetch}`, `community.general.{journald,syslog}` | **1 (read-only)** | **Strongest demo.** Pure observation, no state change, instantly readable, exercises ~20+ modules across hosts. Containers make 5 fake hosts trivial. |
| **2** | **Drift detection & guided remediation** | LLM runs playbooks in `--check` mode, narrates diffs, proposes targeted fixes, applies on approval. | Platform / ops engineer | `ansible.builtin.{template,copy,lineinfile,package,service}`, `ansible.posix.{sysctl,selinux,firewalld}` | **2** | Idempotent by design; check-mode = safe rehearsal. Maps directly onto the "agentic with human approval" UX customers expect. |
| **3** | **Container/Podman lifecycle automation** | Spin up, configure, network, and tear down containerized apps via natural language. | Dev/test, app teams, demo audience itself | `containers.podman.*`, `community.docker.*`, `ansible.builtin.uri` for healthchecks | **1** | Native to the demo substrate. Story: "the LLM is operating the same containers running this demo." Self-referential and clean. |
| **4** | **CIS / STIG hardening + audit narration** | LLM walks a host against a CIS profile, narrates pass/fail, applies remediations one by one with diffs. | SecOps, compliance | `ansible.posix.*`, `ansible.builtin.{lineinfile,sysctl,user,mount}`, `community.crypto` | **2** | Hits the most-cited Red Hat use case (security+compliance). Container-friendly. Risk: CIS profiles are large; pick one section. |
| **5** | **Patch management with CVE-driven prioritization** | "Apply CVE-2026-XXXX fixes across this fleet, staged, with rollback." | SecOps, IT ops | `ansible.builtin.{dnf,apt,reboot,package_facts}`, `community.general.{snap}` | **3** | Highest customer-pain score in survey content. Reboot semantics complicate container demo; use `--check` + simulated reboot. |
| **6** | **Network config change against VyOS / cisco.nxos in containerlab** | "Add ACL X to interfaces matching Y; show the diff before applying." | NetOps engineer | `vyos.vyos.*`, `cisco.nxos.*`, `ansible.netcommon.*` | **4** | Highest LLM-leverage of any category (gnarly CLIs, vendor sprawl), and Itential/NetBrain/Gluware are all targeting this. Stretch demo: requires running VyOS or NX-OS images as containers. |
| **7** | **DB ops: backup, schema migration, user management** | "Rotate the app DB password across these three Postgres replicas; verify; fail closed." | DBA / platform | `community.postgresql.*`, `community.mysql.*`, `community.mongodb.*` | **3** | Crisp, frequently-painful, container-native. Good "second demo" once #1–#3 land. |

### Recommended IBM-internal pick (3 demos)
If forced to choose three for the Linux-container demo:

1. **#1 Read-only diagnostics** — sells the safety story and the "every module is a tool" reflection cleanly.
2. **#3 Container lifecycle** — self-referential, zero infrastructure, demonstrates write-path operations.
3. **#6 Network change against VyOS** *or* **#4 CIS hardening** — pick based on whether the audience leans NetOps (go #6 with VyOS in a container) or SecOps/RHEL (go #4). For an IBM Z Software internal audience, **#4 is the safer bet**; the natural follow-on slide is "and the same pattern works against `ibm.zos_core` in a Wazi sandbox" — which leverages the existing `profiles/wazi-*.yml` work without putting Wazi on the critical path of the live demo.

---

## Open data gaps (worth chasing before the deck is final)
- **Hard Galaxy download counts** by collection — search did not return a public leaderboard. Pull via `https://galaxy.ansible.com/api/v3/...` directly.
- **AAP seat / customer counts** — Red Hat doesn't disclose; closest proxies are mindshare percentages on PeerSpot and IBM Software segment color in earnings.
- **Annual Ansible community survey** — there is a "year-end survey" thread on the Ansible forum but no published 2025/2026 aggregate report was found.
- **Ansible Lightspeed adoption numbers** — Red Hat has not published seat counts; only feature-roadmap milestones (BYOM, Gemini in Dec 2025, OpenAI/Azure OpenAI early 2026, watsonx.ai/Gemini for the assistant in early 2026 — [What's new in AAP 2.6](https://www.redhat.com/en/blog/whats-new-in-ansible-automation-platform-2.6)).
