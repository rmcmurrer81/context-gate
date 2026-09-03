-- Agent output is written to a separate branch. Deterministic decisions already exist.
-- SQL performs a first guard; the Streamlit/Pydantic boundary performs full validation.

CREATE VIEW raw_context_agent_outputs AS
SELECT
  d.request_id,
  d.decision_id,
  d.decision,
  d.risk,
  d.classification,
  d.authoritative_value,
  d.evidence_event_ids,
  d.requires_human_approval,
  d.explanation AS fallback_explanation,
  a.status AS agent_status,
  a.response AS raw_response
FROM context_decisions AS d,
LATERAL TABLE(
  AI_RUN_AGENT(
    'context_reviewer_agent',
    d.evidence_package,
    d.request_id,
    'context_agent_logs',
    MAP['debug', 'true']
  )
) AS a(status, response);

INSERT INTO context_agent_explanations
SELECT
  request_id,
  decision_id,
  agent_status,
  raw_response,
  CASE
    WHEN agent_status = 'SUCCESS'
      AND raw_response IS JSON OBJECT
      AND JSON_VALUE(raw_response, 'strict $.decision') = decision
      AND JSON_VALUE(raw_response, 'strict $.risk') = risk
      AND JSON_VALUE(raw_response, 'strict $.classification') = classification
      AND JSON_VALUE(raw_response, 'strict $.authoritative_value') = authoritative_value
      AND JSON_VALUE(raw_response, 'strict $.evidence_event_ids[0]') = evidence_event_ids[1]
      AND JSON_VALUE(raw_response, 'strict $.evidence_event_ids[1]') = evidence_event_ids[2]
      AND JSON_VALUE(
        raw_response,
        'strict $.requires_human_approval' RETURNING BOOLEAN
      ) = requires_human_approval
    THEN JSON_VALUE(raw_response, 'strict $.explanation')
    ELSE fallback_explanation
  END AS accepted_explanation,
  CASE
    WHEN agent_status = 'SUCCESS'
      AND raw_response IS JSON OBJECT
      AND JSON_VALUE(raw_response, 'strict $.decision') = decision
      AND JSON_VALUE(raw_response, 'strict $.risk') = risk
      AND JSON_VALUE(raw_response, 'strict $.classification') = classification
      AND JSON_VALUE(raw_response, 'strict $.authoritative_value') = authoritative_value
      AND JSON_VALUE(raw_response, 'strict $.evidence_event_ids[0]') = evidence_event_ids[1]
      AND JSON_VALUE(raw_response, 'strict $.evidence_event_ids[1]') = evidence_event_ids[2]
      AND JSON_VALUE(
        raw_response,
        'strict $.requires_human_approval' RETURNING BOOLEAN
      ) = requires_human_approval
    THEN 'SQL_GUARD_PASSED_REQUIRE_PYDANTIC_VALIDATION'
    ELSE 'FALLBACK_USED'
  END AS validation_status,
  CURRENT_TIMESTAMP AS created_at
FROM raw_context_agent_outputs;
