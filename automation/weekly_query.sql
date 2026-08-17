-- Weekly Breakdown: Tickets per employee per week (Mon-Sun) for current month
-- week_start = the Monday of each week
-- MOD(DAYOFWEEK(d)+5, 7) maps Sun=1..Sat=7 → offset 6,0,1,2,3,4,5

WITH manual_names AS (
  SELECT 'HTan3' AS employee_id, 'Howie Tan' AS employee_name
  UNION ALL SELECT 'AJian3', 'Aaron Jiang'
),
sys_user_names AS (
  SELECT u.user_name AS employee_id, u.name AS employee_name
  FROM published_domain.rese_prd_servicenow.sys_user u
  WHERE u.user_name IN (
    'BLiu60','AGuo22','JDen4','HFeng1',
    'LCh158','TTao5','L31','CLe144','RJu1','AXu72',
    'YWa456','HYip2','JCh603','KChu17','ALan2',
    'KOuYan','VCHE11','YWei29','LXIAN2',
    'JQIANG','HZhu8','DCha49','PWan61','YDin23','XZh302',
    'FL22','YY7','YZh33','Z36','GYo2',
    'HZh8','PZh105'
  )
),
employee_names AS (
  SELECT * FROM manual_names
  UNION ALL
  SELECT * FROM sys_user_names
),
incident_weekly AS (
  SELECT
    n.employee_id,
    DATE_SUB(
      DATE(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')),
      MOD(DAYOFWEEK(DATE(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai'))) + 5, 7)
    ) AS week_start,
    COUNT(DISTINCT i.number) AS cnt
  FROM published_domain.rese_prd_servicenow.incident i
  JOIN employee_names n ON i.assigned_to = n.employee_name
  WHERE i.state IN ('Resolved', 'Closed')
    AND YEAR(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) = YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
    AND MONTH(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) = MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
  GROUP BY n.employee_id, week_start
),
task_weekly AS (
  SELECT
    n.employee_id,
    DATE_SUB(
      DATE(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')),
      MOD(DAYOFWEEK(DATE(from_utc_timestamp(t.closed_at, 'Asia/Shanghai'))) + 5, 7)
    ) AS week_start,
    COUNT(DISTINCT t.number) AS cnt
  FROM published_domain.rese_prd_servicenow.sc_task t
  JOIN employee_names n ON t.assigned_to = n.employee_name
  WHERE t.state IN ('Resolved','Closed','Closed Complete','Closed Incomplete')
    AND YEAR(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')) = YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
    AND MONTH(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')) = MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
  GROUP BY n.employee_id, week_start
)
SELECT
  employee_id,
  week_start,
  SUM(incident_count) AS incident_count,
  SUM(task_count) AS task_count,
  SUM(incident_count) + SUM(task_count) AS total_count
FROM (
  SELECT employee_id, week_start, cnt AS incident_count, 0 AS task_count FROM incident_weekly
  UNION ALL
  SELECT employee_id, week_start, 0 AS incident_count, cnt AS task_count FROM task_weekly
) combined
GROUP BY employee_id, week_start
ORDER BY week_start, employee_id
