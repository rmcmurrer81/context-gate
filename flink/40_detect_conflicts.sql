-- The temporal join evaluates each request against authority as it existed at request time.
-- Keep the INSERT running before publishing the synthetic action request.

CREATE VIEW normalized_action_requests AS
SELECT
  r.`$rowtime` AS request_time,
  TRIM(request_id) AS request_id,
  TRIM(action_id) AS action_id,
  LOWER(TRIM(action_type)) AS action_type,
  LOWER(TRIM(entity_id)) AS entity_id,
  LOWER(TRIM(field_name)) AS field_name,
  TRIM(requested_value) AS requested_value,
  LOWER(TRIM(requested_value)) AS normalized_requested_value,
  supporting_event_id,
  requested_effective_at,
  LOWER(TRIM(sensitivity)) AS sensitivity,
  consequential,
  created_at
FROM action_requests AS r;

CREATE VIEW deterministic_context_decisions AS
SELECT
  CONCAT('dec-', r.request_id) AS decision_id,
  CONCAT('flink-', r.request_id) AS run_id,
  r.request_id,
  r.action_id,
  r.action_type,
  r.requested_value,
  r.supporting_event_id,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN 'INSUFFICIENT_EVIDENCE'
    WHEN r.requested_effective_at < a.effective_at THEN 'STALE'
    WHEN r.normalized_requested_value <> a.normalized_value THEN 'CONFLICT'
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN 'SENSITIVE'
    ELSE 'SAFE'
  END AS classification,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN 'REVIEW'
    WHEN r.requested_effective_at < a.effective_at THEN 'BLOCK'
    WHEN r.normalized_requested_value <> a.normalized_value THEN 'BLOCK'
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN 'REVIEW'
    WHEN r.consequential THEN 'REVIEW'
    ELSE 'ALLOW'
  END AS decision,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN 'HIGH'
    WHEN r.requested_effective_at < a.effective_at THEN 'HIGH'
    WHEN r.normalized_requested_value <> a.normalized_value THEN 'HIGH'
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN 'HIGH'
    WHEN r.consequential THEN 'MEDIUM'
    ELSE 'LOW'
  END AS risk,
  a.field_value AS authoritative_value,
  ARRAY[a.event_id, s.event_id] AS evidence_event_ids,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL
      THEN 'Required evidence or lineage is missing; ContextGate will not infer it.'
    WHEN r.requested_effective_at < a.effective_at
      THEN 'The requested effective time is older than the authoritative record.'
    WHEN r.normalized_requested_value <> a.normalized_value
      THEN 'The requested value conflicts with a higher-authority record.'
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public'
      THEN 'Sensitive context requires explicit human approval.'
    WHEN r.consequential
      THEN 'The value is supported, but this external action requires explicit approval.'
    ELSE 'The requested value matches the best-supported current evidence.'
  END AS explanation,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN ARRAY['CG-005-REQUIRED-PROVENANCE']
    WHEN r.requested_effective_at < a.effective_at THEN ARRAY['CG-004-STALE-EFFECTIVE-TIME']
    WHEN r.normalized_requested_value <> a.normalized_value THEN ARRAY['CG-002-LOWER-AUTHORITY-CONFLICT']
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN ARRAY['CG-006-SENSITIVE-APPROVAL']
    WHEN r.consequential THEN ARRAY['CG-008-SAFE', 'CG-007-CONSEQUENTIAL-APPROVAL']
    ELSE ARRAY['CG-008-SAFE']
  END AS deterministic_rule_ids,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN TRUE
    WHEN r.requested_effective_at < a.effective_at THEN TRUE
    WHEN r.normalized_requested_value <> a.normalized_value THEN TRUE
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN TRUE
    ELSE r.consequential
  END AS requires_human_approval,
  CASE
    WHEN a.entity_id IS NULL OR r.supporting_event_id IS NULL OR s.event_id IS NULL THEN 'PENDING'
    WHEN r.requested_effective_at < a.effective_at THEN 'PENDING'
    WHEN r.normalized_requested_value <> a.normalized_value THEN 'PENDING'
    WHEN r.sensitivity <> 'public' OR a.sensitivity <> 'public' THEN 'PENDING'
    WHEN r.consequential THEN 'PENDING'
    ELSE 'NOT_REQUIRED'
  END AS review_status,
  JSON_OBJECT(
    'request_id' VALUE r.request_id,
    'action_type' VALUE r.action_type,
    'requested_value' VALUE r.requested_value,
    'supporting_event_id' VALUE s.event_id,
    'supporting_value' VALUE s.field_value,
    'supporting_source' VALUE s.source_name,
    'supporting_evidence_reference' VALUE s.evidence_reference,
    'authoritative_event_id' VALUE a.event_id,
    'authoritative_value' VALUE a.field_value,
    'authoritative_source' VALUE a.source_name,
    'authoritative_policy_rank' VALUE a.source_policy_rank
  ) AS evidence_package,
  CURRENT_TIMESTAMP AS created_at
FROM normalized_action_requests AS r
LEFT JOIN authoritative_context FOR SYSTEM_TIME AS OF r.request_time AS a
  ON r.entity_id = a.entity_id
 AND r.field_name = a.field_name
LEFT JOIN deduplicated_context_events AS s
  ON r.supporting_event_id = s.event_id
 AND r.entity_id = s.entity_id
 AND r.field_name = s.field_name
 AND r.normalized_requested_value = s.normalized_value
 AND s.ingestion_time <= r.request_time;

-- Submit this INSERT as a continuous Flink statement.
INSERT INTO context_decisions
SELECT * FROM deterministic_context_decisions;
