-- Normalize fields and apply the synthetic demo policy.
-- Production must stamp an allowlisted source identity in an authenticated connector
-- or topic; source_type/status in this public JSON fixture are not authentication.
-- Rank is ordered separately downstream so producer trust_score cannot outrank it.

CREATE VIEW normalized_context_events AS
SELECT
  e.`$rowtime` AS ingestion_time,
  TRIM(event_id) AS event_id,
  LOWER(TRIM(entity_id)) AS entity_id,
  LOWER(TRIM(field_name)) AS field_name,
  TRIM(field_value) AS field_value,
  LOWER(TRIM(field_value)) AS normalized_value,
  TRIM(source_name) AS source_name,
  LOWER(TRIM(source_type)) AS source_type,
  trust_score,
  observed_at,
  effective_at,
  LOWER(TRIM(sensitivity)) AS sensitivity,
  evidence_uri,
  evidence_reference,
  content_hash,
  UPPER(TRIM(`status`)) AS verification_status,
  CASE LOWER(TRIM(source_type))
    WHEN 'registration_confirmation' THEN 300
    WHEN 'organizer_api' THEN 298
    WHEN 'organizer_website' THEN 295
    WHEN 'official_email' THEN 292
    WHEN 'partner_website' THEN 270
    WHEN 'copied_webpage' THEN 250
    WHEN 'user_report' THEN 240
    ELSE 210
  END AS source_policy_rank,
  CASE LOWER(TRIM(source_type))
    WHEN 'registration_confirmation' THEN LEAST(trust_score, 1.00)
    WHEN 'organizer_api' THEN LEAST(trust_score, 1.00)
    WHEN 'organizer_website' THEN LEAST(trust_score, 0.98)
    WHEN 'official_email' THEN LEAST(trust_score, 0.98)
    WHEN 'partner_website' THEN LEAST(trust_score, 0.85)
    WHEN 'copied_webpage' THEN LEAST(trust_score, 0.70)
    WHEN 'user_report' THEN LEAST(trust_score, 0.65)
    ELSE LEAST(trust_score, 0.40)
  END AS effective_trust,
  CAST(
    CASE LOWER(TRIM(source_type))
      WHEN 'registration_confirmation' THEN 300
      WHEN 'organizer_api' THEN 298
      WHEN 'organizer_website' THEN 295
      WHEN 'official_email' THEN 292
      WHEN 'partner_website' THEN 270
      WHEN 'copied_webpage' THEN 250
      WHEN 'user_report' THEN 240
      ELSE 210
    END
    + CASE WHEN UPPER(TRIM(`status`)) = 'CONFIRMED' THEN 10 ELSE 0 END
    + CASE LOWER(TRIM(source_type))
        WHEN 'registration_confirmation' THEN LEAST(trust_score, 1.00)
        WHEN 'organizer_api' THEN LEAST(trust_score, 1.00)
        WHEN 'organizer_website' THEN LEAST(trust_score, 0.98)
        WHEN 'official_email' THEN LEAST(trust_score, 0.98)
        WHEN 'partner_website' THEN LEAST(trust_score, 0.85)
        WHEN 'copied_webpage' THEN LEAST(trust_score, 0.70)
        WHEN 'user_report' THEN LEAST(trust_score, 0.65)
        ELSE LEAST(trust_score, 0.40)
      END
    AS DOUBLE
  ) AS authority_score
FROM context_events AS e;
