-- Confluent requires this exact ROW_NUMBER + rownum = 1 pattern for deduplication.

CREATE VIEW deduplicated_context_events AS
SELECT
  ingestion_time,
  event_id,
  entity_id,
  field_name,
  field_value,
  normalized_value,
  source_name,
  source_type,
  trust_score,
  observed_at,
  effective_at,
  sensitivity,
  evidence_uri,
  evidence_reference,
  content_hash,
  verification_status,
  source_policy_rank,
  effective_trust,
  authority_score
FROM (
  SELECT
    ingestion_time,
    event_id,
    entity_id,
    field_name,
    field_value,
    normalized_value,
    source_name,
    source_type,
    trust_score,
    observed_at,
    effective_at,
    sensitivity,
    evidence_uri,
    evidence_reference,
    content_hash,
    verification_status,
    source_policy_rank,
    effective_trust,
    authority_score,
    ROW_NUMBER() OVER (
      PARTITION BY entity_id, field_name, normalized_value, source_name, content_hash
      ORDER BY ingestion_time ASC
    ) AS rownum
  FROM normalized_context_events
  WHERE content_hash IS NOT NULL
    AND observed_at IS NOT NULL
    AND effective_at IS NOT NULL
    AND source_name IS NOT NULL
    AND source_type IS NOT NULL
    AND trust_score BETWEEN 0.0 AND 1.0
)
WHERE rownum = 1;
