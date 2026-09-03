# Event-day checklist — September 3, 2026

## Before judging eligibility

- Ask staff whether a pre-event prototype or reusable code is eligible. The public NYC page publishes no pre-existing-code policy.
- Ask whether entries are individual or teams, how many people may be on a team, and whether “one entry per person” limits team submissions.
- Ask for the actual judging rubric, submission method, deadline, and IP/license terms.
- Keep the pre-event Git checkpoint and describe tonight's work honestly as the local prototype/foundation.

## Arrival

- Plan for the published 8:30 a.m. registration start even though the event headline says 9:00 a.m.
- Use the confirmation email as the arrival authority; the public page contains inconsistent street numbers for the same venue complex.
- Bring power, charger, hotspot, and a local copy of the repository.

## Workshop facts to capture

- Catalog and database names.
- Registered model identifier and provider.
- Whether Streaming Agents are enabled.
- Flink permissions/RBAC and topic-write access.
- Supported topic/table formats.
- Any organizer naming prefix or cleanup requirement.
- Whether the five-argument `AI_RUN_AGENT` overload and agent system logs are enabled.

Never write credentials into source, SQL, screenshots, or slides.

## Build sequence

- Run local tests before changing anything.
- Create tables and views in the supplied environment.
- Start authoritative-state materialization.
- Insert `evt-100` and `evt-104`; prove `evt-100` wins.
- Start deterministic decisions and only then insert `req-201`.
- Prove `CONFLICT / BLOCK / HIGH` in `context_decisions` without a model.
- Create the Streaming Agent using the organizer's actual model identifier.
- Start the explanation branch and verify `context_agent_logs`.
- Confirm malformed or contradicting output falls back.
- Wire the UI only after topic rows are correct.

## Freeze and rehearse

- Stop adding features by 3:15 p.m.; the current agenda begins demos at 4:00 p.m.
- Capture screenshots/exported rows of the working cloud path.
- Rehearse the two-minute script twice.
- Keep the local fallback already open in another terminal.
- Run `python -m pytest` one final time.

## Cleanup

- Use only the organizer-approved cleanup process.
- Remove any resources you personally provisioned.
- Do not delete shared workshop resources.
- Revoke personal model/API keys if any were used.
