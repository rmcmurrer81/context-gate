-- Agent runtime trace. Valid types include CHAT_MESSAGES, MODEL_RESPONSE,
-- TOOL_CALL_REQUEST, TOOL_RESPONSE, and ERROR.

SELECT
  request_id,
  iteration,
  `type`,
  `data`,
  metrics
FROM context_agent_logs
WHERE request_id = 'req-201'
ORDER BY iteration ASC;

SELECT *
FROM context_agent_explanations
WHERE request_id = 'req-201';
SELECT *
FROM context_decisions
WHERE request_id = 'req-201';
