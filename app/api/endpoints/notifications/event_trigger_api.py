"""
Internal Event Trigger API — called by other microservices.
POST /notifications/event

NO JWT required — this is a service-to-service endpoint used by the
Login/Job/Candidate/RBAC/Resume services to fire auto-notifications.
The endpoint is tagged no-auth in Consul so the gateway doesn't block it.

Security: the endpoint only fires pre-registered events from
`notification_events` table. It cannot be used to inject arbitrary content.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.database_Layer.db_config import get_db
from app.notification_layer.event_handler import handle_event
from app.notification_layer.schemas import EventTriggerRequest, EventTriggerResponse
from app.whatsapp_layer.clawback_engine import handle_clawback_state_changed

CLAWBACK_STATE_CHANGED = "clawback_state_changed"

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.post("/event", response_model=EventTriggerResponse)
async def trigger_event(
    request: EventTriggerRequest,
    db: Session = Depends(get_db),
):
    """
    Trigger an auto-notification event from another microservice.

    The `event_name` must be registered + enabled in the `notification_events` table.
    The endpoint is open (no JWT) because login/signup flows call it
    before a user token exists. Protected at the gateway level via
    no_auth_path Consul tag.
    """
    logger.info("Event trigger received: %s (data keys: %s)",
                request.event_name, list((request.data or {}).keys()))

    # Clawback state changes (job service, contract §3.1) cancel / refresh
    # WhatsApp health checks and never create an in-app notification.
    if request.event_name == CLAWBACK_STATE_CHANGED:
        try:
            # May call the job service — keep the blocking work off the event loop.
            result = await run_in_threadpool(handle_clawback_state_changed, db, request.data or {})
        except Exception as e:
            db.rollback()
            logger.error("clawback_state_changed handling failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail="clawback_state_changed handling failed")
        return EventTriggerResponse(success=True, notification_id=None, message=result["message"])

    success, notification_id, message = handle_event(
        db=db,
        event_name=request.event_name,
        data=request.data or {},
    )

    if not success:
        logger.warning("Event '%s' failed: %s", request.event_name, message)
        raise HTTPException(status_code=404, detail=message)

    logger.info("Event '%s' succeeded → notification_id=%s", request.event_name, notification_id)
    return EventTriggerResponse(
        success=True,
        notification_id=notification_id,
        message=message,
    )
