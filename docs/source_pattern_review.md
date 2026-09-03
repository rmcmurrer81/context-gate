# Read-only source-pattern review

The separate reference project was reviewed only for generic engineering patterns. No reference file was modified, copied into this repository, or committed here; no personal records or identity-specific data were reused.

Neutral patterns carried into ContextGate:

- Treat extracted claims as evidence, not automatically trusted state.
- Keep source authority, confidence, evidence identity, and creation time attached to a claim.
- Reduce confidence or escalate when sources conflict.
- Do not present unsupported reconstruction as confirmed fact.
- Separate a proposed action from an approved or executed action.
- Require explicit enablement and review gates for consequential behavior.
- Bind decisions to exact request/evidence identifiers and reject duplicate processing.
- Validate adapter/model output against a strict schema and bounded fields.
- Keep model output outside the trusted enforcement boundary.
- Append decision receipts to an audit record instead of treating them as memory.
- Expose public-safe receipts that explicitly say when no external action occurred.

ContextGate rewrites these ideas under neutral event, evidence, policy, decision, and review terminology.
