# ContextGate

**Real-time evidence and approval control for AI agents.**

ContextGate is a streaming context firewall. It evaluates changing facts before an AI agent stores them or acts on them, preserves the best-supported value, blocks unsafe actions, explains the evidence, and records human review without silently inventing missing context.

The proved demo is intentionally narrow:

1. An official confirmation says the fictional Nova AI Summit venue is **10 Innovation Street**.
2. A later community listing says **2 Innovation Street**.
3. An agent asks to update a calendar to the community value.
4. ContextGate applies source policy before recency, classifies a conflict, and emits **BLOCK / HIGH**.
5. A human can hold, reject, or record an explicit override. No calendar update is executed.
6. Every decision and review is appended to a hash-chained audit log.

## Why this fits Confluent AI Day

The application makes streaming infrastructure part of the product, not a transport afterthought:

```text
context_events ──> normalize + deduplicate ──> authoritative_context ──┐
                                                                      ├─> deterministic decision ──> context_decisions
action_requests ─────────────── temporal join (as-of request time) ───┘                              │
                                                                                                    ├─> review UI ──> review_events
                                                                                                    └─> Streaming Agent explanation
                                                                                                         └─> context_agent_logs
```

- Kafka is the replayable evidence and audit spine.
- Flink maintains authoritative context and makes the enforcement decision.
- The Streaming Agent explains a decision already made; it cannot override `BLOCK` or `REVIEW`.
- The local Python path stays functional if cloud access, model credentials, or venue Wi-Fi fail.

## Run the proved local slice

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m context_gate
python -m pytest
streamlit run app.py
```

The command-line result should include:

```json
{
  "classification": "CONFLICT",
  "decision": "BLOCK",
  "risk": "HIGH",
  "authoritative_value": "10 Innovation Street",
  "evidence_event_ids": ["evt-100", "evt-104"],
  "requires_human_approval": true
}
```

The Streamlit app writes runtime records to `runtime/audit.jsonl`. That directory is ignored by Git. Replaying adds a new run; it does not erase earlier decisions.

## Repository map

```text
context_gate/   Strict models, source policy, deterministic rules, approvals, audit, adapter
data/           Fully synthetic Nova AI Summit inputs
flink/          Documentation-derived Confluent Cloud SQL, separated by deployment step
schemas/        Generated JSON Schemas for the public event, request, and decision contracts
tests/          Deterministic and failure-path tests
docs/           Architecture, demo script, source-pattern review, and event-day checklist
```

## Event-day Confluent sequence

Do not run the SQL blindly as one script. In the organizer-provided Flink workspace:

1. Confirm the catalog, database, registered model name, permissions, and allowed table formats.
2. Run `00_create_tables.sql`, `10_normalize_events.sql`, and `20_deduplicate_events.sql`.
3. Submit the continuous `INSERT` in `30_authoritative_context.sql`.
4. Seed the two context events with `05_seed_context_events.sql` and verify `evt-100` is authoritative.
5. Submit the continuous decision statement in `40_detect_conflicts.sql`.
6. Publish the action with `45_seed_action_request.sql`; verify `context_decisions` contains `CONFLICT / BLOCK / HIGH` before enabling any model.
7. Replace only the documented `workshop_model` placeholder and run `50_create_agent.sql`.
8. Submit `60_run_agent.sql`, then inspect `70_query_agent_logs.sql`.
9. Keep screenshots or exported rows from the working cloud path.

The SQL is derived from current official Confluent documentation but has not been cloud-compiled because the workshop catalog, registered model, entitlements, and RBAC are not available locally. Its claimed scope is the Nova conflict and missing-evidence paths—not all seven classifications.

## Safety and cost defaults

- `CONTEXTGATE_MODE=local` is the default and makes no network call.
- No credentials belong in this repository; `.env` and Streamlit secrets are ignored.
- The demo uses synthetic data only.
- Human review records intent but never performs a real external action.
- No cloud resource is created by the local app.
- Use event-provided/free infrastructure and ask before provisioning anything billable.

## Current official references

- [Confluent Streaming Agents quickstart](https://github.com/confluentinc/quickstart-streaming-agents)
- [CREATE AGENT](https://docs.confluent.io/cloud/current/flink/reference/statements/create-agent.html)
- [AI_RUN_AGENT](https://docs.confluent.io/cloud/current/flink/reference/functions/model-inference-functions.html#ai-run-agent)
- [Monitor Streaming Agents](https://docs.confluent.io/cloud/current/ai/streaming-agents/monitor-streaming-agents.html)
- [Flink deduplication](https://docs.confluent.io/cloud/current/flink/reference/queries/deduplication.html)
- [Flink temporal joins](https://docs.confluent.io/cloud/current/flink/reference/queries/joins.html#temporal-joins)
