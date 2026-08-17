-- Dashboard: Monthly Completed Tickets by Employee
-- Uses assigned_to to determine ticket owner
-- Uses resolved_at (incidents) / closed_at (tasks) for date filtering
-- Timestamps converted to Asia/Shanghai (CST) to match ServiceNow display
-- Manual name overrides for employees missing from sys_user

WITH employees AS (
  SELECT EXPLODE(ARRAY(
    'BLiu60', 'AGuo22', 'AJian3', 'JDen4', 'HTan3', 'HFeng1',
    'LCh158', 'TTao5', 'L31', 'CLe144', 'RJu1', 'AXu72',
    'YWa456', 'HYip2', 'JCh603', 'KChu17', 'ALan2',
    'KOuYan', 'VCHE11', 'YWei29', 'LXIAN2',
    'JQIANG', 'HZhu8', 'DCha49', 'PWan61', 'YDin23', 'XZh302',
    'FL22', 'YY7', 'YZh33', 'Z36', 'GYo2',
    'HZh8', 'PZh105'
  )) AS employee_id
),
manual_names AS (
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
incident_counts AS (
  SELECT n.employee_id, COUNT(DISTINCT i.number) AS cnt
  FROM published_domain.rese_prd_servicenow.incident i
  JOIN employee_names n ON i.assigned_to = n.employee_name
  WHERE i.state IN ('Resolved', 'Closed')
    AND YEAR(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) = YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
    AND MONTH(from_utc_timestamp(i.resolved_at, 'Asia/Shanghai')) = MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
  GROUP BY n.employee_id
),
task_counts AS (
  SELECT n.employee_id, COUNT(DISTINCT t.number) AS cnt
  FROM published_domain.rese_prd_servicenow.sc_task t
  JOIN employee_names n ON t.assigned_to = n.employee_name
  WHERE t.state IN ('Resolved','Closed','Closed Complete','Closed Incomplete')
    AND YEAR(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')) = YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
    AND MONTH(from_utc_timestamp(t.closed_at, 'Asia/Shanghai')) = MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))
  GROUP BY n.employee_id
)
SELECT
  e.employee_id,
  COALESCE(n.employee_name, e.employee_id) AS employee_name,
  COALESCE(i.cnt, 0) AS incident_count,
  COALESCE(t.cnt, 0) AS task_count,
  COALESCE(i.cnt, 0) + COALESCE(t.cnt, 0) AS total_count
FROM employees e
LEFT JOIN employee_names n ON e.employee_id = n.employee_id
LEFT JOIN incident_counts i ON e.employee_id = i.employee_id
LEFT JOIN task_counts t ON e.employee_id = t.employee_id
ORDER BY total_count DESC, e.employee_id ASC
