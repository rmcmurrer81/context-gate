# ContextGate four-minute solo demo

Target length: **3 minutes 45 seconds**. The event records, locations, counts, sources, and email-like evidence are fictional. **Kira Labs** and `kiralabs.org` are presentation branding, not a claim that the fictional evidence came from company systems.

## Before presenting

1. Start ContextGate with the Desktop shortcut, `.\run.ps1` on Windows, or `bash run.sh` on macOS/Linux.
2. Open [http://127.0.0.1:8501](http://127.0.0.1:8501) and maximize the browser. Keep zoom near 90% so the left settings, center dashboard, and right chat are visible together.
3. Click **Company setup** in the left rail and confirm:
   - **Company display name:** `Kira Labs`
   - **Company website:** `https://kiralabs.org`
   - **Most important detail:** `Crowd size`
   - **Risk posture:** `Safety first (active default)`
   - **Company name on exports:** on
   - **Custom export footer:** `Kira Labs · kiralabs.org`
   - **Company logo:** the supplied Kira Labs logo
4. Save the profile or operator settings only if a value changed, then close the dialog. Open **Connect sources** and confirm **Fictional scenario stream — Available** and **Public website intake — ON-DEMAND**. Do not add a live URL during the timed path. Close it and confirm the left source signal says **Mailbox: Not connected**.
5. In the center queue, select **ALL** and make sure the fictional baseline is visible. Do not leave a correction from rehearsal active.
6. Keep speaker output off unless room audio has already been tested. Keep these tested backup images open in another window:
   - `docs/screenshots/01-dashboard.png`
   - `docs/screenshots/02-red-explanation.png`
   - `docs/screenshots/03-correction-learned.png`
   - `docs/screenshots/04-event-intelligence.png`
   - `docs/screenshots/05-website-sources.png`
   - `docs/screenshots/06-calendar.png`
   - `docs/screenshots/07-sales-intelligence.png`

## 0:00–0:17 — Hook: changing context is the risk

**Show:** The entire dark command center. Keep all three columns visible.

**Say:**

> Agents act on moving facts: a venue changes, a count rises, or a weak source contradicts the official one. ContextGate evaluates that evidence before an agent can act.

## 0:17–0:46 — Make it a company workspace

**Click:** In the left column, open **Company setup**. Point to **Kira Labs**, `kiralabs.org`, the logo, **Crowd size**, and **Safety first (active default)**. Scroll briefly to **Connected mailbox accounts**, then close the dialog. Open **Connect sources** and point to **Fictional scenario stream — Available** and **Public website intake — ON-DEMAND**. Close it and point to **Mailbox: Not connected** in the left rail.

**Say:**

> A company can brand the workspace and its reports, then define what matters, how records match, and how cautious decisions should be. Kira Labs tracks crowd size under a safety-first policy, using fictional evidence. This display name is not authentication.

**Point to:** **Add Google account** and **Add Microsoft account** in **Company setup**, if they are visible.

**Say:**

> Gmail and Outlook or Hotmail require provider-hosted, read-only OAuth consent. An emailed address or password is never a shortcut. Production tokens belong in a secret vault; this demo is not connected.

> A public website needs a URL and an extraction goal, then a person chooses Scan. An operator can also enable a visible periodic check while the command center stays open; it is not a hidden or durable background service.

## 0:46–1:12 — Read the decision picture instantly

**Point to:** The center outcome totals and visual distribution.

**Click:** Select **REVIEW**, then **BLOCK**, then return to **ALL**.

**Say:**

> This is an operational picture: green can preview safely, amber needs a person, and red is stopped. New evidence is reevaluated under company policy, and the operator filters everything here.

## 1:12–1:43 — Prove why one action was stopped

**Click:** Select **BLOCK**, click the **B1 · Lower-authority conflict** row, then click **Inspect case** to open **Case details**.

**Point to:** The official value, conflicting copied value, evidence timeline, rule ID, and original `BLOCK` receipt.

**Say:**

> A newer copied listing conflicts with an official confirmation. Newer is not automatically more authoritative. The gate cites both records, applies the conflict rule, and blocks the action. A model can explain the receipt, not rewrite it.

**Click:** Close **Case details**, then return the queue to **ALL**.

## 1:43–2:25 — Ask the evidence two real-world questions

**Click:** In the persistent right-side **Ask ContextGate** box, leave company-guidance learning off. Enter exactly:

> How many fictional events came from Eventbrite? Show your sources.

**Click:** **Ask ContextGate**. Point to the answer and its fictional source references.

**Click:** In the same box, enter exactly:

> How many fictional events are in NYC? Show your sources.

**Click:** **Ask ContextGate**. Point to the answer and its contributing records.

**Say:**

> Chat stays beside the decisions. It counts only loaded fictional records, removes update duplicates, and shows its sources—not my inbox. Manual or explicitly enabled while-open email and public-website scans can feed the same questions without mixing their real records with the fictional catalog.

## 2:25–2:50 — Close the human and audit loop

**Click:** Select **REVIEW**, click the first amber row, then click **Inspect case**.

**Point to:** **Record a human correction**, **Rationale**, and **Original receipt**. Do not submit a rehearsal correction during the judged run.

**Say:**

> Ambiguous evidence and consequential actions go to a person. Their rationale becomes a separate receipt while the original stays intact. Explicit guidance can improve explanations, but chat never silently changes authority or bypasses the gate.

**Click:** Close **Case details**.

## 2:50–3:10 — Create branded evidence without taking action

**Click:** In **Ask ContextGate**, enter exactly:

> Create a combo report and pie chart of what you are showing me.

**Point to:** The `.docx`, `.pdf`, and `.png` paths in the answer. If generation would interrupt timing, point to the pre-generated copies in `Documents/ContextGate Exports` instead.

**Say:**

> One instruction creates real Word, PDF, and image files in Documents. Kira Labs, its logo, website, and custom footer appear automatically. A person can print or attach them later; ContextGate sends nothing and takes no automatic action.

## 3:10–3:34 — Why this belongs at Confluent AI Day

**Point to:** The Kafka → Flink → deterministic gate → agent → human/audit flow in the command center.

**Say:**

> Companies lose time reconciling changing facts, duplicate updates, and risky exceptions. Confluent fits because that context changes continuously: Kafka is the replayable evidence spine, Flink deduplicates, handles event time, maintains authority, and evaluates conflicts. The gate emits ALLOW, REVIEW, or BLOCK before agent action. Then a Streaming Agent can explain the governed receipt, and human audit feedback can return to the stream.

## 3:34–3:45 — Close

**Say:**

> ContextGate makes changing evidence governable: what is known, why it is trusted, and who approved it. The agent stays useful; the company stays in control.

## Optional live prompts after the timed path

Use these only if a judge asks to explore further:

1. Enter `Keep track of events from Hanson Robotics.` Point out that the answer lists each matching fictional event's address, date, and time and records the request as explicit company guidance.
2. Open **Calendar** and select a dated event. Show its organizer, time, address, source, and evidence pointer; then expand **Events needing a date** to explain that ContextGate does not guess missing dates. The amber banner clearly identifies the fictional proof set.
3. Enter `Do not show me data from Posh.` Ask for the current counts and show that the records are hidden from answers, patterns, and the calendar but retained. Enter `Show me data from Posh again.` to reverse it.
4. Explain before using `Delete data from Posh.` Deletion removes matching records from the local catalog and blocks automatic re-import; it does not delete the upstream email and is intentionally different from hide.
5. Enter `Create a PDF report without the company name.` Show that this single file omits the Kira Labs name while the saved branding preference stays on.
6. Enter `Also track office sales`, inspect the proposed metric and identity field, then enter `Confirm tracking configuration`. Upload `examples/company/fictional-office-sales.csv`; ask `What are total sales for New York?` and `What are total sales for Austin?` to show the fictional totals 189 and 73 with contributing row references. Add `Track revenue by region`, confirm it, then use `What are you tracking?` and `Go back to sales` to show that topics remain independent. This path reads only the deliberately uploaded local CSV; it does not connect to a CRM or start continuous monitoring.

These prompts demonstrate useful company controls without pretending that chat can silently change source authority or act in an external system.

## Optional personal-use story

**Say:**

> The same controls can help one person track events, purchases and receipts, reservations and travel, venue changes, or any important update spread across authorized email, uploaded files, and public websites. I add a public URL, state the evidence goal, and scan it when I choose. ContextGate keeps the message, file, or public URL reference behind each answer, so I can ask where a date, address, or total came from and correct it without erasing the original receipt. Hide is a reversible view preference; delete removes matching records from this local catalog and prevents automatic re-import, but does not erase the upstream source.

The public website connector is implemented with immediate manual scans and optional while-open periodic scans. It extracts Schema.org JSON-LD or iCalendar events and otherwise returns bounded page evidence. It does not run JavaScript, log in, bypass paywalls, reach private/local network addresses, download more than 2 MB, or claim to be a durable monitoring service. The page clears its timer when closed and pauses future checks after detecting that the local server is unavailable.

## Offline backup

The fictional demo and local safety gate require no cloud call, email connection, or API key. If Wi-Fi fails, continue with the live localhost app.

If the browser or projector fails, show these screenshots in order without changing their filenames:

1. `docs/screenshots/01-dashboard.png` — narrate the one-screen command center, company settings, sources, and visual outcomes.
2. `docs/screenshots/02-red-explanation.png` — narrate the persistent chat and its evidence-grounded answer.
3. `docs/screenshots/03-correction-learned.png` — narrate the preserved original receipt and explicit human feedback.
4. `docs/screenshots/04-event-intelligence.png` — show organization tracking with grounded dates, times, addresses, and source links.
5. `docs/screenshots/05-website-sources.png` — show saved public sources and the operator-defined extraction goal.
6. `docs/screenshots/06-calendar.png` — show dated events on the calendar and undated evidence kept separate.
7. `docs/screenshots/07-sales-intelligence.png` — show a second tracking topic with grouped totals and row-level evidence.

Say:

> These are captures from the tested local build using the same fictional evidence. The decision engine is local, deterministic, and independent of the presentation network.

If a terminal is still visible, run:

```powershell
.\run.ps1 acceptance
```

or:

```bash
bash run.sh acceptance
```

Say:

> The acceptance matrix runs the same safety branches without the browser and performs no external action.

## Judge Q&A

**What did you use to build it?** Python 3.11+, strict Pydantic contracts, a lightweight local HTML/CSS/JavaScript command center, SQLite for local memory and operator feedback, and an append-only audit log. The repository also includes a Kafka producer adapter, ordered Flink SQL, Streaming Agent artifacts, JSON Schemas, launchers, and a pytest acceptance suite.

**Where exactly do Kafka, Flink, and Confluent fit?** In the deployment path, Kafka carries context evidence, proposed actions, decisions, and human receipts as replayable streams. Flink normalizes and deduplicates evidence, handles event time, maintains source authority, joins each request to the evidence available at that moment, and emits `ALLOW`, `REVIEW`, or `BLOCK`. A Confluent Streaming Agent is downstream and explains the completed receipt; it is not the enforcement layer.

**Are Kafka and Flink live in this demo?** Not yet. This local demo's decision engine is Python, and the bundled fictional catalog runs in-process. The repository supplies the Kafka adapter and Flink SQL as the workshop/deployment path, but I will not claim a live Confluent cluster until the event credentials, catalog, permissions, topics, and SQL compilation are added.

**What is live, and what is fictional?** The command center, policy evaluation, queue filters, evidence modal, deduplication, count answers, chat grounding, company settings, public website connector, while-open timer, receipts, and local fallback are running live. The people, companies, events, locations, messages, counts, and evidence references in the default workspace are fictional. No personal inbox or live website is needed for the judged path.

**Did you test the internet connector against real sites?** Yes. On September 3, 2026, the same local connector successfully reached Kira Labs, NASA Events, Hanson Robotics, Eventbrite, Meetup, and a public launch API. Eventbrite returned 20 structured event records and Meetup returned 31 records that resolved to 30 distinct events; the other sources returned bounded page or API evidence without supported event markup, so ContextGate did not invent calendar entries. These numbers are a test observation, not a permanent claim about those sites.

**Are the security tests fake because they simulate attacks?** No. The scanner makes real network requests. Deterministic tests simulate private-address redirects, oversized responses, invalid content, and timeouts so those hostile conditions are safe and repeatable; separate live-site checks prove the outbound path. A simulated attack test validates a real control—it is not a substitute for the live check.

**Why not let the LLM make the decision?** An LLM is probabilistic, prompt-sensitive, and difficult to replay exactly. ContextGate keeps source rank, time, identity matching, conflicts, and action policy in deterministic code. The model can translate a fixed receipt into useful language, but schema validation prevents its prose from changing the result.

**How does it prevent wrong totals and duplicates?** Every source item has a stable record ID and normalized event key, so an updated message can be retained as evidence without being counted as another event. In the fictional catalog, six Eventbrite messages resolve to five distinct Eventbrite events, and seven NYC messages resolve to six distinct NYC events. For quantities, exact entity keys prevent cross-event updates, while configured language distinguishes a delta such as “78 more” from a replacement total such as “78 are going”; the formula and contributors are shown.

**How does Gmail or Hotmail login work?** The installation owner first registers a Google OAuth desktop app or Microsoft Entra public client. The user clicks **Connect**, signs in only on Google's or Microsoft's page, reviews read-only mail permissions, and returns through an authorization-code flow with PKCE and anti-forgery state. Typing an email address—or typing a password into ContextGate—never connects a mailbox.

**Are mailbox passwords or tokens stored?** ContextGate never asks for or stores mailbox passwords. In this local connector, access and refresh tokens exist only in process memory and disappear when the server stops. They are never written to Git, the audit log, chat, or screenshots. A production deployment would place refresh tokens and client secrets in an encrypted secret manager with rotation and revocation.

**Can it support multiple email accounts?** Yes. Each Gmail or Microsoft account is a separate OAuth session and can be disconnected independently. The local connector can hold multiple accounts during one process, while each provider message keeps its own stable ID and lineage. Production would add per-account scope controls, encrypted token persistence, and retention/deletion workflows.

**How does the public website connector work?** In **Connect sources**, the operator adds a public HTTP or HTTPS URL and a 3–240 character extraction goal, then chooses **Scan** or enables the visible while-open interval in Settings. ContextGate extracts Schema.org JSON-LD `Event` and iCalendar event data; if neither exists, it returns bounded page-title, metadata, and visible-text evidence instead of inventing an event. It does not execute JavaScript, sign in, or bypass paywalls or access controls. DNS and redirects to local or private addresses are blocked, and responses are capped at 2 MB. The page clears the browser timer when closed and pauses it after detecting a local-server failure; it is not a durable production scheduler. When the first real website event is imported, the fictional catalog is retired before import so the totals cannot mix demo and real events.

**Can a company customize the product and its documents?** Yes. Settings control the company and operator names, website, important detail, identity fields, risk posture, and export branding. A PNG or JPEG logo is normalized locally with metadata removed. The company name is the default report heading, while the website and custom footer carry into DOCX, PDF, PNG, and HTML artifacts. A one-time “without the company name” request overrides only that export.

**What is the difference between hide and delete?** Hide is reversible: the matching records stay stored but disappear from the dashboard, chat, counts, and patterns. Delete removes the matching local catalog records and creates a persistent exclusion against automatic re-import. Neither operation changes an upstream mailbox; production deletion must also follow the company's retention, legal-hold, and erasure rules.

**Can it watch a particular organization?** Yes, within loaded evidence. For example, “Keep track of events from Hanson Robotics” returns matching fictional events with address, date, and time and saves that explicit tracking instruction as company guidance. A public events page can be saved and rescanned manually or at the while-open interval; durable monitoring after the local page closes requires an approved production scheduler or stream.

**Can it support multiple companies?** The policy, schemas, and records already carry company-oriented configuration, but the current desktop build is a single-operator local workspace—not a production multi-tenant service. A hosted version must bind every user, connector, topic, key, receipt, and memory query to an authenticated tenant rather than trusting a company-name field.

**How does it scale?** Partition Kafka by tenant and entity, keep authority and deduplication as keyed Flink state, and scale ingestion, decision consumers, and review APIs independently. Compact claims travel through Kafka while private source bodies remain in controlled object storage. The local HTTP server and SQLite file are a pilot surface, not the production scaling boundary.

**What happens if the AI model or cloud is unavailable?** Deterministic enforcement still runs and still produces a receipt; only optional model-generated wording is lost. The local demo continues without Wi-Fi. In production, if a connector or required evidence is unavailable, the system fails closed into review or block instead of inventing context.

**Can chat override a block?** No. Chat can explain evidence and propose an explicit correction. A person must confirm the outcome and rationale, producing a separate fingerprint-bound receipt. The original decision remains intact, the correction is retractable, and neither step executes an external action.

**What can a reviewer export?** A chat request can create branded `.docx`, `.pdf`, and `.png` artifacts directly in `Documents/ContextGate Exports`; an explicit HTML request is also supported. The Export dialog additionally provides printable HTML, current data and receipts as JSON, dashboard SVG, browser print/PDF, and a human-reviewed email draft. ContextGate never silently sends mail, chooses a recipient, or executes an external action.

**What makes ContextGate different from a chatbot or RAG application?** RAG helps a model find text; ContextGate governs whether changing evidence is strong enough for action. It combines event-time source authority, deterministic enforcement, human exceptions, and attributable receipts while placing AI explanation outside the safety boundary. It is a context firewall, not another answer box.

**What business value does that create?** It can reduce wrong updates, duplicate counts, manual reconciliation, exception investigation time, and audit effort. Relevant use cases include event capacity and venue changes, invoice and payment details, logistics addresses and delivery status, customer records, compliance approvals, and any agent that may write to another system. The demo proves the control pattern; it does not claim a measured dollar saving yet.

**What would you build next?** First, connect the assigned Confluent environment and prove the same branches through Kafka and Flink. Then add registered OAuth applications, durable encrypted tenant storage, Schema Registry, authenticated review workflows, OCR with lineage, replay/load tests, operational monitoring, and a narrowly scoped actuator that accepts only a fresh approved `ALLOW` receipt.
