-- MVP scope: deterministic ranking for the Nova conflict scenario.
-- Near-peer ties must be routed to REVIEW before claiming broader production use.

CREATE VIEW authoritative_context_view AS
SELECT
  entity_id,
  field_name,
  event_id,
  field_value,
  normalized_value,
  source_name,
  source_type,
  trust_score,
  source_policy_rank,
  effective_trust,
  authority_score,
  observed_at,
  effective_at,
  evidence_reference,
  content_hash,
  verification_status,
  sensitivity
FROM (
  SELECT
    entity_id,
    field_name,
    event_id,
    field_value,
    normalized_value,
    source_name,
    source_type,
    trust_score,
    source_policy_rank,
    effective_trust,
    authority_score,
    observed_at,
    effective_at,
    evidence_reference,
    content_hash,
    verification_status,
    sensitivity,
    ROW_NUMBER() OVER (
      PARTITION BY entity_id, field_name
      ORDER BY authority_score DESC
    ) AS rownum
  FROM deduplicated_context_events
)
WHERE rownum = 1;

-- Submit this INSERT as a continuous Flink statement.
INSERT INTO authoritative_context
SELECT
  entity_id,
  field_name,
  event_id,
  field_value,
  normalized_value,
  source_name,
  source_type,
  trust_score,
  source_policy_rank,
  effective_trust,
  authority_score,
  observed_at,
  effective_at,
  evidence_reference,
  content_hash,
  verification_status,
  sensitivity
FROM authoritative_context_view;
