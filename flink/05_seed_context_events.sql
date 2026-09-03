-- Publish evidence first, after the normalization/authority statements are running.
-- Fully synthetic data; no real event or personal details.

INSERT INTO context_events VALUES
(
  'evt-100', 'nova-summit', 'venue', '10 Innovation Street',
  'Official Confirmation', 'registration_confirmation', 0.98,
  TO_TIMESTAMP_LTZ(1788267600000, 3),
  TO_TIMESTAMP_LTZ(1788438600000, 3),
  'public', NULL, 'synthetic-confirmation://nova-summit/evt-100',
  '78d4fc9d66b0ab2596d3271aec35429fd2ee9d25f2a99bd08e7efe7848a67f50',
  'confirmed'
),
(
  'evt-104', 'nova-summit', 'venue', '2 Innovation Street',
  'Community Event Listing', 'copied_webpage', 0.62,
  TO_TIMESTAMP_LTZ(1788373800000, 3),
  TO_TIMESTAMP_LTZ(1788438600000, 3),
  'public', NULL, 'synthetic-listing://nova-summit/evt-104',
  'a134a88079f4625372469fd05172579e159b5bc86f8954f5fa454df496a7d684',
  'unverified'
);
