# Team Notifications — DB Setup & Test Runbook

Run these in order against the shared MySQL DB (`ats_staging`).

---

## 1. Apply migrations (in order)

```bash
mysql -u hrmis-hti -p ats_staging < migrations/create_notification_tables.sql
mysql -u hrmis-hti -p ats_staging < migrations/v2_delivery_modes_and_banners.sql
mysql -u hrmis-hti -p ats_staging < migrations/v3_upgrade_job_candidate_events.sql
mysql -u hrmis-hti -p ats_staging < migrations/v4_refine_banner_templates.sql
mysql -u hrmis-hti -p ats_staging < migrations/v5_performance_indexes.sql
mysql -u hrmis-hti -p ats_staging < migrations/v6_remove_candidate_name_from_closure_templates.sql
mysql -u hrmis-hti -p ats_staging < migrations/v7_team_events.sql
```

For a fresh DB, all 7 are required. For an existing DB, only run the ones not yet applied (each is idempotent — safe to re-run).

---

## 2. Verify the team events were inserted

```sql
SELECT id, event_name, delivery_mode, also_banner, target_type, target_roles
FROM notification_events
WHERE event_name LIKE 'team_%'
ORDER BY event_name;
```

Expected rows:

| event_name | delivery_mode | also_banner | target_type | target_roles |
|---|---|---|---|---|
| team_assigned_to_job | push | 1 | user | NULL |
| team_created | push | 1 | role | super_admin,admin |
| team_deleted | push | 1 | role | super_admin,admin |
| team_members_added | push | 0 | user | NULL |
| team_members_removed | push | 0 | user | NULL |
| team_removed_from_job | push | 0 | user | NULL |
| team_updated | push | 0 | role | super_admin,admin |

---

## 3. Verify the teams + team_members tables exist

These should already exist (created earlier by the RBAC service migration). Quick check:

```sql
DESCRIBE teams;
DESCRIBE team_members;
DESCRIBE job_team_assignments;
```

If any are missing — run the RBAC service's bootstrap SQL first.

---

## 4. Sample API calls to fire each event

### A. Create a team (RBAC) → fires `team_created` + `team_members_added` (per initial member)

```bash
curl -X POST 'http://api-gateway/rbac/teams' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Backend Engineering",
    "description": "Owns API + DB",
    "department": "Engineering",
    "status": "active",
    "manager_ids": [5],
    "member_ids": [12, 16, 23]
  }'
```

WebSocket events you should see:
- Admins: `{"type":"notification","data":{...team_created...}}`
- Admins: `{"type":"banner","action":"create","data":{...}}` + `{"type":"banners","action":"snapshot",...}`
- Users 5, 12, 16, 23: `{"type":"notification","data":{...team_members_added...}}`

---

### B. Add members to a team → fires `team_members_added`

```bash
curl -X POST 'http://api-gateway/rbac/teams/1/members' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_ids": [42, 43],
    "role_in_team": "member"
  }'
```

Users 42, 43 receive push: *"You have been added to team 'Backend Engineering' by Admin User."*

---

### C. Remove members from a team → fires `team_members_removed`

```bash
curl -X DELETE 'http://api-gateway/rbac/teams/1/members' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "user_ids": [42]
  }'
```

User 42 receives push: *"You have been removed from team 'Backend Engineering' by Admin User."*

---

### D. Update a team → fires `team_updated`

```bash
curl -X PUT 'http://api-gateway/rbac/teams/1' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Backend Platform",
    "description": "Platform team",
    "status": "active"
  }'
```

Admins receive push: *"Admin User updated team 'Backend Platform'."*

---

### E. Delete a team → fires `team_deleted` + `team_members_removed` (per member)

```bash
curl -X DELETE 'http://api-gateway/rbac/teams/1' \
  -H 'Authorization: Bearer <admin_token>'
```

Note: deletion is blocked if the team is still assigned to any job. Remove all team→job assignments first.

WebSocket events:
- Admins: push + banner *"Admin User deleted team 'Backend Engineering'."*
- Each former member: push *"You have been removed from team 'Backend Engineering' by Admin User."*

---

### F. Assign team(s) to a job (Job Service) → fires `team_assigned_to_job`

```bash
curl -X POST 'http://api-gateway/api/jobs/assign/teams' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "jobId": 42,
    "teamIds": [1, 2]
  }'
```

WebSocket events:
- Members of team 1 + team 2: push *"Your team 'Backend Engineering' has been assigned to job 'Senior Engineer' by Admin User."*
- Admins: banner *"Admin User assigned team 'Backend Engineering' to job 'Senior Engineer'."*

---

### G. Remove team(s) from a job → fires `team_removed_from_job`

```bash
curl -X DELETE 'http://api-gateway/api/jobs/42/assigned-teams' \
  -H 'Authorization: Bearer <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "teamIds": [1]
  }'
```

Members of team 1 receive push: *"Your team 'Backend Engineering' has been removed from job 'Senior Engineer' by Admin User."*

---

## 5. Verify notifications were created in the DB

After firing any event:

```sql
SELECT id, title, delivery_mode, domain_type, target_type, target_id,
       source_service, event_type, created_at
FROM notifications
WHERE event_type LIKE 'team_%'
ORDER BY id DESC
LIMIT 20;
```

```sql
-- Check who received each
SELECT n.id, n.event_type, n.title,
       COUNT(nr.id) AS recipient_count,
       SUM(nr.is_read) AS read_count
FROM notifications n
LEFT JOIN notification_recipients nr ON nr.notification_id = n.id
WHERE n.event_type LIKE 'team_%'
GROUP BY n.id
ORDER BY n.id DESC
LIMIT 20;
```

---

## 6. Quick smoke test — toggle a team event

Disable an event without dropping it:
```sql
UPDATE notification_events SET is_enabled = 0 WHERE event_name = 'team_updated';
```

Re-enable:
```sql
UPDATE notification_events SET is_enabled = 1 WHERE event_name = 'team_updated';
```

Switch a team event to log-only (no badge / no toast):
```sql
UPDATE notification_events SET delivery_mode = 'log', also_banner = 0
WHERE event_name = 'team_updated';
```

---

## 7. Troubleshooting

**Issue: "Event 'team_xxx' not found or disabled" in status-service logs**
- Run v7 migration. The event isn't in the DB yet.

**Issue: Notification created but no WS event arrives**
- Check `target_type` + `target_id` resolved to actual user_ids.
- For `target_type=user`, the API caller must pass `user_ids` as a CSV string in the data payload.

**Issue: Team-add notify fires but recipient doesn't receive it**
- The `target_type=user` event resolves recipients from `data.user_ids` or `data.user_id`.
- Verify the RBAC API passed those keys correctly:
  ```sql
  SELECT id, target_type, target_id, extra_metadata FROM notifications
  WHERE event_type = 'team_members_added' ORDER BY id DESC LIMIT 5;
  ```
  `target_id` should contain the user_id(s) as a string.

**Issue: Admin doesn't get team_assigned_to_job banner**
- The banner targets `super_admin,admin` roles. Check those roles exist:
  ```sql
  SELECT u.id, u.name, r.name AS role
  FROM users u JOIN roles r ON u.role_id = r.id
  WHERE LOWER(r.name) IN ('admin','super_admin') AND u.deleted_at IS NULL;
  ```
