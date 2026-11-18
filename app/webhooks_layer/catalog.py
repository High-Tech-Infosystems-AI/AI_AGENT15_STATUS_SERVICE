"""Events an organisation can subscribe a webhook to.

Names are the event_name values the services already send to
/notifications/event. Sign-in, password and OTP events are deliberately not
offered: they carry account-security details that should not leave the platform.
"""
from __future__ import annotations

from typing import Dict, List

PING = "webhook.ping"

EVENT_GROUPS: Dict[str, Dict[str, str]] = {
    "Candidates": {
        "candidate_created": "A candidate was added",
        "candidate_updated": "A candidate's details changed",
        "candidate_status_changed": "A candidate's status changed",
        "candidate_assigned_to_user": "A candidate was assigned to a recruiter",
        "candidate_bulk_assigned": "Candidates were assigned in bulk",
        "candidate_assigned_to_job": "A candidate was added to a job",
        "candidate_removed_from_job": "A candidate was removed from a job",
        "candidate_pipeline_stage_changed": "A candidate moved to another pipeline stage",
        "pipeline_stage_activity_added": "Activity was logged on a pipeline stage",
        "candidate_remark_added": "A remark was added to a candidate",
        "candidate_joined": "A candidate joined",
        "candidate_joined_updated": "A joined candidate's details changed",
        "candidate_joined_remark_added": "A remark was added to a joined candidate",
        "candidate_rejected": "A candidate was rejected",
        "candidate_rejected_updated": "A rejected candidate's details changed",
        "candidate_rejected_remark_added": "A remark was added to a rejected candidate",
        "intimation_mail_sent": "An intimation mail was sent to a candidate",
    },
    "Jobs": {
        "job_created": "A job was created",
        "job_updated": "A job changed",
        "job_deadline_updated": "A job's deadline changed",
        "job_positions_updated": "A job's open positions changed",
        "recruiter_assigned_to_job": "A recruiter was assigned to a job",
        "recruiter_removed_from_job": "A recruiter was removed from a job",
        "team_assigned_to_job": "A team was assigned to a job",
        "team_removed_from_job": "A team was removed from a job",
        "spoc_assigned_to_stage": "A SPOC was assigned to a stage",
        "spoc_assignment_updated": "A SPOC assignment changed",
        "spoc_assignment_removed": "A SPOC assignment was removed",
    },
    "Clawback": {
        "clawback_state_changed": "A placement's clawback state changed",
        "clawback_marked_complete": "A placement's clawback period was completed",
    },
    "Resume AI": {
        "resume_parse_started": "Resume parsing started",
        "resume_parse_complete": "Resume parsing finished",
        "resume_parse_failed": "Resume parsing failed",
        "resume_match_started": "Resume matching started",
        "resume_match_complete": "Resume matching finished",
        "resume_match_failed": "Resume matching failed",
        "resume_summarize_started": "Resume summary started",
        "resume_summarize_complete": "Resume summary finished",
        "resume_summarize_failed": "Resume summary failed",
        "resume_upgrade_started": "Resume upgrade started",
        "resume_upgrade_complete": "Resume upgrade finished",
        "resume_upgrade_failed": "Resume upgrade failed",
    },
    "Organisation": {
        "organisation_profile_updated": "The organisation profile changed",
        "organisation_status_changed": "The organisation's status changed",
        "branch_created": "A branch was created",
        "branch_updated": "A branch changed",
        "branch_deleted": "A branch was deleted",
        "department_created": "A department was created",
        "department_updated": "A department changed",
        "department_deleted": "A department was deleted",
        "designation_created": "A designation was created",
        "designation_updated": "A designation changed",
        "designation_deleted": "A designation was deleted",
        "position_created": "A position was created",
        "position_updated": "A position changed",
        "position_deleted": "A position was deleted",
        "team_created": "A team was created",
        "team_updated": "A team changed",
        "team_deleted": "A team was deleted",
        "team_members_added": "Members were added to a team",
        "team_members_removed": "Members were removed from a team",
    },
    "Users": {
        "user_created_by_admin": "An admin created a user",
        "user_updated": "A user's details changed",
        "user_role_changed": "A user's role changed",
        "user_account_enabled": "A user account was enabled",
        "user_account_disabled": "A user account was disabled",
        "user_invitation_sent": "A user was invited",
        "user_invitation_resent": "A user's invitation was resent",
        "role_created": "A role was created",
    },
}

SUBSCRIBABLE = frozenset(name for group in EVENT_GROUPS.values() for name in group)


def as_list() -> List[Dict[str, object]]:
    """The catalogue for the API: groups of {name, description}."""
    return [{"group": group, "events": [{"name": n, "description": d} for n, d in events.items()]}
            for group, events in EVENT_GROUPS.items()]


def subscribed(endpoint_events, event_name: str) -> bool:
    events = endpoint_events or []
    return event_name in SUBSCRIBABLE and ("*" in events or event_name in events)
