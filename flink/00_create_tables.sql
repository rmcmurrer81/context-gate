-- ContextGate Confluent Cloud tables.
-- Event-day prerequisite: select the organizer-provided catalog and database.
-- Do not paste credentials into SQL files.

CREATE TABLE IF NOT EXISTS context_events (
  event_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  field_name STRING NOT NULL,
  field_value STRING NOT NULL,
  source_name STRING,
  source_type STRING,
  trust_score DOUBLE,
  observed_at TIMESTAMP_LTZ(3),
  effective_at TIMESTAMP_LTZ(3),
  sensitivity STRING,
  evidence_uri STRING,
  evidence_reference STRING,
  content_hash STRING,
  `status` STRING
) WITH (
  'value.format' = 'json-registry'
);
CREATE TABLE IF NOT EXISTS action_requests (
  request_id STRING NOT NULL,
  action_id STRING NOT NULL,
  action_type STRING NOT NULL,
  entity_id STRING NOT NULL,
  field_name STRING NOT NULL,
  requested_value STRING NOT NULL,
  supporting_event_id STRING,
  requested_effective_at TIMESTAMP_LTZ(3),
  sensitivity STRING,
  consequential BOOLEAN,
  created_at TIMESTAMP_LTZ(3)
) WITH (
  'value.format' = 'json-registry'
);

-- Ranking is an updating query, so authoritative state must be an upsert table.
CREATE TABLE IF NOT EXISTS authoritative_context (
  entity_id STRING NOT NULL,
  field_name STRING NOT NULL,
  event_id STRING NOT NULL,
  field_value STRING NOT NULL,
  normalized_value STRING NOT NULL,
  source_name STRING NOT NULL,
  source_type STRING NOT NULL,
  trust_score DOUBLE NOT NULL,
  source_policy_rank INT NOT NULL,
  effective_trust DOUBLE NOT NULL,
  authority_score DOUBLE NOT NULL,
  observed_at TIMESTAMP_LTZ(3),
  effective_at TIMESTAMP_LTZ(3),
  evidence_reference STRING,
  content_hash STRING NOT NULL,
  verification_status STRING NOT NULL,
  sensitivity STRING,
  PRIMARY KEY (entity_id, field_name) NOT ENFORCED
) WITH (
  'changelog.mode' = 'upsert',
  'value.format' = 'json-registry'
);

-- Deterministic decisions are the durable enforcement record, independent of the model.
CREATE TABLE IF NOT EXISTS context_decisions (
  decision_id STRING NOT NULL,
  run_id STRING NOT NULL,
  request_id STRING NOT NULL,
  action_id STRING NOT NULL,
  action_type STRING NOT NULL,
  requested_value STRING NOT NULL,
  supporting_event_id STRING,
  classification STRING NOT NULL,
  decision STRING NOT NULL,
  risk STRING NOT NULL,
  authoritative_value STRING,
  evidence_event_ids ARRAY<STRING>,
  explanation STRING NOT NULL,
  deterministic_rule_ids ARRAY<STRING> NOT NULL,
  requires_human_approval BOOLEAN NOT NULL,
  review_status STRING NOT NULL,
  evidence_package STRING NOT NULL,
  created_at TIMESTAMP_LTZ(3) NOT NULL
) WITH (
  'value.format' = 'json-registry'
);

CREATE TABLE IF NOT EXISTS review_events (
  review_id STRING NOT NULL,
  decision_id STRING NOT NULL,
  request_id STRING NOT NULL,
  review_action STRING NOT NULL,
  resulting_status STRING NOT NULL,
  reviewer STRING NOT NULL,
  rationale STRING NOT NULL,
  action_executed BOOLEAN NOT NULL,
  created_at TIMESTAMP_LTZ(3) NOT NULL
) WITH (
  'value.format' = 'json-registry'
);

-- Agent explanations stay separate so model latency/failure cannot block enforcement.
CREATE TABLE IF NOT EXISTS context_agent_explanations (
  request_id STRING NOT NULL,
  decision_id STRING NOT NULL,
  agent_status STRING NOT NULL,
  raw_response STRING,
  accepted_explanation STRING NOT NULL,
  validation_status STRING NOT NULL,
  created_at TIMESTAMP_LTZ(3) NOT NULL
) WITH (
  'value.format' = 'json-registry'
);

-- Fixed schema required by the Confluent Streaming Agent runtime. Do not add columns.
CREATE TABLE IF NOT EXISTS context_agent_logs (
  agent_name STRING NOT NULL,
  job_id STRING NOT NULL,
  request_id STRING NOT NULL,
  statement_name STRING NOT NULL,
  iteration INT NOT NULL,
  `type` STRING NOT NULL,
  `data` STRING NOT NULL,
  metrics STRING
) WITH (
  'value.format' = 'avro-registry'
);
