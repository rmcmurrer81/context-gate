-- Publish only after authoritative_context contains evt-100.

INSERT INTO action_requests VALUES
(
  'req-201', 'act-201', 'update_calendar', 'nova-summit', 'venue',
  '2 Innovation Street', 'evt-104',
  TO_TIMESTAMP_LTZ(1788438600000, 3),
  'public', TRUE,
  TO_TIMESTAMP_LTZ(1788375600000, 3)
);
