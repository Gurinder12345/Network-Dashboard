# CLAUDE.md

# Network Management Platform

This repository contains a lab/MVP Network Management Platform with a path toward a broader production-grade platform.

Claude Code should treat this file as the primary project guidance when working in this repository.

## 1. Project Objective

Build a secure, modular network management platform that can:

- Monitor network devices
- Display device inventory and health
- Run operational/read-only checks
- Back up device configurations
- Perform guarded configuration changes
- Require approvals before configuration changes
- Perform pre-checks and post-checks
- Track jobs, audit history, approvals, and backups
- Provide a web dashboard
- Run approved Python automation scripts
- Later support rollback, canary deployment, topology, compliance, alerts, RBAC, and broader automation

Keep the MVP simple, reliable, and easy to extend.

## 2. Current Architecture

```text
Browser
   |
   v
Frontend / Dashboard
   |
   v
FastAPI
   |
   +------------------------+
   |                        |
   v                        v
PostgreSQL                Celery
                            |
                            v
                          Redis
                            |
                            v
                    Network Worker
                            |
                    +-------+-------+
                    |               |
                    v               v
                  Nornir          Ansible
                    |               |
                    +-------+-------+
                            |
                            v
                      Network Devices
```

Supporting components:

- Kubernetes: K3s
- GitOps: Argo CD
- Secrets: HashiCorp Vault
- Database: PostgreSQL
- Async jobs: Celery
- Celery broker/result backend: Redis
- Network reads/prechecks/postchecks: Nornir + Netmiko
- Network writes: Ansible
- Ingress: Traefik
- Monitoring later: Prometheus, Grafana, Alertmanager, SNMP Exporter

## 3. Lab Infrastructure

K3s cluster:

- `k8s-ctrl01` — `192.168.137.148`
- `k8s-worker01` — `192.168.137.149`
- `k8s-worker02` — `192.168.137.150`

Main namespaces:

- `argocd`
- `network-platform`
- `monitoring`
- `security`

Repository:

```text
~/Network-Dashboard
```

GitHub repository:

```text
https://github.com/Gurinder12345/Network-Dashboard.git
```

Worker container registry:

```text
ghcr.io/gurinder12345/network-worker
```

Use GitOps for deployment:

```text
edit
-> git add
-> git commit
-> git push
-> Argo CD refresh/sync
```

Do not bypass GitOps for normal application deployment.

## 4. Current Repository Direction

Expected structure:

```text
Network-Dashboard/
├── apps/
│   ├── network-worker/
│   ├── network-api/
│   └── network-ui/
├── kubernetes/
├── argocd/
├── bootstrap/
├── README.md
└── CLAUDE.md
```

Current working application is primarily:

```text
apps/network-worker/
```

The dashboard/API is the current priority.

## 5. Device Inventory

The PostgreSQL device inventory contains 10 devices.

### Dell OS6

- `Kenda-HARO-IDF-A` — `10.0.0.31`
- `Kenda-HARO-SW-01` — `10.0.0.4`
- `Kenda-HARO-SW-02` — `10.0.0.25`
- `Kenda-Access-user-1` — `10.0.0.250`
- `Kenda-Access-user-2` — `10.0.0.251`
- `Kenda-HQ-IDF-A` — `10.0.0.29`
- `Kenda-HQ-IDF-B` — `10.0.0.30`
- `Kenda-HQ-SW-01` — `10.0.0.3`

### Dell OS10

- `Kenda-Core-1` — `10.0.0.10`
- `Kenda-Core-2` — `10.0.0.20`

Main OS6 test device:

```text
Hostname: Kenda-HARO-IDF-A
IP: 10.0.0.31
Model: Dell Networking N3224PX-ON
Platform: dell_os6
Software: 6.8.1.0
```

## 6. Completed Platform Work

### K3s / Argo CD

Completed:

- Three-node K3s cluster
- Argo CD installed and working
- GitOps deployment workflow
- ApplicationSet-based platform deployment
- Network worker deployed through Argo CD

### Redis

Completed:

- Redis deployed in `network-platform`
- Used as Celery broker
- Used as Celery result backend
- Standalone architecture
- Persistent storage configured

Redis caching for dashboard/device data is planned later.

### PostgreSQL

Completed:

- PostgreSQL deployed
- Persistent storage configured
- Application DB/user configured
- Database health checks working

Important tables include:

- `devices`
- `jobs`
- `backups`
- `audit_events`
- `change_approvals`

### Vault

Completed:

- Vault deployed in `security`
- KV v2 configured
- Kubernetes authentication working
- Worker service account integrated
- Per-device credentials stored in Vault
- Worker retrieves credentials dynamically

Never hard-code or print network credentials.

## 7. Network Worker

Worker image:

```text
ghcr.io/gurinder12345/network-worker
```

Current development line has reached approximately:

```text
v0.15.7
```

The worker uses:

- Python
- Celery
- Redis
- PostgreSQL
- Vault
- Nornir
- Netmiko
- Ansible

## 8. OS6 Read Automation

Implemented OS6 read tasks include:

- Show version
- Show interface status
- Show VLANs
- Show IP interfaces
- Show spanning tree
- Running-config backup

The worker dynamically retrieves credentials from Vault.

## 9. OS10 Read Automation

Read-only OS10 tasks are already present for:

- Show version
- Show interface status
- Show VLAN
- Show IP interface
- Show spanning tree
- Show running config

OS10 guarded write/change parity is not complete yet.

For the MVP dashboard:

```text
OS6  -> actionable
OS10 -> view-only
```

## 10. Backup System

Implemented:

- Running-config backup
- Backup files stored under `/backups/<hostname>/<timestamp>.cfg`
- SHA256 checksum
- PostgreSQL backup record
- Job tracking
- Audit events
- Backup success/failure state
- Backup tied to an approval/change

Important optimization completed:

OS6 precheck now reuses one running-config snapshot for verification and backup instead of opening another SSH session just to retrieve the same config again.

### Backup work still remaining

Major remaining work:

- Automatic rollback
- Restore from stored backup
- Partial-apply recovery
- Rollback status/audit workflow
- Restore validation
- Backup retention/cleanup policy
- OS10 backup/change parity

Rollback is intentionally not exposed as functional in the MVP dashboard yet.

## 11. Guarded OS6 Change Workflow

The OS6 guarded change workflow is operational.

```text
Precheck
   |
   v
Capture running-config snapshot
   |
   +--> Verify desired state
   |
   +--> Store backup
   |
   v
Create pending approval
   |
   v
Human approval
   |
   v
Approved -> Applying
   |
   v
Ansible apply
   |
   v
Nornir post-check
   |
   v
Applied or Failed
```

Safety properties:

- Apply uses an approval ID
- Target/config is retrieved from PostgreSQL
- Approval must be in `approved` state
- Backup must belong to the same device
- An approval cannot be replayed after it is applied/failed
- Post-check validates actual device state

## 12. Change Approval Table

`change_approvals` tracks:

- approval ID
- device ID
- backup job ID
- requested by
- approved by
- status
- config lines
- config parents
- timestamps

Typical state flow:

```text
pending -> approved -> applying -> applied
```

or:

```text
pending -> approved -> applying -> failed
```

## 13. Generic OS6 Verification

A command-aware verifier has been implemented.

### VLAN commands

```text
vlan 200
no vlan 200
```

Verification source:

```text
show vlan
```

### Generic running-config commands

Examples:

```text
description NETOPS-AUTOMATION-TEST
shutdown
```

Verification source:

```text
show running-config
```

### Generic negative commands

Examples:

```text
no description
no shutdown
```

The verifier checks whether the positive form is absent.

### Parent/context-aware verification

Example:

```text
Parent:
interface Tw1/0/3

Command:
shutdown
```

The verifier scopes the running config to the parent context.

### Multi-command verification

The verifier can evaluate multiple commands independently under the same parent.

## 14. OS6 Change Tests Completed

### VLAN create/remove

Successfully tested:

```text
vlan 200
```

and:

```text
no vlan 200
```

### Interface shutdown

Successfully tested:

```text
interface Tw1/0/3
 shutdown
```

### Interface no shutdown

Successfully tested:

```text
interface Tw1/0/3
 no shutdown
```

### Multi-command change

Precheck successfully handled:

```text
interface Tw1/0/4
 description NETOPS-MULTI-TEST
 shutdown
```

The description command completed, but `shutdown` took down management connectivity because that interface was the management path. This was a test-design issue and exposed a future guardrail requirement.

Future guardrails should protect:

- management interfaces
- uplinks
- destructive commands on protected ports

Do not use management-connected interfaces for destructive automation tests.

## 15. Dell OS6 SSH Reliability Work

The Dell OS6 Netmiko driver had intermittent prompt/session issues.

Implemented improvements:

- longer OS6 initial prompt timeout
- retry logic for read commands
- retry logic reused for backups
- reduced duplicate SSH reads during precheck
- customized Dell OS6 session preparation behavior

Do not remove this workaround without testing against the N3224PX-ON.

## 16. Ansible Write Path

The original `dellemc.os6.os6_config` path proved unreliable in this environment.

The current write engine uses:

```text
ansible.netcommon.cli_command
```

The playbook:

- enters configuration mode
- enters zero or more parent contexts
- applies approved configuration lines
- exits configuration mode

Important:

`cli_command` may report `changed: false` even when a configuration command was executed.

Therefore, the platform's own precheck/post-check verifier is the source of truth.

Do not use Ansible's `changed` flag as proof that a network change did or did not occur.

## 17. MVP Dashboard Scope

Planned MVP navigation:

```text
Overview
Devices
Changes
Approvals
Jobs
Backups
Audit
Scripts
```

### Overview

Display:

- total devices
- OS6 count
- OS10 count
- healthy/down/unknown devices
- pending approvals
- failed jobs
- recent changes
- recent backups

### Devices

Show all devices.

```text
OS6  -> actionable
OS10 -> view-only
```

### Device detail

Planned tabs:

```text
Overview
Interfaces
VLANs
Changes
Backups
Jobs
```

### Changes

OS6 UI workflow:

```text
Enter parent/context
-> Enter commands
-> Run precheck
-> Display proposed changes
-> Backup result
-> Submit/Review approval
-> Approve
-> Apply
-> Display post-check
```

### Approvals

Show:

- pending approvals
- device
- requested configuration
- requester
- backup reference
- approve/reject controls

### Jobs

Show:

- queued
- running
- success
- failed
- task details

### Backups

For MVP:

- list backups
- show timestamp/checksum/device
- no functional rollback button yet

### Audit

Show important platform events.

## 18. Device Health for MVP

Use a simple health model first:

```text
healthy
degraded
down
unknown
```

Suggested flow:

```text
Celery scheduled health task
-> Nornir
-> lightweight device command
-> success/failure
-> store last_seen/status
-> FastAPI
-> Dashboard
```

Possible stored fields:

- status
- last_seen
- last_check
- response time
- last error

Do not build complex health scoring for the first MVP.

Later health may include:

- SNMP
- interface state
- CPU/memory
- latency
- packet loss
- routing adjacency state
- Prometheus alerts
- config drift

## 19. Scripts Tab

The MVP may include a simple Scripts page.

Do NOT create an arbitrary Python execution textbox.

Initial model:

```text
approved scripts stored in Git
-> user selects script
-> selects target
-> enters approved parameters
-> FastAPI submits Celery task
-> worker executes script
-> output/history displayed
```

Suggested future directory:

```text
apps/network-worker/scripts/
├── interface_audit.py
├── vlan_audit.py
├── config_validation.py
└── backup_check.py
```

Later:

- scheduling
- RBAC
- script approval
- sandboxing
- version management

## 20. Planned FastAPI Service

Create:

```text
apps/network-api/
```

Suggested structure:

```text
apps/network-api/
├── app/
│   ├── main.py
│   ├── routers/
│   ├── services/
│   ├── schemas/
│   └── db/
├── requirements.txt
└── Dockerfile
```

Initial endpoints:

```text
GET /health
GET /api/v1/devices
GET /api/v1/devices/{hostname}
GET /api/v1/jobs
GET /api/v1/approvals
GET /api/v1/backups
GET /api/v1/audit
```

Then add OS6 change endpoints for:

- precheck
- approval
- apply

Frontend must never connect directly to:

- network devices
- Vault
- PostgreSQL
- Redis
- Celery

All frontend operations go through FastAPI.

## 21. Planned Frontend

Create:

```text
apps/network-ui/
```

Preferred visual direction:

- dark navy / charcoal
- compact cards
- clear tables
- status badges
- restrained accent colors
- responsive layout
- no excessive animations
- no generic AI-generated marketing appearance

The dashboard should feel like enterprise network-management software.

## 22. Development Workflow

The project is edited through VS Code Remote SSH on:

```text
K8S-CTRL01
```

Repository path:

```text
/home/ubuntu/Network-Dashboard
```

Claude Code may be used from VS Code, but all modifications should remain reviewable through Git.

Before modifying files:

```bash
git status
```

After modifying files:

```bash
git diff
```

Before committing Python changes:

```bash
python3 -m py_compile <changed-python-files>
```

Then:

```bash
git add <specific-files>
git commit -m "<clear description>"
git push origin main
```

## 23. Kubernetes / GitOps Rules

Preferred workflow:

```text
edit manifest
-> commit
-> push
-> Argo CD sync
```

Use direct `kubectl` primarily for:

- inspection
- logs
- debugging
- temporary port-forwarding
- controlled testing

## 24. Security Rules

Claude Code must follow these rules:

1. Never hard-code passwords, Vault tokens, SSH credentials, API keys, or secrets.
2. Never print secrets to logs or stdout.
3. Use Vault for device credentials.
4. Do not commit `.env` files containing secrets.
5. Do not expose Redis/PostgreSQL/Vault directly to the frontend.
6. Do not allow arbitrary Python execution from the dashboard.
7. Network write operations must remain approval-gated.
8. Prefer read-only operations when testing.
9. Do not test destructive commands against management/uplink interfaces.
10. Keep backup/audit/job records for configuration changes.
11. Validate inputs before sending configuration commands.
12. Do not bypass existing approval state checks.

## 25. Important Design Principles

### PostgreSQL is the durable source of truth

Use PostgreSQL for:

- devices
- jobs
- approvals
- backups
- audit records
- durable platform state

### Redis is not the durable source of truth

Use Redis for:

- Celery broker
- Celery results
- later caching

### Nornir vs Ansible

Use:

```text
Nornir:
- reads
- discovery
- prechecks
- post-checks
- operational data

Ansible:
- controlled configuration writes
```

### GitOps

Use Git + Argo CD for deployment state.

### API isolation

Frontend talks to FastAPI only.

## 26. Remaining Major Work

After MVP dashboard:

1. OS6 automatic rollback
2. Partial-apply recovery
3. Protected management/uplink interface guardrails
4. OS10 guarded writes
5. OS10 verification parity
6. OS10 rollback
7. Redis cache-aside layer
8. RBAC
9. Monitoring integration
10. Alerts
11. Topology
12. Canary/batch deployment
13. Compliance/config drift
14. Risk scoring
15. Change impact analysis
16. Maintenance mode
17. Broader production hardening

## 27. Current Priority

Current priority:

```text
MVP Dashboard
```

Recommended order:

```text
1. FastAPI skeleton
2. PostgreSQL connection
3. Devices endpoint
4. Jobs endpoint
5. Approvals endpoint
6. Backups endpoint
7. Audit endpoint
8. OS6 change APIs
9. Frontend shell
10. Overview page
11. Devices page
12. Approvals page
13. Jobs page
14. Backups page
15. Change workflow
16. Simple Scripts page
17. Containerize
18. Kubernetes manifests
19. Argo CD deployment
20. Traefik exposure
```

## 28. Claude Code Working Style

When working on this project:

- Make small, reviewable changes.
- Do not rewrite working components unnecessarily.
- Preserve existing database/audit/approval behavior.
- Explain major architectural changes before implementing them.
- Prefer extending existing modules over duplicating logic.
- Validate syntax before builds.
- Test read-only paths before write paths.
- Do not assume a network command is safe merely because it is syntactically valid.
- Do not claim a change succeeded until post-check/device state confirms it.
- Keep OS6 and OS10 platform-specific behavior separated where necessary.
- Build the MVP first; broader platform features come afterward.

## 29. Definition of MVP Success

The MVP is successful when a user can:

1. Open the dashboard.
2. See all network devices.
3. See basic device health.
4. Open a device.
5. Review jobs/backups/history.
6. Submit an OS6 configuration change.
7. Run a precheck.
8. See proposed changes.
9. Create/review an approval.
10. Approve and apply the change.
11. See post-check status.
12. View backups and audit history.
13. Run a small set of approved automation scripts.

Rollback and full OS10 writes do not need to be complete for the first MVP.
