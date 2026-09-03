# Judge start here — 90-second acceptance path

This path proves the local product without credentials, cloud access, or a model.

## Start

```powershell
# Windows
.\run.ps1
```

```bash
# macOS or Linux
bash run.sh
```

Open [http://127.0.0.1:8501](http://127.0.0.1:8501) if the browser does not open automatically.

## 90-second enforcement test

1. Confirm the default B1 metrics are `CONFLICT / BLOCK / HIGH / PENDING`.
2. Verify the official **10 Innovation Street** card remains authoritative over the newer copied **2 Innovation Street** card.
3. In **Ask ContextGate**, choose **Show the overall outcome breakdown**. Expect exactly `ALLOW=3`, `REVIEW=3`, and `BLOCK=3`.
4. Ask **Why was B1 blocked, and what should happen next?** Expect case B1, events `evt-100` and `evt-104`, and rule `CG-002` in the grounded trace.
5. Turn **Speaker on**. The visible answer should be read with an available English device voice; switch it off or press **Stop**.
6. Select A1. Expect `SAFE / ALLOW / LOW / NOT_REQUIRED` and no review form.
7. Select R1. Expect `INSUFFICIENT_EVIDENCE / REVIEW / HIGH / PENDING` and an incomplete evidence card.
8. Return to B1, choose **Reject**, and confirm a `REJECTED` review receipt with `action_executed=false`.
9. Expand the audit log. Expect **Verified** in the sidebar and downloadable decision/review receipts.

## Two-minute memory and meaning test

1. Open **Company Memory**, click **Load fictional 3 + 8 event dataset**, and confirm counts of 3 at `35 Main St` and 8 at `76 New Avenue`.
2. Assess the default Suite `354` candidate. Expect `REVIEW`, the prior Suite `232` pattern, a request for stronger confirmation, and no lookup, storage, or action.
3. Expand the contributor trace to see the exact fictional evidence references behind the count.
4. Open **Important Details**, start from crowd size 35, and analyze “78 more people are confirmed.” Expect `35 + 78 = 113`.
5. Change the statement to “78 people are going.” Expect `TOTAL 78 = 78` rather than 113.
6. Ask **How did you get that total?** and inspect the cited formula, bounded excerpt, evidence reference, and digest.
7. Try ambiguous wording or a different event identity. Expect `REVIEW` with no candidate update.
8. Append a numeric correction and confirm the original interpretation remains visible beside the recalculated trace.

## Pass checklist

- [ ] All three enforcement outcomes render without an exception.
- [ ] The chat answer cites only real case, event, and rule IDs.
- [ ] An out-of-scope question produces an explicit abstention.
- [ ] Voice is optional and all spoken content remains visible.
- [ ] No external calendar, email, web, or cloud action occurs.
- [ ] The original B1 block remains immutable after human review.
- [ ] Suite 354 is reviewed rather than silently learned as truth.
- [ ] Delta, total, mismatch, ambiguity, and append-only correction paths behave as described.

## Command-line fallback

If a browser is unavailable:

```powershell
python -m context_gate list
python -m context_gate run conflict
python scripts/doctor.py
python scripts/acceptance_matrix.py
python -m pytest
```

## What this proves

- Deterministic, replayable local enforcement over nine synthetic cases.
- Model-independent safety and strict explanation fallback.
- Bounded grounded conversation, related-pattern discovery, and safe-step guidance.
- Tenant-scoped pattern counts, attributable contributors, and anomaly review.
- Configured total-versus-delta interpretation with exact calculation traces.
- Exact request/evidence/decision binding and local audit corruption detection.

## What this does not prove yet

- Authentication of a real producer or organizer source.
- Successful compilation in the workshop’s Confluent Cloud environment.
- A live registered model, Streaming Agent entitlement, or agent log row.
- An externally anchored, deletion-proof audit ledger.
- Execution of any real-world action.
