# ContextGate solo hackathon demo script

Target length: **4 minutes 35 seconds**. All people, companies, events, addresses, and evidence in this walkthrough are fictional.

## Before presenting

1. From the repository root, run `.\run.ps1` on Windows or `bash run.sh` on macOS/Linux.
2. Open [http://127.0.0.1:8501](http://127.0.0.1:8501), leave the example company policy active, and confirm the dashboard shows **9 total / 3 passed / 3 blocked / 3 needing attention**. If rehearsal left B1 corrected, choose **ALLOW**, expand its **HUMAN-CORRECTED** row, and click **Retract correction for B1** to restore the opening 3/3/3 resolved view; the retraction preserves history.
3. Keep browser zoom around 80–90% so the four dashboard cards and chat box fit together.
4. Use only the fictional data already in the app. Turn the speaker off unless the room audio has been tested.
5. Keep these three included backup screenshots ready in case the browser display fails:

   - `docs/screenshots/01-dashboard.png`
   - `docs/screenshots/02-red-explanation.png`
   - `docs/screenshots/03-correction-learned.png`

## 0:00–0:20 — Hook: stop an agent before the mistake

**Show:** The top of the app, including the pipeline and four dashboard cards.

**Say:**

> Agents fail when an action rests on a changed, conflicting, or unverified fact. ContextGate is a context firewall: it resolves evidence, enforces policy, and creates a receipt before an agent can act.

## 0:20–0:45 — Prove all three outcomes live

**Click:** Leave **Dashboard queue** on **REVIEW** long enough to show the amber `!` labels, then choose **ALLOW**, and finally **BLOCK**.

**Say:**

> Nine cases evaluate live under this policy: three green passes, three amber reviews, and three red stops. Labels and symbols reinforce every color. These are current policy outcomes, and opening one executes nothing.

## 0:45–1:15 — Ask the dashboard why it is red

**Click:** In **Ask ContextGate**, enter exactly **Why are there so many red items?** Leave **Remember this as company guidance** unchecked because this is a question, then click **Ask ContextGate**.

**Point to:** The B1, B2, and B3 explanations and their evidence/rule citations.

**Say:**

> Red maps to BLOCK, so ContextGate explains all three stopped cases with their evidence and rule IDs. This is bounded, local receipt retrieval—no lookup, model call, or action.

## 1:15–1:50 — Inspect B1's evidence and rule

**Click:** Set **Dashboard queue** to **BLOCK**, expand the row beginning **× B1 · Lower-authority conflict · BLOCK · Stopped**, and click **Open B1 details**.

**Scroll:** Move below **Ask ContextGate** to the full B1 evidence view.

**Point to:** Official **10 Innovation Street**, copied **2 Innovation Street**, evidence IDs `evt-100` and `evt-104`, and rule `CG-002-LOWER-AUTHORITY-CONFLICT`.

**Say:**

> A newer copied listing conflicts with the official confirmation. Freshness is not authority: CG-002 keeps the official value, cites both events, and emits BLOCK before a model can influence enforcement.

## 1:50–2:50 — Correct a fictional false positive without erasing history

**Click:** Scroll back to **Decision dashboard** and expand the B1 row beginning **× B1 · Lower-authority conflict · BLOCK · Stopped** again. Under **Fix a mistaken decision**, choose **ALLOW** in **Corrected outcome for B1**, leave **Correction reviewer for B1** as `local-reviewer`, and keep **Remember correction for similar future cases** checked. Enter this in **Correction rationale for B1**, then click **Save correction for B1**:

> Fictional judge demo: the reviewer determined B1 was a labeled false positive for training. This resolved-view correction authorizes no action.

**Scroll and point:** Return to the dashboard metrics: **Passed gate** is now **4**, **Blocked** is **2**, and the notice still shows original totals of `3 ALLOW / 3 REVIEW / 3 BLOCK`. Set **Dashboard queue** to **ALLOW**, expand the row beginning **✓ B1 · Lower-authority conflict · ALLOW · Passed gate · HUMAN-CORRECTED**, and point to **Original deterministic outcome: BLOCK** plus **Human-corrected: original BLOCK → effective ALLOW**. In **Ask ContextGate**, expand the row beginning **Learned operator guidance** and point to the new correction-based lesson.

**Say:**

> The resolved view changes to four passed and two blocked, but B1's deterministic BLOCK receipt remains intact. The correction is a separate, fingerprint-bound human record, and no action was executed.
>
> The checked box saves an attributable lesson for future chat guidance. It is persistent and reversible, but it never changes or bypasses enforcement. **Retract correction for B1** restores the original effective status while preserving history.

**If the correction already exists from rehearsal:** Point to the retained correction and learned guidance, explain that reloading does not erase the original receipt, and continue. Do not create a duplicate correction.

## 2:50–3:45 — Show useful company memory and exact arithmetic

**Click:** Expand **Company Memory · patterns, anomalies, and corrections**, then click **Load fictional 3 + 8 event dataset**.

**Point to:** Three observations at **35 Main St** and eight at **76 New Avenue**. Then expand **Company Memory · show your work contributor trace** and point to the contributing evidence references.

**Say:**

> Company Memory finds a three-versus-eight address pattern with contributor provenance. A change from Suite 232 to 354 asks for stronger confirmation instead of silently becoming truth.

**Click:** Expand **Important Details · totals, additions, and corrections**, leave the current crowd size at **35** and the statement as **78 more people are confirmed**, then click **Interpret important detail**.

**Point to:** `PROPOSE · DELTA`, the formula `35 + 78 = 113`, and the evidence reference/digests.

**Say:**

> “Seventy-eight more” is a delta, so 35 plus 78 proposes 113. Without “more,” 78 would be the total. Identity mismatch or ambiguity goes to review. This shows its arithmetic and proposes only; it does not update state.

## 3:45–4:15 — Explain the Confluent deployment path

**Scroll and point:** Return to the pipeline at the top of the app.

**Say:**

> Kafka is the replayable evidence spine. The supplied Flink path covers core authority, conflict, and hold branches; the full nine-case Python gate works locally. A Streaming Agent can explain an already-governed receipt. The event workspace still needs its catalog, permissions, and SQL compilation.

## 4:15–4:35 — Close

**Say:**

> ContextGate remembers what is known, why it is trusted, what changed, and who corrected it. People improve the guidance; policy still controls action. Without a model, cloud, or Wi-Fi, the local safety path still works.

## Offline backup path

If the network fails, continue normally: the app binds to localhost and the showcased dashboard, chat, memory lab, semantic interpreter, and deterministic decisions require no cloud call or API key.

If the browser fails:

```powershell
.\run.ps1 acceptance
.\run.ps1 demo
```

```bash
bash run.sh acceptance
bash run.sh demo
```

Say:

> The acceptance matrix exercises the same nine fictional paths without the browser: three ALLOW, three REVIEW, and three BLOCK. The demo command prints the B1 receipt. Neither command attempts an external action.

If both the browser and terminal display fail, present the three included screenshots in order and narrate the matching sections above. Explain that they are captured output from the tested local build rather than the current live session.

## Fast answers for likely judge questions

**Who assigns source trust?** The active company policy assigns authority ranks and caps submitted trust. A payload cannot declare itself official; production requires an authenticated connector or ACL-bound route.

**Did the false-positive correction change the safety rule?** No. It added a human correction and optional advisory guidance. The original decision stays immutable, and future enforcement still runs the deterministic policy.

**Is the memory an autonomous knowledge base?** No. It is bounded, local, tenant-tagged structured observation storage with contributor traces. In this demo it is plaintext SQLite; production needs authenticated tenant binding, encryption, retention controls, and protected backups.

**Why Confluent?** The context changes continuously. Kafka preserves the evidence history, Flink maintains authority and evaluates requests at event time, and the Streaming Agent explains an already-governed result.

**What works without Confluent Cloud?** The Python engine, dashboard, fictional acceptance suite, grounded local chat, memory lab, important-detail interpreter, and audit receipts. The supplied Flink SQL is the workshop deployment path and must be adapted and compiled in the assigned cloud environment.
