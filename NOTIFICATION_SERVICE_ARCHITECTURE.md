# Notification Service - Architecture & System Design

> **Service:** AI_AGENT15_STATUS_SERVICE (Extended)  
> **Version:** 2.0  
> **Date:** 2026-04-09  
> **Author:** System Architect

---

## 1. Overview

The Notification Service is integrated into the existing Status Service (`AI_AGENT15_STATUS_SERVICE`) to provide a comprehensive real-time notification system for the HRMIS Recruitment Platform.

### Core Capabilities

- **Two delivery modes:** Push notifications (WebSocket real-time bell icon) and Banner notifications (scrolling dashboard ticker)
- **Three visibility levels:** Personal, Public, Restricted
- **Domain-typed notifications:** `login`, `jobs`, `ai`, `candidate`, `security`, `system`, `user_management` — each filterable
- **Full notification log:** Per-user read/unread tracking; admin/super_admin see ALL logs with date + type + visibility filters
- **Auto-notifications:** 59 system events across all 6 microservices trigger notifications automatically
- **Manual triggers:** Admin/super_admin can send targeted or broadcast notifications on demand
- **Scheduling:** One-time or recurring notifications with cron-like scheduling
- **Real-time delivery:** Redis Pub/Sub + WebSocket for zero-latency push

---

## 2. Architecture Diagram

```
                    ┌──────────────────────────────────────────────────┐
                    │                  FRONTEND                        │
                    │  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
                    │  │ Dashboard │  │  Banner  │  │  Notification │  │
                    │  │   Bell   │  │ Scroller │  │  Log Center   │  │
                    │  └────┬─────┘  └────┬─────┘  └──────┬────────┘  │
                    │       │             │               │            │
                    │       └─────────┬───┘───────────────┘            │
                    │                 │ WebSocket + REST               │
                    └─────────────────┼────────────────────────────────┘
                                      │
                    ┌─────────────────┼────────────────────────────────┐
                    │   API GATEWAY   │   (JWT Auth + Routing)         │
                    │   Port 8050     │   Consul Service Discovery     │
                    └─────────────────┼────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────────┐
          │        STATUS + NOTIFICATION SERVICE (Port 8515)          │
          │                                                           │
          │  ┌─────────────────────────────────────────────────────┐  │
          │  │              REST API ENDPOINTS                     │  │
          │  │                                                     │  │
          │  │  --- Manual Triggers (admin/super_admin) ---        │  │
          │  │  POST /notifications/send         (push notif)      │  │
          │  │  POST /notifications/banner       (banner notif)    │  │
          │  │                                                     │  │
          │  │  --- User Notification Log ---                      │  │
          │  │  GET  /notifications              (paginated log)   │  │
          │  │       ?type=login,jobs,ai,candidate,...             │  │
          │  │       ?visibility=personal,public,restricted        │  │
          │  │       ?date_from=2026-04-01&date_to=2026-04-09     │  │
          │  │       ?priority=critical,high                       │  │
          │  │       ?is_read=true|false                           │  │
          │  │  GET  /notifications/unread-count                   │  │
          │  │  GET  /notifications/banners/active                 │  │
          │  │                                                     │  │
          │  │  --- Actions ---                                    │  │
          │  │  PUT  /notifications/{id}/read                      │  │
          │  │  PUT  /notifications/mark-all-read                  │  │
          │  │                                                     │  │
          │  │  --- Admin Log (super_admin + admin) ---            │  │
          │  │  GET  /notifications/admin/logs   (ALL notifs)      │  │
          │  │       ?type=login,jobs,ai,candidate,...             │  │
          │  │       ?visibility=personal,public,restricted        │  │
          │  │       ?date_from=...&date_to=...                    │  │
          │  │       ?priority=...&user_id=...                     │  │
          │  │       ?source_service=login,job,candidate,...       │  │
          │  │                                                     │  │
          │  │  --- Scheduling (admin/super_admin) ---             │  │
          │  │  POST /notifications/schedule                       │  │
          │  │  GET  /notifications/schedules                      │  │
          │  │  PUT  /notifications/schedules/{id}/cancel          │  │
          │  │                                                     │  │
          │  │  --- Internal Event Trigger ---                     │  │
          │  │  POST /notifications/event        (service-to-svc)  │  │
          │  └─────────────────────────────────────────────────────┘  │
          │                                                           │
          │  ┌─────────────────────────────────────────────────────┐  │
          │  │           WEBSOCKET ENDPOINT                        │  │
          │  │  WS /ws/notifications?token=<jwt>                   │  │
          │  │  - Per-user channel  (notif:user:{uid})             │  │
          │  │  - Broadcast channel (notif:broadcast)              │  │
          │  │  - Banner channel    (notif:banner)                 │  │
          │  └──────────────────────┬──────────────────────────────┘  │
          │                         │                                  │
          │  ┌──────────────────────┼──────────────────────────────┐  │
          │  │              NOTIFICATION ENGINE                    │  │
          │  │                      │                              │  │
          │  │  ┌───────────┐  ┌────┴─────┐  ┌────────────────┐   │  │
          │  │  │  Event    │  │  Redis   │  │   Scheduler    │   │  │
          │  │  │  Handler  │  │  Pub/Sub │  │  (Background)  │   │  │
          │  │  │           │  │  Manager │  │                │   │  │
          │  │  └─────┬─────┘  └────┬─────┘  └───────┬────────┘   │  │
          │  │        │             │                 │            │  │
          │  │  ┌─────┴─────────────┴─────────────────┴────────┐  │  │
          │  │  │           WebSocket Manager                   │  │  │
          │  │  │  (Connection registry, fan-out delivery)      │  │  │
          │  │  └──────────────────────────────────────────────┘  │  │
          │  └─────────────────────────────────────────────────────┘  │
          │                                                           │
          └──────────┬──────────────────────────┬─────────────────────┘
                     │                          │
          ┌──────────┴──────────┐    ┌──────────┴──────────┐
          │      MySQL DB       │    │    Redis Stack      │
          │                     │    │                     │
          │  notifications      │    │  Pub/Sub Channels:  │
          │  notification_      │    │  notif:user:{uid}   │
          │    recipients       │    │  notif:broadcast    │
          │  notification_      │    │  notif:banner       │
          │    schedules        │    │                     │
          │  notification_      │    │  Caching:           │
          │    events           │    │  notif:unread:{uid} │
          │                     │    │  notif:banners      │
          └─────────────────────┘    └─────────────────────┘

  ┌───────────────────────────────────────────────────────────────────────┐
  │                    OTHER MICROSERVICES (event sources)                 │
  │                                                                       │
  │  Login Service (9 events) ──── POST /notifications/event ──┐          │
  │  (login ok/fail, OTP, password reset, session mgmt)        │          │
  │                                                            ▼          │
  │  Job Service (22 events) ──── POST /notifications/event ──► Notif.   │
  │  (create, assign, deadline, candidate joined/rejected)      Service   │
  │                                                            ▲          │
  │  Candidate Service (8 events) ─ POST /notifications/event─┘          │
  │  (create, update, assign, status change, remark)                      │
  │                                                                       │
  │  Resume Analyzer (14 events) ─ POST /notifications/event ──► (AI)    │
  │  (parse/match/upgrade/summarize start/complete/fail)                  │
  │                                                                       │
  │  RBAC Service (9 events) ──── POST /notifications/event ──► (user)   │
  │  (user create, role change, enable/disable, invitation)               │
  │                                                                       │
  │  Bulk Candidate (8 events) ── POST /notifications/event ──► (AI)     │
  │  (upload, validation, processing progress, completion)                │
  └───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Notification Dimensions

Every notification has three key dimensions: **visibility**, **domain type**, and **delivery mode**.

### 3.1 Visibility Levels

| Visibility | Description | Who Sees It | Example |
|------------|-------------|-------------|---------|
| `personal` | Only for the specific user(s) targeted | The targeted user(s) only. Admin/super_admin also see it in admin logs. | "Your resume parse is complete", "Candidate X assigned to you" |
| `public` | Visible to all active users | Everyone on the platform | "System maintenance at 10 PM", Banner announcements |
| `restricted` | Visible to specific roles or job-assigned users | Only users with matching role or job assignment. Admin/super_admin always included. | "Illegal login detected for user X", "Job deadline exceeded" |

**Key rule:** `super_admin` and `admin` can see **ALL** notifications in the admin log view regardless of visibility level. They act as system auditors.

### 3.2 Domain Types (Filterable)

| Domain Type | Code | Source Services | Description |
|-------------|------|-----------------|-------------|
| Login & Auth | `login` | Login Service | Login success/failure, OTP, password reset, session events |
| Jobs & Positions | `jobs` | Job Service | Job create/update, deadline, assignment, pipeline, SPOC |
| AI & Processing | `ai` | Resume Analyzer, Bulk Candidate | Resume parse/match/upgrade/summarize, bulk extraction |
| Candidates | `candidate` | Candidate Service, Job Service | Candidate create/update/assign, status change, joined/rejected |
| Security | `security` | Login Service, Resume Analyzer | Illegal login, wrong passwords, scraping detection |
| System | `system` | All services | Bulk upload complete, system announcements, scheduled |
| User Management | `user_management` | RBAC Service | User create, role change, enable/disable, invitation |

### 3.3 Delivery Modes

| Mode | Code | Display | Persistence |
|------|------|---------|-------------|
| Push Notification | `push` | Bell icon + notification center | Stored in DB, per-user read/unread |
| Banner | `banner` | Scrolling ticker on dashboard (like news/stock market) | Stored in DB, has expiration, cached in Redis |

### 3.4 Priority Levels

| Priority | Code | Usage |
|----------|------|-------|
| Critical | `critical` | Security alerts, deadline exceeded |
| High | `high` | Candidate joined, deadline approaching, wrong passwords |
| Medium | `medium` | Job created, candidate assigned, status changes |
| Low | `low` | Login activity, user info views, routine logs |

---

## 4. Database Schema Design

### 4.1 `notifications` Table (Core)

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INT | PK, AUTO_INCREMENT | Primary key |
| `title` | VARCHAR(255) | NOT NULL | Notification title |
| `message` | TEXT | NOT NULL | Notification body content |
| `delivery_mode` | VARCHAR(20) | NOT NULL | `push` or `banner` |
| `domain_type` | VARCHAR(30) | NOT NULL | `login`, `jobs`, `ai`, `candidate`, `security`, `system`, `user_management` |
| `visibility` | VARCHAR(20) | NOT NULL | `personal`, `public`, `restricted` |
| `priority` | VARCHAR(20) | NOT NULL, DEFAULT 'medium' | `low`, `medium`, `high`, `critical` |
| `target_type` | VARCHAR(20) | NOT NULL | `all`, `user`, `job`, `role` |
| `target_id` | VARCHAR(255) | NULLABLE | Comma-separated user_ids, job internal id, or role name |
| `source_service` | VARCHAR(50) | NULLABLE | Originating service: `login`, `job`, `candidate`, `resume_analyzer`, `rbac`, `bulk_candidate`, `system` |
| `event_type` | VARCHAR(100) | NULLABLE | Auto-notification event name (e.g., `candidate_joined`) |
| `metadata` | TEXT | NULLABLE | JSON string with extra context (job_id, candidate_id, username, links, etc.) |
| `created_by` | INT | FK users.id, NULLABLE | User who created (NULL for system/auto) |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Creation timestamp |
| `expires_at` | DATETIME | NULLABLE | Expiration for banners |
| `is_active` | TINYINT(1) | DEFAULT 1 | Soft-delete / active flag |

**Indexes:**
- `idx_delivery_mode` on `delivery_mode`
- `idx_domain_type` on `domain_type`
- `idx_visibility` on `visibility`
- `idx_priority` on `priority`
- `idx_source_service` on `source_service`
- `idx_event_type` on `event_type`
- `idx_created_at` on `created_at`
- `idx_is_active` on `is_active`
- `idx_target_type` on `target_type`
- Composite: `idx_filter_combo` on (`domain_type`, `visibility`, `created_at`, `is_active`) for admin log queries

### 4.2 `notification_recipients` Table (Per-User Delivery + Read Tracking)

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INT | PK, AUTO_INCREMENT | Primary key |
| `notification_id` | INT | FK notifications.id, NOT NULL | Parent notification |
| `user_id` | INT | FK users.id, NOT NULL | Recipient user |
| `is_read` | TINYINT(1) | DEFAULT 0 | Read status per user |
| `read_at` | DATETIME | NULLABLE | When user read it |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Delivery timestamp |

**Indexes:**
- `idx_user_read` on (`user_id`, `is_read`) — for "my unread" queries
- `idx_notif_user` UNIQUE on (`notification_id`, `user_id`) — prevent duplicates
- `idx_user_created` on (`user_id`, `created_at`) — for paginated user log

### 4.3 `notification_schedules` Table

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INT | PK, AUTO_INCREMENT | Primary key |
| `title` | VARCHAR(255) | NOT NULL | Notification title |
| `message` | TEXT | NOT NULL | Notification body |
| `delivery_mode` | VARCHAR(20) | NOT NULL | `push` or `banner` |
| `domain_type` | VARCHAR(30) | NOT NULL | Domain type |
| `visibility` | VARCHAR(20) | NOT NULL | Visibility level |
| `priority` | VARCHAR(20) | DEFAULT 'medium' | Priority level |
| `target_type` | VARCHAR(20) | NOT NULL | Target type |
| `target_id` | VARCHAR(255) | NULLABLE | Target identifier |
| `metadata` | TEXT | NULLABLE | Extra JSON data |
| `scheduled_at` | DATETIME | NOT NULL | When to send |
| `repeat_type` | VARCHAR(20) | DEFAULT 'once' | `once`, `daily`, `weekly` |
| `repeat_until` | DATETIME | NULLABLE | Repeat end date |
| `status` | VARCHAR(20) | DEFAULT 'pending' | `pending`, `sent`, `cancelled` |
| `created_by` | INT | FK users.id | Creator |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Creation time |
| `last_sent_at` | DATETIME | NULLABLE | Last execution time |

**Indexes:** `idx_status_scheduled` on (`status`, `scheduled_at`), `idx_created_by`

### 4.4 `notification_events` Table (Auto-Notification Registry)

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INT | PK, AUTO_INCREMENT | Primary key |
| `event_name` | VARCHAR(100) | UNIQUE, NOT NULL | Event identifier |
| `description` | TEXT | NULLABLE | Human-readable description |
| `default_title_template` | VARCHAR(255) | NOT NULL | Python format string for title |
| `default_message_template` | TEXT | NOT NULL | Python format string for message |
| `domain_type` | VARCHAR(30) | NOT NULL | Domain type for this event |
| `visibility` | VARCHAR(20) | NOT NULL | Default visibility level |
| `target_type` | VARCHAR(20) | NOT NULL | `role`, `job`, `user`, `all` |
| `target_roles` | VARCHAR(255) | NULLABLE | Comma-separated target roles (when target_type=role) |
| `source_service` | VARCHAR(50) | NOT NULL | Which service fires this event |
| `priority` | VARCHAR(20) | DEFAULT 'medium' | Default priority |
| `delivery_mode` | VARCHAR(20) | DEFAULT 'push' | `push` or `banner` |
| `is_enabled` | TINYINT(1) | DEFAULT 1 | Enable/disable this event |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Creation time |

---

## 5. Notification Log & Filtering

### 5.1 User Notification Log (GET /notifications)

Every user can view their own notification log with filters:

| Filter | Parameter | Type | Example |
|--------|-----------|------|---------|
| Domain type | `domain_type` | comma-separated | `?domain_type=login,jobs` |
| Visibility | `visibility` | comma-separated | `?visibility=personal,public` |
| Date range from | `date_from` | ISO date | `?date_from=2026-04-01` |
| Date range to | `date_to` | ISO date | `?date_to=2026-04-09` |
| Priority | `priority` | comma-separated | `?priority=critical,high` |
| Read status | `is_read` | boolean | `?is_read=false` |
| Delivery mode | `delivery_mode` | string | `?delivery_mode=push` |
| Page | `page` | int | `?page=1` |
| Page size | `limit` | int (max 100) | `?limit=25` |
| Sort | `sort_by` | string | `?sort_by=created_at` |
| Sort order | `sort_order` | string | `?sort_order=desc` |

**What a regular user sees:**
- All `public` notifications
- `personal` notifications where they are a recipient
- `restricted` notifications where they are a recipient (resolved via job assignment or role match)
- Read/unread status is per-user

### 5.2 Admin Notification Log (GET /notifications/admin/logs)

`super_admin` and `admin` get a **complete audit view** of every notification ever sent across the entire system.

| Filter | Parameter | Type | Example |
|--------|-----------|------|---------|
| All user log filters | (same as above) | | |
| Source service | `source_service` | comma-separated | `?source_service=login,job` |
| Event type | `event_type` | string | `?event_type=illegal_login_attempt` |
| Target user | `user_id` | int | `?user_id=42` (filter notifs sent to this user) |
| Created by | `created_by` | int | `?created_by=1` (manual notifications by this admin) |

**What admin/super_admin sees:**
- **ALL** notifications regardless of visibility
- Full metadata including who sent, who received, source service, event type
- Per-recipient read/unread status
- Complete audit trail of system activity

---

## 6. Targeting System

| Target Type | Visibility | Resolution Logic |
|-------------|------------|-----------------|
| `all` | `public` | Query all users where `enable=1` and `deleted_at IS NULL` |
| `user` | `restricted` | `target_id` = comma-separated user IDs + always add super_admin/admin |
| `job` | `restricted` | Query `user_jobs_assigned` table by job internal ID + always add super_admin/admin |
| `role` | `restricted` | `target_id` = role name; query users table by role name + always add super_admin/admin |

**Global admin inclusion:** Super_admin and admin users are **always** added as recipients for **every** notification regardless of visibility or target_type. This ensures they have a complete audit trail of all system activity in their notification log. The only exception is `target_type=all` where they are already included as part of "all active users".

---

## 7. Complete Auto-Notification Event Catalog

> **Global Rule:** Every notification — regardless of visibility — **always includes super_admin and admin as recipients**. They act as system-wide auditors and see everything in their notification log.

### 7.1 Login & Auth Events (9 events) — domain_type: `login`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 1 | `user_signup_success` | restricted | low | The user who signed up + super_admin + admin | New user registered on the platform |
| 2 | `user_login_success` | restricted | low | The logged-in user + super_admin + admin | User logged in successfully (activity trail) |
| 3 | `user_login_failed` | restricted | medium | super_admin + admin | Failed login attempt (wrong credentials) |
| 4 | `user_logout` | restricted | low | The user who logged out + super_admin + admin | User logged out (activity trail) |
| 5 | `password_changed` | restricted | medium | The user + super_admin + admin | User changed password, all sessions invalidated |
| 6 | `forgot_password_otp_sent` | restricted | medium | The user + super_admin + admin | OTP sent for password reset |
| 7 | `otp_validated` | restricted | low | super_admin + admin | OTP validated successfully |
| 8 | `password_reset_complete` | restricted | medium | The user + super_admin + admin | Password reset via OTP flow completed |
| 9 | `role_created` | restricted | low | super_admin + admin | New role added to the system |

### 7.2 Job & Position Events (22 events) — domain_type: `jobs`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 11 | `job_created` | restricted | medium | super_admin, admin | New job opening created |
| 12 | `job_updated` | restricted | medium | Job assignees + admin | Job details modified |
| 13 | `job_deadline_approaching` | restricted | high | Job assignees + super_admin + admin | Job deadline is within 3 days |
| 14 | `job_deadline_exceeded` | restricted | critical | Job assignees + admin | Job deadline has passed |
| 15 | `candidate_assigned_to_job` | restricted | medium | Job SPOC + assigned recruiter + super_admin + admin | Candidate added to a job pipeline |
| 16 | `candidate_bulk_assigned_to_job` | restricted | medium | Job SPOC + all recruiters + super_admin + admin | Multiple candidates added to job |
| 17 | `candidate_pipeline_stage_changed` | restricted | medium | Assigned recruiter + SPOC + super_admin + admin | Candidate moved through pipeline stage |
| 18 | `candidate_joined` | restricted | high | All job assignees + admin | Candidate accepted offer and joined |
| 19 | `candidate_joined_updated` | restricted | medium | All job assignees + admin | Joining details updated |
| 20 | `candidate_rejected` | restricted | medium | All job assignees + super_admin + admin | Candidate rejected from job |
| 21 | `candidate_rejected_updated` | restricted | medium | All job assignees + super_admin + admin | Rejection details updated |
| 22 | `candidate_joined_remark_added` | restricted | low | Job SPOC + super_admin + admin | Internal note on accepted candidate |
| 23 | `candidate_rejected_remark_added` | restricted | low | Job SPOC + super_admin + admin | Internal note on rejected candidate |
| 24 | `clawback_marked_complete` | restricted | medium | User who marked it + super_admin + admin | Clawback monitoring period finished |
| 25 | `intimation_mail_sent` | restricted | medium | All job stakeholders + super_admin + admin | Bulk intimation email sent to stakeholders |
| 26 | `candidate_removed_from_job` | restricted | medium | Job SPOC + assigned recruiter + super_admin + admin | Candidate unassigned from job |
| 27 | `pipeline_stage_activity_added` | restricted | low | Job SPOC + super_admin + admin | Activity log entry for pipeline stage |
| 28 | `spoc_assigned_to_stage` | restricted | medium | The assigned SPOC + super_admin + admin | SPOC assigned to pipeline stage |
| 29 | `spoc_assignment_updated` | restricted | medium | Old SPOC + New SPOC + super_admin + admin | SPOC changed for pipeline stage |
| 30 | `spoc_assignment_removed` | restricted | medium | Former SPOC + super_admin + admin | SPOC unassigned from pipeline stage |
| 31 | `recruiter_assigned_to_job` | restricted | medium | Each assigned recruiter + super_admin + admin | Job assigned to recruiter(s) |
| 32 | `recruiter_removed_from_job` | restricted | medium | The removed recruiter + super_admin + admin | Recruiter unassigned from job |

### 7.3 AI & Processing Events (14 events) — domain_type: `ai`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 33 | `resume_parse_started` | restricted | low | Submitting user + super_admin + admin | Resume parsing queued |
| 34 | `resume_parse_complete` | restricted | medium | Submitting user + super_admin + admin | Resume parsed successfully |
| 35 | `resume_parse_failed` | restricted | high | Submitting user + super_admin + admin | Resume parsing failed |
| 36 | `resume_match_started` | restricted | low | Submitting user + super_admin + admin | Resume-JD matching queued |
| 37 | `resume_match_complete` | restricted | medium | Submitting user + super_admin + admin | Resume-JD matching done |
| 38 | `resume_match_failed` | restricted | high | Submitting user + super_admin + admin | Resume-JD matching failed |
| 39 | `resume_summarize_started` | restricted | low | Submitting user + super_admin + admin | Summarization queued |
| 40 | `resume_summarize_complete` | restricted | medium | Submitting user + super_admin + admin | Summarization done |
| 41 | `resume_summarize_failed` | restricted | high | Submitting user + super_admin + admin | Summarization failed |
| 42 | `resume_upgrade_started` | restricted | low | Submitting user + super_admin + admin | Resume upgrade queued |
| 43 | `resume_upgrade_complete` | restricted | medium | Submitting user + super_admin + admin | Resume upgraded successfully |
| 44 | `resume_upgrade_failed` | restricted | high | Submitting user + super_admin + admin | Resume upgrade failed |
| 45 | `bulk_processing_started` | restricted | medium | Uploading user + super_admin + admin | Bulk file processing started |
| 46 | `bulk_processing_complete` | restricted | medium | Uploading user + super_admin + admin | Bulk processing finished with stats |

### 7.4 Candidate Events (8 events) — domain_type: `candidate`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 47 | `candidate_created` | restricted | low | Creator user + super_admin + admin | New candidate profile created |
| 48 | `candidate_created_bulk_upload` | restricted | medium | Uploading user + super_admin + admin | Candidate imported from bulk file |
| 49 | `candidate_updated` | restricted | low | Creator/Assignee + super_admin + admin | Candidate information modified |
| 50 | `candidate_status_changed` | restricted | medium | Assigned user + super_admin + admin | Candidate pipeline status updated |
| 51 | `candidate_assigned_to_user` | restricted | medium | Newly assigned user + super_admin + admin | Candidate assigned to recruiter |
| 52 | `candidate_bulk_assigned` | restricted | medium | All assigned users + super_admin + admin | Multiple candidates bulk-assigned |
| 53 | `candidate_remark_added` | restricted | low | Assigned user + super_admin + admin | Comment/remark added to candidate |
| 54 | `candidate_duplicate_detected` | restricted | low | Requesting user + super_admin + admin | Duplicate candidate email detected |

### 7.5 Security Events (3 events) — domain_type: `security`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 55 | `illegal_login_attempt` | restricted | critical | super_admin, admin | Suspicious/repeated failed login detected |
| 56 | `scraping_activity_detected` | restricted | critical | super_admin, admin | Potential data scraping detected |
| 57 | `wrong_password_threshold` | restricted | high | super_admin, admin | User exceeded wrong password attempt threshold |

### 7.6 User Management Events (9 events) — domain_type: `user_management`

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| 57 | `user_created_by_admin` | restricted | medium | New user + super_admin + admin | Admin created a new user account |
| 58 | `user_invitation_sent` | restricted | low | The invited user + super_admin + admin | Welcome email with setup link sent |
| 59 | `user_invitation_resent` | restricted | low | The user + super_admin + admin | Invitation re-sent |
| 60 | `user_token_verified` | restricted | low | super_admin + admin | User clicked email verification link |
| 61 | `user_password_set` | restricted | medium | The user + super_admin + admin | User set their initial password |
| 62 | `user_account_enabled` | restricted | medium | The user + super_admin + admin | Previously disabled user re-enabled |
| 63 | `user_account_disabled` | restricted | high | The user + super_admin + admin | User account deactivated |
| 64 | `user_role_changed` | restricted | high | The user + super_admin + admin | User role updated |
| 65 | `user_updated` | restricted | low | The user + super_admin + admin | User details (name, email, etc.) updated |

### 7.7 System Events (auto-generated)

| # | Event Name | Visibility | Priority | Target | Description |
|---|-----------|------------|----------|--------|-------------|
| -- | `scheduled_notification` | varies | varies | varies | Triggered by scheduler (admin-defined) |
| -- | `banner_expired` | public | low | all | Banner auto-expired |

### 7.8 Global Admin Inclusion Rule

**Every notification always includes super_admin and admin as recipients.** This is enforced at the engine level — the `resolve_target_user_ids` function in `store.py` always appends admin/super_admin user IDs to the recipient list regardless of target_type or visibility. This means:

- A personal notification to a single user (e.g., "Your resume parse completed") also appears in admin/super_admin notification logs
- Admin/super_admin can filter by domain_type, date, source_service, etc. to see exactly what's happening across the system
- The notification is stored once in the `notifications` table; multiple recipient entries exist in `notification_recipients` (one per user including admins)

---

## 8. Event Trigger Flow

```
Other Service                    Notification Service
     │                                  │
     │  POST /notifications/event       │
     │  {                               │
     │    "event_name": "candidate_joined",
     │    "data": {                     │
     │      "candidate_name": "John",   │
     │      "job_title": "SWE",         │
     │      "job_id": 42,               │
     │      "user_id": 5               │
     │    }                             │
     │  }                               │
     │ ─────────────────────────────►   │
     │                                  │ 1. Lookup event in notification_events table
     │                                  │ 2. Check is_enabled
     │                                  │ 3. Render title/message from templates + data
     │                                  │ 4. Determine domain_type, visibility, priority
     │                                  │ 5. Resolve target users:
     │                                  │    - If target_type=role → query users by role
     │                                  │    - If target_type=job → query user_jobs_assigned
     │                                  │    - If target_type=user → use data.user_id
     │                                  │    - ALWAYS add super_admin/admin (every notification)
     │                                  │ 6. INSERT into notifications table
     │                                  │ 7. Batch INSERT into notification_recipients
     │                                  │ 8. Publish to Redis Pub/Sub channels
     │                                  │ 9. Invalidate unread count caches
     │    { "notification_id": 123 }    │
     │ ◄─────────────────────────────   │
```

---

## 9. Redis Design

### 9.1 Pub/Sub Channels

| Channel Pattern | Purpose | Publishers | Subscribers |
|----------------|---------|------------|-------------|
| `notif:user:{user_id}` | Per-user targeted notifications | Notification Engine | User's WebSocket connection |
| `notif:broadcast` | Public notifications to all users | Notification Engine | All active WebSocket connections |
| `notif:banner` | Banner create/update/expire events | Notification Engine | All active WebSocket connections |

### 9.2 Cache Keys

| Key Pattern | Type | TTL | Purpose |
|-------------|------|-----|---------|
| `notif:unread:{user_id}` | STRING (int) | 300s | Cached unread count per user |
| `notif:banners:active` | STRING (JSON) | 60s | Active banner list cache |
| `notif:schedule:lock` | STRING | 30s | Scheduler distributed lock (prevent duplicates) |

### 9.3 Pub/Sub Message Format

```json
{
    "id": 123,
    "title": "Candidate Joined: John Doe",
    "message": "John Doe has joined for the Software Engineer position at Acme Corp",
    "delivery_mode": "push",
    "domain_type": "jobs",
    "visibility": "restricted",
    "priority": "high",
    "source_service": "job",
    "event_type": "candidate_joined",
    "metadata": {
        "candidate_id": "CAND_001",
        "candidate_name": "John Doe",
        "job_id": "JOB_2026040812345",
        "job_title": "Software Engineer"
    },
    "created_at": "2026-04-09T10:30:00Z"
}
```

---

## 10. WebSocket Protocol

### 10.1 Connection
```
WS /ws/notifications?token=<jwt_token>
```

On connect, the server:
1. Validates JWT token
2. Extracts user_id from token
3. Subscribes to `notif:user:{user_id}`, `notif:broadcast`, and `notif:banner`
4. Sends initial unread count

### 10.2 Server → Client Messages

**Push Notification:**
```json
{
    "type": "notification",
    "data": {
        "id": 123,
        "title": "Candidate Assigned",
        "message": "Candidate John Doe has been assigned to you",
        "delivery_mode": "push",
        "domain_type": "candidate",
        "visibility": "personal",
        "priority": "medium",
        "metadata": {"candidate_id": "CAND_001"},
        "created_at": "2026-04-09T10:30:00Z"
    }
}
```

**Banner Event:**
```json
{
    "type": "banner",
    "action": "create",
    "data": {
        "id": 456,
        "title": "Q2 Hiring Freeze Lifted",
        "message": "All positions are now open for hiring. Target: 50 hires by June 30.",
        "priority": "high",
        "expires_at": "2026-04-15T00:00:00Z"
    }
}
```

**Unread Count Update:**
```json
{
    "type": "unread_count",
    "data": {"count": 5}
}
```

### 10.3 Client → Server Messages

```json
{"action": "mark_read", "notification_id": 123}
{"action": "ping"}
```

---

## 11. API Endpoints

### 11.1 Manual Triggers (admin/super_admin only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/notifications/send` | Send a push notification with full targeting options |
| POST | `/notifications/banner` | Create a banner notification with expiry |

### 11.2 User Notification Log (any authenticated user)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/notifications` | Paginated notification log with all filters (domain_type, visibility, date range, priority, read status) |
| GET | `/notifications/unread-count` | Quick unread count (cached in Redis) |
| GET | `/notifications/banners/active` | Active banners for dashboard ticker |

### 11.3 Actions (any authenticated user)

| Method | Endpoint | Description |
|--------|----------|-------------|
| PUT | `/notifications/{id}/read` | Mark single notification as read |
| PUT | `/notifications/mark-all-read` | Mark all notifications as read |

### 11.4 Admin Log (admin/super_admin only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/notifications/admin/logs` | ALL notifications with full filters: domain_type, visibility, date range, priority, source_service, event_type, user_id, created_by |

### 11.5 Scheduling (admin/super_admin only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/notifications/schedule` | Create scheduled notification |
| GET | `/notifications/schedules` | List all scheduled notifications |
| PUT | `/notifications/schedules/{id}/cancel` | Cancel a pending schedule |

### 11.6 Internal Event Trigger (service-to-service)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/notifications/event` | Trigger auto-notification from any microservice |

### 11.7 WebSocket

| Protocol | Endpoint | Description |
|----------|----------|-------------|
| WS | `/ws/notifications` | Real-time notification stream (push + banner + unread count) |

---

## 12. Scheduler Design

A background asyncio task runs every **60 seconds** checking for:

1. **Scheduled notifications** where `scheduled_at <= now()` and `status = 'pending'`
   - Sends the notification via the normal flow
   - Updates `status = 'sent'` and `last_sent_at`
   - For repeating: calculates next occurrence, keeps `status = 'pending'`
   - Respects `repeat_until` — cancels if past

2. **Job deadline checks**
   - Deadlines exceeded today → triggers `job_deadline_exceeded` event
   - Deadlines within 3 days → triggers `job_deadline_approaching` event
   - Only fires once per deadline (checks if event already fired via metadata)

3. **Expired banners**
   - Deactivates banners where `expires_at <= now()` and `is_active = 1`
   - Publishes `banner_expired` event to Redis for real-time UI removal
   - Invalidates banner cache

4. **Distributed lock** via Redis (`notif:schedule:lock`, 30s TTL) to prevent duplicate execution in multi-instance deployments.

---

## 13. File Structure

```
AI_AGENT15_STATUS_SERVICE/
├── app/
│   ├── api/
│   │   ├── status_api.py                          # Existing (unchanged)
│   │   ├── notification_api.py                     # NEW: Notification router
│   │   └── endpoints/
│   │       ├── notifications/
│   │       │   ├── __init__.py
│   │       │   ├── send_notification_api.py        # Manual send (push)
│   │       │   ├── get_notifications_api.py        # User notification log
│   │       │   ├── banner_api.py                   # Banner CRUD
│   │       │   ├── notification_actions_api.py     # Read/unread actions
│   │       │   ├── admin_notifications_api.py      # Admin log view
│   │       │   ├── schedule_api.py                 # Schedule CRUD
│   │       │   ├── event_trigger_api.py            # Internal event trigger
│   │       │   └── ws_notification.py              # WebSocket endpoint
│   │       └── dependencies/                       # Existing (auth_utils, progress)
│   ├── notification_layer/
│   │   ├── __init__.py
│   │   ├── models.py                               # SQLAlchemy models (4 tables)
│   │   ├── schemas.py                              # Pydantic request/response schemas
│   │   ├── store.py                                # DB CRUD operations
│   │   ├── redis_manager.py                        # Redis pub/sub + caching
│   │   ├── ws_manager.py                           # WebSocket connection manager
│   │   ├── event_handler.py                        # Auto-notification processing
│   │   └── scheduler.py                            # Background scheduler
│   ├── database_Layer/                             # Existing (unchanged)
│   ├── core/                                       # Existing (unchanged)
│   └── main.py                                     # Modified: add notification router + scheduler startup
├── migrations/
│   └── create_notification_tables.sql              # SQL migration script
├── NOTIFICATION_SERVICE_ARCHITECTURE.md             # This document
└── pyproject.toml                                  # Updated with new deps
```

---

## 14. Security Considerations

1. **Authentication**: All REST endpoints require JWT. WebSocket uses token query parameter.
2. **Authorization**: Manual send/banner/schedule restricted to `admin` and `super_admin`.
3. **Admin audit**: Admin/super_admin see all notifications in admin logs — full audit trail.
4. **Data isolation**: Regular users see only their own notifications (personal + public + restricted they're part of).
5. **Event API**: Protected by JWT; any authenticated service can trigger events.
6. **Rate limiting**: Handled by API Gateway (existing).

---

## 15. Integration Guide for Other Services

To trigger auto-notifications from any microservice, POST to the event endpoint:

```python
import requests

# Example: Login Service triggers on illegal login attempt
requests.post(
    "http://status-service:8515/status/notifications/event",
    json={
        "event_name": "illegal_login_attempt",
        "data": {
            "username": "john_doe",
            "ip_address": "192.168.1.100",
            "attempts": 5,
            "timestamp": "2026-04-09T14:30:00Z"
        }
    },
    headers={"Authorization": f"Bearer {service_token}"}
)

# Example: Job Service triggers on candidate joining
requests.post(
    "http://status-service:8515/status/notifications/event",
    json={
        "event_name": "candidate_joined",
        "data": {
            "candidate_name": "Jane Smith",
            "candidate_id": "CAND_042",
            "job_title": "Senior Backend Engineer",
            "job_id": 42,
            "joining_date": "2026-04-15"
        }
    },
    headers={"Authorization": f"Bearer {service_token}"}
)

# Example: Resume Analyzer triggers on parse complete
requests.post(
    "http://status-service:8515/status/notifications/event",
    json={
        "event_name": "resume_parse_complete",
        "data": {
            "candidate_name": "Alice Johnson",
            "candidate_id": "CAND_099",
            "user_id": 5,
            "resume_version": 1
        }
    },
    headers={"Authorization": f"Bearer {service_token}"}
)
```

---

## 16. Performance Considerations

1. **Redis Pub/Sub** for zero-latency real-time delivery (no polling)
2. **Unread count caching** in Redis (5-minute TTL) — avoids COUNT queries on every page load
3. **Active banner caching** in Redis (60s TTL)
4. **Composite indexes** on notification filter columns for fast admin log queries
5. **Batch INSERT** for notification_recipients when broadcasting
6. **Connection pooling** for both MySQL (existing) and Redis
7. **Scheduler lock** via Redis to prevent duplicate execution in multi-instance deployments
8. **Pagination** with cursor-based or offset pagination on all log endpoints
