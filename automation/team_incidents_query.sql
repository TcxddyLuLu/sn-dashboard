-- Closed incidents for team members in one calendar month (CST).
-- Uses assigned_to display name (same join path as fast summary queries).

WITH manual_names AS (
  SELECT 'HTan3' AS employee_id, 'Howie Tan' AS employee_name
  UNION ALL SELECT 'AJian3', 'Aaron Jiang'
),
sys_user_names AS (
  SELECT u.user_name AS employee_id, u.name AS employee_name
  FROM published_domain.rese_prd_servicenow.sys_user u
  WHERE u.user_name IN (__EMPLOYEE_ID_LIST__)
),
employee_names AS (
  SELECT * FROM manual_names
  UNION ALL
  SELECT * FROM sys_user_names
)
SELECT
  n.employee_id,
  n.employee_name,
  i.number AS ticket_number,
  'incident' AS ticket_type,
  DATE(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) AS closed_date
FROM published_domain.rese_prd_servicenow.incident i
INNER JOIN employee_names n ON i.assigned_to = n.employee_name
WHERE i.state IN ('Resolved', 'Closed')
  AND i.resolved_at IS NOT NULL
  AND i.resolved_at >= TIMESTAMP '__MONTH_START_UTC__'
  AND i.resolved_at < TIMESTAMP '__MONTH_END_UTC__'
