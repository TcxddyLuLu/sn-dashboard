-- Per-employee closed SC tasks for one month (small point lookup).

SELECT
  '__EMPLOYEE_ID__' AS employee_id,
  '__EMPLOYEE_NAME__' AS employee_name,
  t.number AS ticket_number,
  'task' AS ticket_type,
  DATE(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')) AS closed_date
FROM published_domain.rese_prd_servicenow.sc_task t
WHERE t.assigned_to = '__EMPLOYEE_NAME__'
  AND t.state IN ('Resolved', 'Closed', 'Closed Complete', 'Closed Incomplete')
  AND t.closed_at IS NOT NULL
  AND t.closed_at >= TIMESTAMP '__MONTH_START_UTC__'
  AND t.closed_at < TIMESTAMP '__MONTH_END_UTC__'
