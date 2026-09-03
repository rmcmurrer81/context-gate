# Two-minute demo script

## 0:00–0:20 — The problem

“AI agents act on a world that keeps changing. They often treat the newest fact as the true fact—even when it came from a copied page. ContextGate is a real-time context firewall: evidence and approval control before an agent acts.”

## 0:20–0:45 — Stream the conflict

Click **Replay incoming stream**.

“Here an official registration confirmation says the fictional Nova AI Summit is at 10 Innovation Street. A later community listing says 2 Innovation Street. Then an agent asks to update the calendar to the later value.”

Point out both evidence cards, timestamps, source ranks, and trust caps.

## 0:45–1:10 — Show deterministic enforcement

“Flink maintains the authoritative context. Source authority beats simple recency, so rule CG-002 emits `CONFLICT`, `BLOCK`, and `HIGH` before any LLM runs. The official value remains authoritative and both evidence IDs are attached.”

Show the decision topic or saved Confluent output, then the red local result banner.

## 1:10–1:30 — Show the agent safely

“A Confluent Streaming Agent turns that evidence package into a plain-language explanation and writes its trace to the system log. It is not the policy engine. If it fails, returns malformed JSON, or says ALLOW, ContextGate discards it and keeps the deterministic template.”

Expand **Enforcement trace** or show `context_agent_logs`.

## 1:30–1:50 — Human review and audit

Choose **Reject**.

“The reviewer can hold, reject, or record an explicit human override. Reviews are new events; they never rewrite the original BLOCK and this demo never performs a real calendar update.”

Expand **Replayable audit log** and show the verified hash chain.

## 1:50–2:00 — Close

“Kafka is the evidence spine, Flink is the deterministic gate, and the Streaming Agent is the grounded explainer. When the model or Wi-Fi disappears, the safety decision still works.”

## Likely judge questions

**Who assigns trust?** ContextGate policy assigns the source rank and caps producer-submitted trust. Producers cannot declare themselves official.

**Is this hardcoded to block?** No. A matching, non-consequential request is `SAFE / ALLOW`; consequential or sensitive actions still require review. The test suite covers the paths.

**Can approval erase the block?** No. It appends a `HUMAN_OVERRIDE` receipt linked to the original decision. The evidence and BLOCK remain replayable.

**Why Confluent?** Authority changes over time. Kafka preserves the evidence history; Flink continuously resolves state and evaluates requests as-of event time; Streaming Agents explain decisions against live context.
