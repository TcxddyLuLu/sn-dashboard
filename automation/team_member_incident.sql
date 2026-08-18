-- Per-employee closed incidents for one month (small point lookup).

SELECT
  '__EMPLOYEE_ID__' AS employee_id,
  '__EMPLOYEE_NAME__' AS employee_name,
  i.number AS ticket_number,
  'incident' AS ticket_type,
  DATE(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) AS closed_date
FROM published_domain.rese_prd_servicenow.incident i
WHERE i.assigned_to = '__EMPLOYEE_NAME__'
  AND i.state IN ('Resolved', 'Closed')
  AND i.resolved_at IS NOT NULL
  AND i.resolved_at >= TIMESTAMP '__MONTH_START_UTC__'
  AND i.resolved_at < TIMESTAMP '__MONTH_END_UTC__'
