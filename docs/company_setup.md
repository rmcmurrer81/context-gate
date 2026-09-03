# Self-host ContextGate for your company

ContextGate can be installed inside a company environment and used as a Python decision gate with a company-owned source policy. The core evaluates structured evidence and action requests, returns `ALLOW`, `REVIEW`, or `BLOCK`, and binds the policy version and fingerprint into every decision receipt.

The repository includes a local HTML/CSS/JavaScript command center plus session-only Gmail and Microsoft OAuth connectors for read-only manual or while-open periodic mailbox scans. It does **not** yet include production OCR, an authenticated multi-user service, durable encrypted token storage, a durable scheduler, or a production-ready Confluent deployment. The older Streamlit proof lab remains available as an optional evaluation surface. Production systems should call the decision engine from a controlled service and place external actions behind its result.

## 1. Choose the deployment boundary

What is available now:

- a deterministic Python decision engine with schema-validated models and strict untrusted-boundary examples;
- a fail-closed, company-editable source policy;
- JSON Schemas for context events, action requests, decisions, and reviews;
- a local fixed-screen command center with company settings and evidence-grounded chat;
- read-only Gmail and Microsoft authorization-code OAuth with PKCE, anti-forgery state, multiple in-memory account sessions, and manual or explicitly enabled while-open periodic scans;
- local document intake with bounded extraction and content hashes;
- append-only local review receipts and an integrity-linked audit file;
- an optional one-way Kafka producer; and
- Flink SQL and Streaming Agent artifacts for workshop adaptation.

What a production owner must add:

- authenticated source connectors and tenant isolation;
- a service/API, durable queues, and a production review workflow;
- identity and access management for operators;
- encrypted secret and source-artifact storage;
- production OCR/vision for images and scanned PDFs;
- durable, externally protected decision and audit storage; and
- an actuator that executes only an approved `ALLOW` result.

The important integration rule is:

```text
authenticated source -> ContextEvent -> ContextGate -> ALLOW -> company actuator
                                                |----> REVIEW -> human queue
                                                `----> BLOCK  -> no action
```

ContextGate itself does not execute calendar changes, send email, or modify another system.

## 2. Install locally

Requirements:

- Python 3.11 or newer;
- Git; and
- network access during the first dependency installation.

Clone the repository and enter its directory:

```bash
git clone https://github.com/rmcmurrer81/context-gate.git
cd context-gate
```

### Windows PowerShell

```powershell
Copy-Item .\config\source_policy.example.json .\config\source_policy.json
$env:CONTEXTGATE_POLICY_PATH = (Resolve-Path .\config\source_policy.json).Path
.\run.ps1
```

The launcher creates `.venv`, installs the application dependencies, binds the local command center to `127.0.0.1`, and opens it at [http://127.0.0.1:8501](http://127.0.0.1:8501). It does not expose the service on the local network. The optional `lab` task starts the legacy Streamlit proof surface with Streamlit's email prompt and usage telemetry disabled.

If PowerShell blocks local scripts:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

### macOS or Linux

```bash
cp config/source_policy.example.json config/source_policy.json
export CONTEXTGATE_POLICY_PATH="$(pwd)/config/source_policy.json"
bash run.sh
```

The environment variable must be present in the process that runs ContextGate. For a service, configure it in the service manager or container definition rather than a developer shell profile.

The policy must never contain credentials. For production, either keep the approved policy in controlled version history or place it in a protected configuration directory outside the checkout and point the environment variable there. The default copied path, `config/source_policy.json`, is ignored by Git to prevent accidental commits; use a separately reviewed filename only when the company intentionally versions policy.

The launchers deliberately force `CONTEXTGATE_MODE=local`; they never contact Kafka or another external service.

## 3. Define the company source policy

Edit the copied `config/source_policy.json`. Do not edit the example in place, because upgrades may replace it.

Each source has:

- `rank`: deterministic authority from `0` to `100`;
- `trust_cap`: the highest effective trust that events from this source may receive, from `0.0` to `1.0`; and
- `label`: operator-facing text used in explanations.

The policy also controls:

- `minimum_automatic_authority_rank` and `minimum_automatic_trust`: both must be met before an otherwise safe claim can be automatically allowed;
- `near_peer_max_authority_rank_gap` and `near_peer_max_trust_gap`: how close two disagreeing sources may be before the conflict is routed to review; and
- `unknown`: the required fallback for any source type that is not explicitly configured.

Source names must be lowercase identifiers such as `hr_system`, `approved_vendor_api`, or `employee_upload`. Keep unverified and user-controlled sources below automatic-action thresholds. A high producer-supplied `trust_score` cannot exceed the policy's `trust_cap`.

Automatic `ALLOW` also requires confirmed, public evidence, an exact evidence ID/value match, no stronger or near-peer conflict, current effective time, and a non-consequential action. Changing only a rank does not bypass those checks.

An explicitly configured policy is strict and fail-closed:

- unknown JSON fields, duplicate keys, invalid ranges, or a missing `unknown` source are rejected;
- the file must be UTF-8 JSON, a regular file, no larger than 64 KiB, with no symbolic link in its path; and
- a missing or unreadable configured file stops evaluation instead of silently using defaults.

Validate the active policy after editing it:

```powershell
# Windows
.\.venv\Scripts\python.exe -c "from context_gate.policy_config import get_active_policy; p=get_active_policy(); print(p.policy_version, p.policy_fingerprint)"
```

```bash
# macOS or Linux
.venv/bin/python -c 'from context_gate.policy_config import get_active_policy; p=get_active_policy(); print(p.policy_version, p.policy_fingerprint)'
```

### Policy version and change control

`policy_version` is the company's human-readable release identifier. Change it whenever policy semantics change. ContextGate also calculates a SHA-256 fingerprint from the validated, canonical policy. Both values are bound into `DecisionRecord` and its identity, so two policy revisions cannot silently produce the same decision receipt.

For controlled changes:

1. review the JSON through normal code or policy approval;
2. increment `policy_version`;
3. run representative company fixtures covering `ALLOW`, `REVIEW`, and `BLOCK`;
4. archive the exact validated policy with its fingerprint and approval record;
5. replace the deployed file atomically so a reader cannot observe a partial write; and
6. retain old policies for decision replay and audit investigation.

The process reads and hashes the configured file for each evaluation, with an immutable in-process cache by content. A valid atomic replacement is therefore visible to the next decision without clearing the cache. A service may still restart during a controlled rollout to make the release boundary obvious.

## 4. Integrate the decision engine

The public contracts are in `schemas/`. An ingestion adapter should validate incoming records as `ContextEvent`; an action adapter should validate the proposed change as `ActionRequest` before anything is executed.

A service can call the current library directly:

```python
from context_gate.decision_engine import evaluate_request
from context_gate.models import ActionRequest, ContextEvent, EnforcementDecision
from pydantic import TypeAdapter

events = TypeAdapter(list[ContextEvent]).validate_json(context_json, strict=True)
request = ActionRequest.model_validate_json(action_json, strict=True)
decision = evaluate_request(events, request, run_id=company_run_id)

if decision.decision is EnforcementDecision.ALLOW:
    company_actuator.execute(request, decision)
elif decision.decision is EnforcementDecision.REVIEW:
    review_queue.enqueue(request, decision)
else:
    blocked_record_store.append(request, decision)
```

This is an integration pattern, not a complete service from the repository: `company_actuator`, `review_queue`, authentication, storage, retries, and idempotency belong to the deploying company. Revalidate the decision immediately before a delayed external action so new evidence cannot be bypassed.

Do not let a model or chat response call the actuator. Model output may explain a completed deterministic decision but cannot change its result.

### Add company memory and important-detail semantics

The optional local memory and semantic interpreter are separate from the enforcement decision engine. `MemoryStore` persists bounded `CompanyObservation` rows in a caller-selected SQLite file; `PatternAnalyzer` calculates attributable counts and routes weak or conflicting patterns to an advisory review. `interpret_quantity_update` evaluates a bounded statement against a caller-supplied exact state snapshot and company-defined `CategorySemanticConfig`; it does not fetch or update state.

Copy [`config/semantic_profile.example.json`](../config/semantic_profile.example.json), review its identity keys, vocabulary, and quantity limits, then load it with `CategorySemanticConfig.model_validate_json(..., strict=True)`. The file is not discovered automatically. Keep semantic-profile releases under the same review discipline as source policy, and archive the validated profile fingerprint used for each proposal.

The local app defaults to `runtime/company_memory.sqlite3` and accepts an alternate path through `CONTEXTGATE_MEMORY_PATH`. This SQLite database is plaintext, its tenant ID is not an authentication boundary, and its payload digest is an unkeyed integrity check rather than a signature. No automatic retention/deletion workflow is provided. For production, use authenticated server-side tenant binding, authorization, encryption, durable protected storage, and company-approved retention procedures. See [Company memory and important-detail interpretation](company_memory.md) for the fictional 3/8 address example, Suite 354 anomaly, total-versus-delta rules, contributor traces, and append-only correction behavior.

### Deploy operator learning deliberately

The local app stores Company Memory and operator-learning records in the same local SQLite file selected by `CONTEXTGATE_MEMORY_PATH`; the default is `runtime/company_memory.sqlite3`. `CONTEXTGATE_TENANT_ID` selects the operator-learning namespace and defaults to `local-company`:

```powershell
$env:CONTEXTGATE_MEMORY_PATH = "C:\ContextGate\runtime\company.sqlite3"
$env:CONTEXTGATE_TENANT_ID = "example-company"
.\run.ps1
```

```bash
export CONTEXTGATE_MEMORY_PATH="/var/lib/contextgate/company.sqlite3"
export CONTEXTGATE_TENANT_ID="example-company"
bash run.sh
```

This is explicit-only learning: ordinary evaluations and unchecked chat questions are not learned. An operator must deliberately save company guidance or confirm a decision correction. A correction is bound to the original case and decision ID plus the exact request, evidence, and policy fingerprints, so it cannot silently attach to a different evaluation.

The local Company Memory tenant text field is separate from `CONTEXTGATE_TENANT_ID`; sharing a database file does not merge their row namespaces. For a local test, select the intended tenant consistently. In production, remove caller-selectable tenant identity and bind both features to the same authenticated, authorized server-side tenant.

Guidance, corrections, and retractions are append-only. Retracting a record adds a tombstone and makes it inactive; it does not erase the original or its provenance. Guidance can influence bounded chat advice, and a matching correction can change the resolved dashboard view, but neither can change deterministic enforcement, authorize an actuator, or claim an external action occurred.

Both environment variables are configuration, not access control. The SQLite contents are plaintext unless the host volume supplies encryption, and an environment-supplied tenant ID does not authenticate a user. Before production, bind tenant identity to the signed-in operator server-side, enforce authorization, encrypt the database and backups, restrict filesystem access, and implement approved retention, export, deletion, restoration, and audit procedures. Use one controlled local path per deployment instance; the app rejects database URIs and network-share paths.

## 5. Connect company email safely

The local command center implements authorization-code OAuth with PKCE and anti-forgery state for Gmail, Outlook.com/Hotmail, and Microsoft 365. It requests read-only Gmail or Microsoft Graph mail scopes, supports multiple connected accounts, and scans after an operator chooses **Scan** or explicitly enables **Auto-monitor while open**. The page clears that browser-controlled timer when it closes and pauses future checks after detecting that the local server is unavailable; it is not a production scheduler. An email address alone never authenticates an inbox, and ContextGate never asks for a mailbox password.

Each installation owner must first register a Google Desktop OAuth client and/or Microsoft Entra public client, then configure the public client details through **Sources** or **Settings**. Provider-hosted consent is the only sign-in path. The local connector keeps access and refresh tokens in server memory only, never returns them to the browser, and clears them when the process stops. Do not paste mailbox passwords, client secrets, access tokens, or refresh tokens into chat, policy JSON, source events, logs, reports, or Git. See the exact provider setup steps in [Company quick start](company_quickstart.md).

Before treating this connector as a production mailbox service, a company owner must add:

1. a registered application in the company's Google Cloud or Microsoft Entra tenant;
2. approved redirect URIs, provider verification, PKCE, anti-CSRF state, and provider-recommended token and consent validation;
3. the least-privilege read-only mail permission approved by the tenant administrator;
4. an encrypted secret store for client credentials and per-account refresh tokens, with rotation and revocation;
5. one non-secret `connection_id` per mailbox, bound to the company tenant and owner;
6. allowlisted folders/labels, sender domains, message types, and retention periods;
7. verified webhook delivery or bounded polling with replay protection and idempotent provider message IDs;
8. malware scanning and hashing before attachment extraction; and
9. deletion, export, disconnection, and audit controls for every connected account.

Use delegated consent for individual accounts unless the company has explicitly approved broader access. Domain-wide delegation or application-wide mailbox permissions are high-impact and should not be the default.

A production connector should keep original mail in a private, access-controlled source store and emit only the minimum claim required for a decision. A `ContextEvent` should retain a stable connection ID, provider message ID/reference, received time, attachment digest where applicable, and available sender-authentication results. It should not carry a full inbox, full message body, token, or secret.

SPF, DKIM, DMARC, a display name, or a claimed `source_type` alone does not prove that content is company-authoritative. A trusted adapter must stamp source identity from the authenticated connection and apply a company-managed sender/domain allowlist. Treat all other mail as `unverified` or `unknown`.

Inbound reading and outbound sending should be separate OAuth grants and separate services. ContextGate decisions do not grant permission to send mail.

## 6. Documents, screenshots, and OCR

The local intake currently accepts artifacts up to 10 MiB and extracts at most 20,000 characters:

- UTF-8 text, Markdown, CSV, JSON, HTML, and XML;
- `.eml` subject and visible text, while skipping attachments;
- text-layer PDFs with `pypdf`; and
- `.docx` paragraphs and table cells with `python-docx`.

PNG, JPEG, GIF, WebP, and scanned PDFs return `OCR_REQUIRED`. ContextGate does not pretend to read pixels. Upload bytes are inspected in memory and are not saved by the intake module; the receipt contains a safe filename, size, media type, SHA-256 digest, extraction status, and bounded extracted text when available.

An upload-created `ContextEvent` is always `unverified` and is not automatically published or inserted into the displayed decision. Its claim value is explicitly entered by the operator; extraction does not authenticate the claim or its author.

Before production document intake, add authenticated upload endpoints, authorization, streaming size limits, malware and archive-bomb scanning, encrypted object storage, retention/deletion rules, sandboxed parsers, and a trusted OCR/vision service. Preserve the original artifact digest, extractor/OCR version, page or region reference, and human verification status. Send only the minimum necessary text to any remote model.

## 7. Adapt the Confluent path

Install the optional Kafka client in the deployment environment:

```bash
python -m pip install -e ".[confluent]"
```

The current producer reads these environment variables only when `CONTEXTGATE_MODE=confluent`:

```text
CONTEXTGATE_MODE=confluent
CONFLUENT_BOOTSTRAP_SERVERS=<broker endpoint>
CONFLUENT_API_KEY=<service account key>
CONFLUENT_API_SECRET=<secret supplied by a secret manager>
```

Do not use the local launchers for this path because they intentionally reset the mode to `local`. Run a separately reviewed worker or service entry point with secrets injected by the deployment platform. The included adapter only produces a caller-supplied JSON value to a caller-supplied topic and reports confirmed delivery or timeout; it does not create topics, consume events, configure Schema Registry, manage checkpoints, or run the application workflow.

The numbered SQL files in `flink/` are an adaptation starting point. Before using them outside the workshop:

1. select company-owned catalog/database and approved object names;
2. create service accounts and least-privilege ACLs for each producer, consumer, and Flink statement;
3. choose and test topic formats, compatibility, retention, partition keys, and replay behavior;
4. replace the synthetic source ranking in `flink/10_normalize_events.sql` with the approved company policy;
5. add the policy version and fingerprint to the cloud decision contract;
6. implement and test near-peer, trust-threshold, identity, and failure behavior equivalent to the Python engine;
7. compile the SQL against the deployed Confluent Cloud/Flink version; and
8. run an end-to-end test before enabling any actuator.

`CONTEXTGATE_POLICY_PATH` affects the Python engine only. It does not automatically update the current Flink SQL. Until policy generation or an authoritative policy table keeps both implementations synchronized and equivalence tests pass, choose one enforcement path as authoritative rather than running two policy interpretations.

The Streaming Agent is optional and downstream of `context_decisions`. Keep its output in a separate explanation/log topic and validate it against the deterministic receipt. Model latency, failure, or prose must never delay or override enforcement.

## 8. Production security checklist

- Stamp tenant, connection, and source identity at authenticated ingestion; ignore or overwrite client-supplied authority fields.
- Isolate tenant topics, object storage, keys, audit records, and review queues.
- Use TLS, least-privilege service accounts, secret-manager injection, rotation, and revocation.
- Require timezone-aware timestamps and bind each request to an exact supporting event ID and value.
- Fail closed when policy, provenance, schemas, storage, or connector identity cannot be validated.
- Authenticate the operator UI/API and add authorization, CSRF protection, rate limits, and bounded inputs.
- Separate read connectors, decision workers, reviewers, and write-capable actuators.
- Make actuator calls idempotent and record their provider response separately from the decision.
- Redact logs and prompts; never log passwords, tokens, full private messages, or unnecessary document text.
- Store decisions and reviews durably with uniqueness constraints and externally protected retention.
- Back up policy releases and schemas; test restoration and evidence replay.
- Pin and scan production dependencies and container images. The repository uses bounded version ranges, not a production lockfile.
- Define incident response, account disconnection, data export/deletion, and retention procedures before onboarding users.

The local `runtime/audit.jsonl` chain can detect ordinary edits and identity collisions, but it is not an externally anchored ledger. A filesystem owner can replace or truncate it. Use append-only managed storage, access controls, and external anchoring where audit guarantees matter.

## 9. Validate before deployment

First verify the unchanged repository without a custom policy:

```powershell
# Windows
Remove-Item Env:CONTEXTGATE_POLICY_PATH -ErrorAction SilentlyContinue
.\run.ps1 doctor
.\run.ps1 test -Dev
```

```bash
# macOS or Linux
unset CONTEXTGATE_POLICY_PATH
bash run.sh doctor
bash run.sh test --dev
```

Then restore `CONTEXTGATE_POLICY_PATH`, print its version and fingerprint as shown above, and run company-owned acceptance fixtures. Include at least:

- an authoritative, public, non-consequential match that should `ALLOW`;
- missing provenance that should `REVIEW`;
- a near-peer disagreement that should `REVIEW`;
- a lower-authority conflict and stale request that should `BLOCK`;
- sensitive or consequential actions that should `REVIEW`;
- an unknown source and excessive claimed trust that must not auto-allow; and
- connector retries, duplicate event IDs, future evidence, malformed input, and dependency outages.

With the built-in policy, the doctor expects B1 to produce `CONFLICT / BLOCK / HIGH`. With a custom policy, it verifies that the engine completes and the receipt carries that policy's version and fingerprint, then reports the resulting B1 outcome. This confirms wiring, not business correctness; keep company-owned invariant tests for the outcomes your approved policy is meant to produce.

For a full contributor check:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
python scripts/doctor.py
python scripts/acceptance_matrix.py
python -m compileall -q context_gate app.py scripts
```

## 10. Upgrade and roll back safely

1. Tag or record the deployed code commit, dependency lock/image digest, schemas, policy JSON, policy version, and policy fingerprint.
2. Back up durable decisions, reviews, connector state, and audit anchors before migration.
3. Test new code with both the current and proposed policy, including replay of representative historical requests.
4. Check schema compatibility and deploy consumers before producers when adding fields.
5. Canary the decision worker without an actuator, compare old and new receipts, then enable writes gradually.
6. Monitor `ALLOW`, `REVIEW`, and `BLOCK` distributions, connector failures, review backlog, and delivery timeouts.

To roll back policy, restore the exact previously approved JSON and confirm that its original version and fingerprint return. Do not reuse an old `policy_version` for edited content. To roll back code, redeploy the recorded release artifact with the compatible schema and policy. Never rewrite old decision receipts or delete reviews to make them match the rollback; their fingerprints document the rules active when they were created.
