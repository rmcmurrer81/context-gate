# Information and connector intake

ContextGate consumes structured claims; it does not require every source to speak the same format. An upstream adapter converts each source into a `ContextEvent` while retaining exact lineage to the source artifact.

## Typical source paths

| Source | Intake path | Lineage retained |
|---|---|---|
| Organizer or business API | authenticated poll/webhook → Kafka producer | connection ID, endpoint/resource ID, observed time |
| Database | CDC connector → Kafka | connector identity, table/key, source timestamp |
| Website/feed | controlled fetcher → extraction → Kafka | URL, fetch time, content hash, parser version |
| Email | provider OAuth → message API/webhook → extraction → Kafka | connection ID, provider message ID, sender-auth results, attachment hashes |
| Text/CSV/JSON | local or managed document intake → Kafka | artifact hash, filename, explicit user-supplied claim |
| PDF/Word | bounded text extraction → candidate claim → Kafka | artifact hash, page/section where available, extractor version |
| Screenshot/photo | OCR or vision service → candidate claim → Kafka | original artifact hash, OCR/model version, extracted region |

Extraction output is evidence, not automatically trusted state. The gate still checks time, provenance, authority, conflicts, sensitivity, and the action’s exact supporting event.

## Multiple email accounts

Each Gmail or Microsoft account should be a separate OAuth connection with a stable, non-secret `connection_id`. The owner chooses which folders, labels, or senders are in scope. OAuth refresh tokens stay in a provider-backed secret store and never enter events, prompts, audit logs, screenshots, or Git.

A safe email adapter should:

1. receive a provider webhook or poll the provider message API;
2. bind the event to the authenticated account connection;
3. retain the provider message ID and received timestamp;
4. capture available SPF, DKIM, and DMARC results without overstating what they prove;
5. hash attachments and deduplicate retries;
6. extract only the minimum claim needed by the downstream action;
7. store the original body/attachment privately and emit a reference, not the full private message;
8. publish the candidate `ContextEvent` to Kafka;
9. support revocation, per-account deletion policy, and tenant isolation.

Sender display names and producer-supplied `source_type=official_email` are not authentication. A production source policy must use the connection identity stamped by the trusted adapter plus an owner-managed sender/domain allowlist.

## Artifact intake boundary

The local application can inspect bounded text files and text-layer documents without saving their bytes. Images receive a content-addressed receipt and require OCR or explicit human transcription. This is intentional: the fallback must say `OCR_REQUIRED` instead of claiming it saw text it did not extract.

An event created from an upload defaults to `UNVERIFIED`. A human or authenticated connector may provide source metadata, but only trusted ingestion policy can promote authority in production.
