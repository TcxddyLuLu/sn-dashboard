-- Team ticket details for one calendar month (CST), filtered by assignee sys_id.
-- Returns one row per closed incident/sc_task; aggregation happens in Python.

WITH team AS (
  SELECT employee_id, sys_id, employee_name
  FROM (
    SELECT
      u.user_name AS employee_id,
      u.sys_id,
      u.name AS employee_name
    FROM published_domain.rese_prd_servicenow.sys_user u
    WHERE upper(u.user_name) IN (__UPPER_EMPLOYEE_IDS__)
    UNION ALL
    SELECT 'HTan3', 'f51b6d83884c0240b98be14246412913', 'Howie Tan'
    UNION ALL
    SELECT 'AJian3', '111e1af70f797100f133983be1050e51', 'Aaron Jiang'
  ) members
  WHERE sys_id IS NOT NULL AND trim(sys_id) != ''
  GROUP BY employee_id, sys_id, employee_name
),
incidents AS (
  SELECT
    t.employee_id,
    t.employee_name,
    i.number AS ticket_number,
    'incident' AS ticket_type,
    DATE(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) AS closed_date
  FROM published_domain.rese_prd_servicenow.incident i
  INNER JOIN team t ON i.assigned_to_id = t.sys_id
  WHERE i.state IN ('Resolved', 'Closed')
    AND i.resolved_at IS NOT NULL
    AND i.resolved_at >= to_utc_timestamp(__MONTH_START_DATE__, 'Asia/Shanghai')
    AND i.resolved_at < to_utc_timestamp(__MONTH_END_DATE__, 'Asia/Shanghai')
),
tasks AS (
  SELECT
    t.employee_id,
    t.employee_name,
    tsk.number AS ticket_number,
    'task' AS ticket_type,
    DATE(from_utc_timestamp(tsk.closed_at, 'Asia/Shanghai')) AS closed_date
  FROM published_domain.rese_prd_servicenow.sc_task tsk
  INNER JOIN team t ON tsk.assigned_to_id = t.sys_id
  WHERE tsk.state IN ('Resolved', 'Closed', 'Closed Complete', 'Closed Incomplete')
    AND tsk.closed_at IS NOT NULL
    AND tsk.closed_at >= to_utc_timestamp(__MONTH_START_DATE__, 'Asia/Shanghai')
    AND tsk.closed_at < to_utc_timestamp(__MONTH_END_DATE__, 'Asia/Shanghai')
)
SELECT employee_id, employee_name, ticket_number, ticket_type, closed_date
FROM incidents
UNION ALL
SELECT employee_id, employee_name, ticket_number, ticket_type, closed_date
FROM tasks
