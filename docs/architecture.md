# Architecture

## Enforcement boundary

ContextGate separates three kinds of work:

1. **Deterministic enforcement** normalizes evidence, derives source authority from policy, identifies the best-supported value, and emits `ALLOW`, `BLOCK`, or `REVIEW`.
2. **Model explanation** receives a structured evidence package and may improve the wording only. Its response is untrusted until every enforcement field matches the deterministic result.
3. **Human review** creates a new append-only receipt. It never rewrites the original decision and does not itself execute an external action.

This boundary means model failure, malformed JSON, prompt injection, or an incorrect `ALLOW` cannot weaken a deterministic `BLOCK`.

## State and event flow

| Stage | Input | Output | Contract |
|---|---|---|---|
| Normalize | `context_events` | canonical IDs, fields, values, timestamps, derived hash | Missing provenance is retained as missing |
| Deduplicate | normalized evidence | stable semantic source record | Policy-relevant sensitivity, status, trust, and effective time cannot be suppressed |
| Resolve authority | deduplicated evidence | keyed `authoritative_context` | Policy rank, verification status, capped trust, then time |
| Decide | action + authority as of request time | append-only `context_decisions` | Deterministic and model-independent |
| Explain | decision evidence package | `context_agent_explanations` | Only explanation text may be adopted after validation |
| Review | pending decision | append-only `review_events` | Hold, reject, or explicit override; no execution |

## Authority policy

The synthetic cases carry a source type and trust score. ContextGate maps that type to the built-in acceptance policy and caps the submitted score. In production, a trusted connector or ACL-bound ingestion route must stamp an allowlisted source identity; payload fields alone are not authentication. In the Pattern Lab:

| Source type | Rank | Trust cap |
|---|---:|---:|
| Registration confirmation | 100 | 1.00 |
| Organizer API | 98 | 1.00 |
| Organizer website | 95 | 0.98 |
| Official email | 92 | 0.98 |
| Partner website | 70 | 0.85 |
| Copied webpage | 50 | 0.70 |
| User report | 40 | 0.65 |
| Unknown | 10 | 0.40 |

The official confirmation therefore remains authoritative even though the copied listing arrived later. This proves deterministic policy behavior over synthetic inputs, not authenticated source identity.

## Review lifecycle

```text
BLOCK or REVIEW
      │
      └── PENDING ──> HELD
                   ├─> REJECTED
                   └─> HUMAN_OVERRIDE
```

`HUMAN_OVERRIDE` is deliberately explicit. It means a person accepted responsibility for departing from the deterministic result; it does not rewrite the evidence and the demo still reports `action_executed=false`. A decision accepts one review disposition; a contradictory second receipt is rejected as an audit identity collision.

## Audit design

The local fallback uses append-only JSON Lines. Each entry includes the previous entry hash and its own SHA-256 digest. Editing a prior record breaks `verify_chain()`, and a reused identity with changed payload is rejected. This detects local corruption but is not an externally anchored ledger: an operator with filesystem control could replace or truncate the whole file. Kafka topics provide the live replay spine; ContextGate-owned decision and review topics remain separate from Confluent's fixed-schema agent system log.

## Honest MVP boundary

The Python engine implements all named classifications and is directly tested on the highest-risk paths. The current Flink SQL deliberately demonstrates:

- exact deduplication shape;
- policy-based authoritative state;
- temporal request-to-authority joining;
- `CONFLICT / BLOCK / HIGH`;
- missing-evidence review;
- non-blocking agent explanation and fixed-schema logs.

The nine-case local Pattern Lab covers three `ALLOW`, three `REVIEW`, and three `BLOCK` results, including near-peer ties. The Flink path must still be compiled and tested in the actual workshop environment before making broader cloud claims.
