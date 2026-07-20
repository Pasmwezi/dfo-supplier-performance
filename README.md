# DFO Supplier Performance Management System

A working full-stack reference implementation for contractor and consultant performance evaluation across construction, A&E contracts, standing offers, supply arrangements, and call-ups.

## Delivered modules

- **Evaluation engine:** Construction GC1.22 and A&E extended/CPERF profiles
- **Construction:** five prescribed 0–20 criteria and automatic outcome logic
- **A&E:** configurable eight-criterion weighted model (weights must total 100%) plus legacy five-criterion 2913-1 profile
- **Performance history:** supplier, contract, SO, call-up, project, region, department, evaluator, date
- **Risk profile:** average, first-to-latest trend, low-score and recurring issue flags
- **Eligibility:** explicit performance-decision records kept separate from automated suspension recommendations
- **Correspondence:** PDF outcome letter generated from evaluation data
- **Dashboards:** executive, workload, warnings/near-threshold/active suspension indicators
- **Reporting:** Excel history and seven PDF report routes
- **Audit/compliance:** immutable field-level audit records, complete snapshots, version history, evidence uploads with SHA-256, and approval workflow
- **Controlled revision:** reviewer and approver returns require actionable comments; authorized evaluators can revise returned records and resubmit them while preserving each prior version and workflow event
- **CPERF project record:** evaluations capture client reference, work description, firm address, project-manager contacts, award and close-out values, contract changes, and construction-specific superintendent/final-certificate information
- **Inline supplier onboarding:** evaluators may add a new firm/contractor/supplier and its contract transactionally while creating the first draft evaluation

## Quick start

Dependencies are installed locally under `.deps` in this build. PostgreSQL 17 is provided through Docker Compose for the local deployment.

```bash
cd /home/clawserver/dfo-supplier-performance
cp .env.example .env
# Replace the example database password, matching URL password, and bootstrap administrator password in .env.
docker compose up -d postgres
./run.sh
```

Open <http://127.0.0.1:8088>. The initial username and temporary password come from `DFO_SPM_DEFAULT_ADMIN_USERNAME` and `DFO_SPM_DEFAULT_ADMIN_PASSWORD`; startup refuses to create the first PostgreSQL administrator if the password is missing or fails policy. The administrator is forced to select a new password before any supplier-performance data or administration page is accessible, and changing it revokes every older session. The development launcher binds to loopback by default; external access must use the production TLS ingress below. The administrator then creates separate evaluator, reviewer, approver and decision-maker accounts under **Administration → User access**; each new account receives a temporary password and must replace it at first sign-in.

Run tests:

```bash
PYTHONPATH=.deps:. .deps/bin/pytest -q
```

For a clean install on a host with Python virtual-environment support:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8088
```

## Docker production deployment

The production stack runs PostgreSQL, one-shot Alembic migration and administrator-bootstrap jobs, the non-root FastAPI service, and Caddy TLS ingress. PostgreSQL and Uvicorn remain on an internal network; only Caddy publishes ports.

1. Point the deployment domain to the host and allow inbound TCP 80/443 and UDP 443.
2. Create the non-secret environment file and edit `APP_DOMAIN` and `CADDY_EMAIL`:

   ```bash
   cp .env.production.example .env.production
   ```

3. Generate local Docker secrets without printing them:

   ```bash
   mkdir -p secrets
   umask 077
   openssl rand -hex 32 > secrets/db_password
   { printf 'Admin!'; openssl rand -base64 24 | tr -d '\n'; printf '\n'; } > secrets/admin_password
   chmod 600 secrets/db_password secrets/admin_password
   ```

4. Build, migrate, bootstrap, and start the stack:

   ```bash
   docker compose --env-file .env.production -f compose.production.yml up -d --build --wait
   docker compose --env-file .env.production -f compose.production.yml ps -a
   ```

   A database containing application tables but no `alembic_version` table is treated as a legacy schema and is **not** stamped automatically. Compare every table, column, index, constraint and type with the initial migration first. Only after an operator records that verification may the baseline be set explicitly with `docker compose --env-file .env.production -f compose.production.yml run --rm migrate alembic stamp 9140020dff13`; then rerun the normal startup command. Never stamp a drifted or partially initialized database.

5. Sign in with the configured bootstrap username and the value in `secrets/admin_password`, then immediately complete the mandatory password change. Do not copy that secret into tickets, chat, logs, or source control.

Health endpoints are `/health` for liveness and `/ready` for database-backed readiness. Review logs with `docker compose --env-file .env.production -f compose.production.yml logs --tail=200 SERVICE`.

### Backup and restore

Backups briefly quiesce Caddy and the application to keep the PostgreSQL dump and attachment archive consistent. Backup and restore share a non-blocking `flock` lock, include SHA-256 checksums, and are excluded from Git. Restore validates both archives, restores into staging, retains the previous database and attachment tree during cutover, and rolls back if the restored application does not become healthy.

```bash
ENV_FILE="$PWD/.env.production" ./deploy/backup.sh
RESTORE_CONFIRM=restore ENV_FILE="$PWD/.env.production" ./deploy/restore.sh backups/dfo-spm-YYYYMMDDTHHMMSSZ
```

Copy encrypted backups off-host and schedule restore drills. Before upgrades, run a backup, pull the reviewed revision, and rerun the production `up -d --build --wait` command; Alembic applies pending schema revisions before traffic reaches the app.

## Data and workflow

- Database: PostgreSQL 17, configured by `DFO_SPM_DATABASE_URL`; local Compose binds PostgreSQL only to `127.0.0.1:5433`
- Attachment store: `data/attachments/`
- Evaluation statuses: `DRAFT → SUBMITTED → REVIEWED → APPROVED`; submitted/reviewed evaluations may be returned
- Only draft and returned evaluations may be edited or receive evidence; submitted, reviewed and approved records are locked
- Actual suspension decisions require a separate `/api/decisions` record with authority, rationale, effective date, expiry date and status
- User accounts, salted scrypt password hashes, role assignments, sessions and access events are retained in PostgreSQL. Raw session tokens are never stored in the database.
- The bootstrap administrator provisions role-specific accounts and can activate/deactivate non-admin users. Deactivation revokes active sessions.
- Contract records declare the applicable performance regime. Incompatible model/regime combinations are rejected.
- On the new-evaluation form, an evaluator may select an existing contract or enter a new supplier and contract. The supplier, contract and draft evaluation are committed as one transaction; duplicate supplier names or contract numbers roll back the entire operation, and the creation is captured in the evaluation audit history.
- Workflow segregation uses stable account identifiers to prevent one account from submitting/reviewing/approving the same evaluation and prevents a decision originator from approving that decision.

### Demonstration role matrix

| Role | Authorized actions |
|---|---|
| `ADMIN` | Supplier and contract master-data creation |
| `EVALUATOR` | Evaluation creation/update, constrained inline supplier/contract creation for a first evaluation, evidence upload, submission, pending decision proposal |
| `REVIEWER` | Independent review/return |
| `APPROVER` | Independent evaluation approval/return |
| `DECISION_MAKER` | Approval of a separately originated performance decision |

For defensible operation, issue separate accounts to separate people. Do not share credentials or combine evaluator, reviewer, approver and decision-maker duties. Production identity claims must identify the employee, organization, role and delegated authority.

## Report endpoints

- `/api/reports/performance-history.xlsx`
- `/api/reports/contractor-performance-history.pdf`
- `/api/reports/consultant-performance-history.pdf`
- `/api/reports/cperf.pdf`
- `/api/reports/supplier-risk.pdf`
- `/api/reports/suspension-eligibility.pdf`
- `/api/reports/procurement-readiness.pdf`
- `/api/reports/trend-analysis.pdf`

## Compliance controls and limitations

| Risk | Level | Control in this build | Production action required |
|---|---|---|---|
| Eight-criterion A&E model differs from GC26/2913-1 five-criterion instrument | **High** | Separate `AE` and `AE_CPERF` profiles; source register documents mapping issue | Obtain DFO legal/policy approval and formally amend contractual instruments before making the extended profile authoritative |
| Recommendation confused with suspension decision | **High** | Automated outcome is only `SUSPENSION_RECOMMENDATION`; pending/approved decisions, notice, representations, legal review and delegated-authority references are separate and audited | Validate the workflow and authority matrix with DFO Legal Services and procurement policy owners |
| Identity and access management | **High** | PostgreSQL-backed accounts, salted scrypt hashes, forced temporary-password replacement, role gates, hashed/revocable sessions, HttpOnly same-site cookie, CSP, access audit and basic segregation of duties | Integrate GC identity/SSO, MFA, centrally managed claims, automated deprovisioning, privileged-access reviews and formal delegated authorities |
| Protected B information | **High** | Authenticated access, no-store responses, security headers, randomized attachment names, 20 MB streaming limit, type/signature checks and SHA-256 evidence metadata | Deploy only to an approved Protected B environment; enable HTTPS and `DFO_SPM_COOKIE_SECURE=true`; encrypt at rest; add malware/CDR scanning; complete SA&A, TRA and privacy review |
| Record retention / disposition | **Medium** | Full evaluation and audit history retained | Configure approved retention schedule, legal holds, disposition controls and Library and Archives requirements |
| Bilingual legal correspondence | **Medium** | English operational templates only | Add mirror-structured French templates, translation QA and language-of-correspondence rules |
| PostgreSQL resilience | **Medium** | PostgreSQL 17 local container with persistent volume and health check | Use a GC-approved managed PostgreSQL service with migrations, encryption, backups, replication, monitoring and disaster recovery |
| Exact PSPC form facsimile | **Medium** | CPERF-compatible data and report register | Obtain permission/current form specification; implement approved fillable-PDF rendering and accessibility validation |

## Production deployment checklist

- Replace the local PostgreSQL container with managed PostgreSQL and introduce reviewed schema migrations.
- Store database credentials and bootstrap secrets in an approved secrets manager; never commit `.env`.
- Terminate TLS at an approved GC ingress and set `DFO_SPM_COOKIE_SECURE=true`.
- Integrate GC SSO/MFA and map authoritative identity claims to the role matrix.
- Add anti-malware/content-disarm scanning before attachments become available to users.
- Configure centralized audit export/SIEM monitoring, alerting, backups, restore tests and disaster recovery.
- Complete Protected B security assessment and authorization, privacy assessment, ATIP/records-retention design, WCAG testing, and bilingual legal review.
- Obtain formal approval for scoring models, correspondence wording, thresholds, adverse-action notice/representation procedures and delegated authorities.

## Source traceability

See [`COMPLIANCE_SOURCES.md`](COMPLIANCE_SOURCES.md) for the extracted GI16, GC1.22, GI23, GC26 and form 2913/2913-1 design basis.

> This is a working reference implementation, not an Authority to Operate and not a final policy instrument. Production use requires security, privacy, accessibility, records-management, bilingual, legal and delegated-authority validation.
