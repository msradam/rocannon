# Rocannon Demo Scenarios

Real-world natural language queries that drive Ansible automation via MCP tools.

## Linux Scenarios

Tested live against podman containers (Alpine, Ubuntu, Rocky Linux).

### 1. System Health Check

> "Check disk usage, memory, and uptime on all the podman hosts."

**What happens:** Model calls `ansible.builtin.shell` with `target: podman` (the group),
runs a combined health check command. Returns per-host results in a single tool call.

**Modules:** `setup`, `shell`, `command`

---

### 2. Investigate Why a Service is Down

> "The nginx service on podman-ubuntu seems down. Check its status, grab recent logs, and restart it if needed."

**What happens:** Model chains multiple tool calls:
1. `shell` to check `ps aux | grep nginx` or `systemctl status nginx`
2. `shell` to grab `journalctl -u nginx -n 50` or relevant log file
3. `systemd` or `service` to restart if down
4. `shell` or `uri` to verify it's responding

**Modules:** `shell`, `command`, `service`, `systemd`

---

### 3. Deploy a Configuration File

> "Push this config to podman-alpine: server.port=8080 and server.host=0.0.0.0. Put it at /etc/app.conf and verify it's there."

**What happens:** Model uses:
1. `copy` with `content` parameter to write the file
2. `stat` to verify it exists with correct size
3. `slurp` to read it back and confirm contents

**Modules:** `copy`, `stat`, `slurp`

---

### 4. Find What's Eating Disk Space

> "Disk is 92% full on podman-rocky. Find the largest files under /var/log and show me what's consuming space."

**What happens:** Model calls `shell` with `du -sh /var/log/* | sort -rh | head -20`,
then `find /var/log -size +100M` to identify large rotated logs. Presents findings
before taking action.

**Modules:** `shell`, `find`, `command`

---

### 5. Security Audit

> "Run a quick security scan on the podman hosts. Find any world-writable files outside /tmp and list SUID binaries."

**What happens:** Model targets the group and runs:
1. `shell` with `find / -xdev -perm -0002 -not -path '/tmp/*' -type f`
2. `shell` with `find /usr -perm -4000 -type f` for SUID binaries
3. Compares results across hosts and flags anomalies

**Modules:** `shell`, `find`, `stat`

---

### 6. Multi-Host OS Identification

> "What operating systems are running on all the podman machines?"

**What happens:** Model calls `setup` with `target: podman` (group), or `shell`
with `cat /etc/os-release`. Returns all hosts' OS info in a single response.

**Modules:** `setup`, `shell`, `command`

---

### 7. Create a User Account

> "Create user 'deploy' on all podman hosts with sudo access and set up SSH key auth."

**What happens:** Model chains:
1. `user` to create the account with group memberships
2. `file` to ensure `~deploy/.ssh` exists with mode 0700
3. `copy` to deploy authorized_keys
4. `command` to verify with `id deploy`

**Modules:** `user`, `file`, `copy`, `command`


## z/OS Scenarios

Schema-validated against ibm.ibm_zos_core collection. These require z/OS LPAR connectivity.

### 1. Batch Job Health Check

> "Check if the PAYROLL batch job ran successfully last night. Show me the output if it failed."

**What happens:** Model chains:
1. `zos_job_query` with `job_name: "PAYROLL*"` to find the job and its return code
2. If RC > 0 or ABEND, `zos_job_output` with the `job_id` to retrieve spool output
3. Interprets the return code and spool content to explain what went wrong

**Modules:** `zos_job_query`, `zos_job_output`

**Replaces:** Logging into SDSF, filtering by jobname, scrolling through spool files.

---

### 2. Dataset Discovery and Space Analysis

> "What datasets are allocated to IBMUSER and which ones are getting close to full?"

**What happens:**
1. `zos_find` with `patterns: ["IBMUSER.**"]` to enumerate all datasets
2. Model analyzes space utilization from the results
3. Summarizes: lists datasets, flags any above 80% utilized

**Modules:** `zos_find`, `zos_data_set`

**Replaces:** Navigating ISPF 3.4, typing HLQ patterns, option-S on each dataset.

---

### 3. COBOL Compile and Link

> "Copy the COBOL source from /u/developer/payroll.cbl to IBMUSER.COBOL.SOURCE(PAYROLL) and compile it."

**What happens:**
1. `zos_data_set` ensures the target PDS exists (FB/80, idempotent)
2. `zos_copy` copies USS file to PDS member with encoding conversion (UTF-8 to IBM-1047)
3. `zos_job_submit` submits compile/link JCL
4. If RC > 4, `zos_job_output` retrieves the SYSPRINT listing

**Modules:** `zos_data_set`, `zos_copy`, `zos_job_submit`, `zos_job_output`

**Replaces:** ISPF edit, upload, JCL editing, submit, SDSF -- collapsed to one request.

---

### 4. System Health Check

> "Check the health of the z/OS system -- IPL info, active address spaces, and any outstanding operator messages."

**What happens:**
1. `zos_gather_facts` collects system facts (IPL volume, sysplex name, LPAR, OS version)
2. `zos_operator` issues `D A,ALL` to display active address spaces
3. `zos_operator_action_query` checks for outstanding WTOR messages

Model synthesizes: "System SYSA in sysplex PLEX1 was IPLed on March 10. 47 active address
spaces. 2 outstanding operator messages requiring action."

**Modules:** `zos_gather_facts`, `zos_operator`, `zos_operator_action_query`

**Replaces:** Issuing multiple console commands and mentally correlating the output.

---

### 5. Backup Critical Datasets

> "Back up all the production DB2 datasets before tonight's maintenance window."

**What happens:**
1. `zos_find` with `patterns: ["DB2PROD.**"]` to identify all DB2 datasets
2. `zos_backup_restore` creates compressed backup using ADRDSSU

Model confirms: "Backed up 23 datasets to IBMUSER.DB2.BACKUP.D260314 (compressed)."

**Modules:** `zos_find`, `zos_backup_restore`

**Replaces:** Writing ADRDSSU JCL, calculating space, submitting, verifying completion.

---

### 6. User Account Audit (RACF)

> "List all TSO user IDs and show me which ones haven't logged in within the last 30 days."

**What happens:**
1. `zos_tso_command` with `SEARCH CLASS(USER)` to list all RACF-defined user IDs
2. `zos_tso_command` with `LU userid` for each to get last-logon dates
3. Model parses RACF output and presents a table with stale accounts flagged

**Modules:** `zos_tso_command`

**Replaces:** RACF admin skills and manual record-keeping. Useful for compliance reviews.

---

### 7. Submit a File Transfer Job

> "Submit a file transfer job to send dataset PROD.DAILY.EXTRACT to the remote FTP server at 10.0.1.50."

**What happens:**
1. Model generates FTP batch JCL inline with the user's parameters
2. `zos_copy` or `zos_job_submit` with `location: local` to submit the JCL
3. `zos_job_query` monitors job completion
4. If failure, `zos_job_output` diagnoses the FTP step

**Modules:** `zos_copy`, `zos_job_submit`, `zos_job_query`, `zos_job_output`

**Replaces:** Remembering FTP JCL syntax, IKJEFT01 SYSTSIN cards, NETRC conventions.


## Key Insight

The value of natural-language-to-Ansible is strongest when the model can:
- **Chain multiple modules** across steps
- **Inspect intermediate results** before deciding next action
- **Target groups** for multi-host operations in a single call
- **Branch on outcomes** (e.g., "only restart if validation passed")

This conditional reasoning across steps is what differentiates Rocannon from running a static playbook.
