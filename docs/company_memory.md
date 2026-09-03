# Company memory and important-detail interpretation

ContextGate can retain bounded, structured observations for one company tenant and calculate explainable patterns from them. A separate deterministic interpreter can distinguish a configured replacement total from an amount to add. Both features are advisory: they make no inbox lookup, network request, state change, or external action.

The local web app includes a fictional path for trying these features. Select a fresh test tenant and choose **Load fictional 3 + 8 event dataset**. It adds three event observations at `35 Main St` and eight at `76 New Avenue`, with the latter observations using Suite `232`. Loading the same data again is idempotent.

## What the pattern memory does

`context_gate.company_memory` stores strict `CompanyObservation` records in a caller-selected SQLite file. Every query is filtered by `tenant_id`, and `PatternAnalyzer` produces:

- per-attribute counts, such as three observations for `35 Main St` and eight for `76 New Avenue`;
- trusted counts after applying the active company source policy and configured trust threshold;
- an address-to-suite conditional pattern;
- the contributing observation IDs, timestamps, source types, statuses, trust decisions, sensitivities, and evidence references; and
- an advisory candidate assessment with reasons, human questions, and recommended confirmation steps.

The default minimum pattern support is three trusted observations. History and contributor lists are bounded, and the result says when history or contributors were truncated. The analyzer only uses observations at or before the candidate's timestamp, so later records cannot create a historical pattern for an earlier candidate.

For example, after eight trusted observations support `76 New Avenue` / Suite `232`, a candidate at the same address but Suite `354` produces `REVIEW` with `NEW_SUITE_AT_KNOWN_ADDRESS`. It asks whether the suite changed and which exact source confirms it. It does not search for that source or silently replace Suite `232`.

`ALLOW_LIKE` only means the candidate resembles sufficiently supported tenant history. It is not ContextGate's `ALLOW` enforcement decision and must still pass the normal source, evidence, sensitivity, and action policy.

## Show the contributors, not just the count

A pattern count without provenance is not enough for a business decision. Each displayed pattern includes a bounded contributor trace. The local app shows the observation ID, source-type claim supplied by the integration, and evidence reference for each visible contributor; it redacts sensitive references in the trace.

Keep evidence references stable and resolvable inside the company's authorized source store. The reference is lineage, not proof by itself. A production adapter must establish source identity from an authenticated connection rather than trusting a user-provided `source_type`.

## Configure an important quantitative detail

[`config/semantic_profile.example.json`](../config/semantic_profile.example.json) defines one strict `CategorySemanticConfig`. Its fictional event profile declares `crowd_size` important and uses both `event_name` and `event_date` as identity keys. Copy and review the file before adapting its vocabulary and numeric bounds:

```powershell
Copy-Item .\config\semantic_profile.example.json .\config\semantic_profile.json
```

```bash
cp config/semantic_profile.example.json config/semantic_profile.json
```

This file is not loaded automatically and must not contain secrets or personal data. The integration owns its path, access controls, release process, and retention. Load it through the strict model boundary:

```python
from pathlib import Path

from context_gate.semantic_updates import CategorySemanticConfig

profile = CategorySemanticConfig.model_validate_json(
    Path("config/semantic_profile.json").read_text(encoding="utf-8"),
    strict=True,
)
```

All configured identity keys must match one unambiguous value after text normalization. A missing, conflicting, or different event name or date routes the statement to `REVIEW`; correcting the quantity cannot bypass identity verification.

The interpreter recognizes only company-configured nouns and markers. In the supplied fictional examples:

| Prior accepted crowd size | Incoming statement | Interpretation | Candidate result |
|---:|---|---|---:|
| 35 | “78 more people are confirmed…” | `DELTA` | `35 + 78 = 113` |
| 35 | “78 people are going…” | `TOTAL` | `78` |
| 35 | total wording for a different event name | identity mismatch | `REVIEW`, no candidate total |
| 35 | “mentions 78 people…” without a total or delta marker | ambiguous | `REVIEW`, no candidate total |

Negation, multiple quantities, conflicting markers, stale evidence, missing prior state for a delta, and configured plausibility-limit violations also require review. This is a constrained deterministic parser, not general language understanding.

## Run the fictional semantic examples

[`examples/company/semantic-state.json`](../examples/company/semantic-state.json) contains the accepted total of 35. [`examples/company/semantic-statements.json`](../examples/company/semantic-statements.json) contains the four cases above. They can be validated and interpreted without persistence:

```python
from pathlib import Path

from pydantic import TypeAdapter

from context_gate.semantic_updates import (
    CategorySemanticConfig,
    EntityQuantityState,
    IncomingQuantityStatement,
    interpret_quantity_update,
)

profile = CategorySemanticConfig.model_validate_json(
    Path("config/semantic_profile.example.json").read_text(encoding="utf-8"),
    strict=True,
)
state = EntityQuantityState.model_validate_json(
    Path("examples/company/semantic-state.json").read_text(encoding="utf-8"),
    strict=True,
)
statements = TypeAdapter(list[IncomingQuantityStatement]).validate_json(
    Path("examples/company/semantic-statements.json").read_text(encoding="utf-8"),
    strict=True,
)

for statement in statements:
    proposal = interpret_quantity_update(profile, state, statement)
    print(statement.evidence_id, proposal.outcome, proposal.calculation_trace.formula)
```

`PROPOSE` means only that the constrained interpretation produced a candidate for downstream evidence and approval checks. The function never updates `state`.

## Answer “how did you get that total?”

Every semantic proposal carries a `calculation_trace` with:

- the bounded, whitespace-normalized excerpt interpreted;
- evidence ID and evidence reference;
- source type and a SHA-256 content digest;
- whether the statement was read as `TOTAL`, `DELTA`, or `AMBIGUOUS`;
- the stated quantity, prior total, and proposed total; and
- a plain formula such as `35 + 78 = 113` or `TOTAL 78 = 78`.

The input, semantic profile, and exact state snapshot each receive a deterministic fingerprint or digest. This makes a later explanation attributable to the inputs used, but it does not authenticate who supplied them.

## Preserve corrections append-only

There are three correction paths, and none rewrites original evidence or a decision receipt:

- `HumanCorrection` in company memory is stored beside the target observation. The current pattern analyzer deliberately does **not** apply correction rows to historical counts. To affect future counts, create a separate, source-confirmed corrected observation and explicitly remember it.
- `HumanQuantityCorrection` can replace the earlier field/mode/quantity interpretation and deterministically recalculate a semantic proposal. The result embeds the untouched original proposal and the full ordered correction history. It cannot override identity, stale-state, configuration-fingerprint, or numeric safety checks, and it still performs no persistence or action.
- `DecisionCorrection` in operator learning changes only the resolved operator view. It is bound to the original case and decision ID plus the request, evidence, and policy fingerprints. The deterministic receipt remains unchanged, and the correction grants no enforcement or action authority.

A production workflow should authenticate and authorize the reviewer, bind the reviewer identity server-side, require a rationale and exact evidence reference, and write the correction to externally protected append-only storage.

Operator guidance is also explicit: the local app stores it only when a person checks the guidance control or confirms a correction with similar-case guidance enabled. Ordinary chat and evaluations are not learned. Guidance and corrections are immutable; retraction appends a tombstone instead of deleting the original. Guidance may influence later chat advice, but it never changes the deterministic gate.

## Local storage and production limitations

The local application defaults to `runtime/company_memory.sqlite3`; `CONTEXTGATE_MEMORY_PATH` can select another local file. Company Memory and operator learning use that same SQLite database, but they keep separate tenant-tagged records. `CONTEXTGATE_TENANT_ID` selects only the operator-learning namespace and defaults to `local-company`; the local Company Memory tenant text field remains a separate test selector. Treat this as a single-user pilot store, not a production tenant database:

- SQLite values, including company attributes and evidence references, are plaintext unless the host or volume supplies encryption.
- `tenant_id` is a row filter, not authentication or authorization. Neither the local Company Memory text field nor `CONTEXTGATE_TENANT_ID` can be trusted as an access-control boundary.
- Stored SHA-256 payload digests are unkeyed. They can detect ordinary corruption or an identity collision during normal use, but a database owner can replace both payload and digest. They are not signatures or an externally anchored audit guarantee.
- No account login, RBAC, per-tenant key management, automatic backup, retention schedule, legal hold, export, or deletion workflow is bundled.
- The application does not automatically clear memory. An authorized administrator must define and carry out retention, backup, restoration, export, and deletion procedures.

Before using real company data, place the memory service behind authenticated tenant binding, authorization, encryption at rest, least-privilege filesystem/database access, protected backups, monitoring, and documented retention/deletion controls. Keep private source artifacts outside this compact pattern store and retain only the minimum attributes and evidence references needed for the decision.
