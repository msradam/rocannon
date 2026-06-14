# dev/: lab experiments

This directory contains infrastructure and tests that require live access to IBM systems.
It is not part of the shippable Rocannon package.

## Contents

```
dev/
├── inventories/
│   ├── csrt.yml              # IBM CSRT lab z/OS LPARs (cb8a-cb89)
│   ├── vsi.yml               # Wazi as a Service cloud z/OS instance
│   ├── ibmcloud_info.yml     # IBM Cloud credentials (gitignored, never commit)
│   └── host_vars/
│       └── vsi01.yml         # Per-host vars for the Wazi VSI
├── profiles/
│   ├── zos.yml               # CSRT lab: ansible.builtin + ibm.ibm_zos_core
│   ├── zos-demo.yml          # CSRT lab: ibm_zos_core only
│   ├── wazi.yml              # Wazi VSI: ibm_zos_core
│   └── wazi-slim.yml         # Wazi VSI: subset of z/OS modules for quick tests
└── tests/
    ├── conftest.py           # Container + Ollama + WatsonX fixtures
    ├── test_llm.py           # LLM integration tests (LinuxONE + z/OS schema + WatsonX)
    ├── interactive.py        # REPL for manual exploration
    └── containers/
        ├── Containerfile.rhel    # RHEL 10 (UBI10 minimal, s390x)
        ├── Containerfile.sles    # SLES 16 (BCI 16.0)
        └── Containerfile.ubuntu  # Ubuntu Server 24.04
```

## Running the integration tests

```bash
# From the repo root, install dev deps first
uv sync

# LinuxONE container tests (requires podman or docker)
uv run pytest dev/tests/test_llm.py -v -k linuxone

# z/OS schema tests (requires ibm.ibm_zos_core collection + csrt.yml inventory)
uv run pytest dev/tests/test_llm.py -v -k zos

# WatsonX cloud model tests (requires dev/inventories/ibmcloud_info.yml)
uv run pytest dev/tests/test_llm.py -v -k watsonx

# Interactive REPL
uv run python dev/tests/interactive.py
```

## IBM Cloud credentials

`dev/inventories/ibmcloud_info.yml` holds the API key and project ID for WatsonX tests.
It is listed in `.gitignore` and must never be committed.

Format:
```
API_KEY=your-ibm-cloud-api-key
PROJECT_ID=your-watsonx-project-id
```

## z/OS profiles

`dev/profiles/zos.yml` and `dev/profiles/zos-demo.yml` reference `dev/inventories/csrt.yml`.
`dev/profiles/wazi.yml` and `dev/profiles/wazi-slim.yml` reference an external inventory
at the absolute path recorded in the file, update this when working on a new machine.
