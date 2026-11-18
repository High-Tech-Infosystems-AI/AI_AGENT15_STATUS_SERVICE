"""
Internal Event Trigger API — called by other microservices.
POST /notifications/event

NO JWT required — this is a service-to-service endpoint used by the
Login/Job/Candidate/RBAC/Resume services to fire auto-notifications.
The endpoint is tagged no-auth in Consul so the gateway doesn't block it.

Security: the endpoint only fires pre-registered events from
`notification_events` table. It cannot be used to inject arbitrary content.
"""

import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.database_Layer.db_config import SessionLocal, get_db
from app.notification_layer.event_handler import handle_event
from app.notification_layer.schemas import EventTriggerRequest, EventTriggerResponse
from app.webhooks_layer.delivery import deliver_now, fan_out
from app.whatsapp_layer.clawback_engine import handle_clawback_state_changed

CLAWBACK_STATE_CHANGED = "clawback_state_changed"

logger = logging.getLogger("app_logger")
router = APIRouter()

GATEWAY_HEADER = "X-Trace-Id"

# Running first attempts, kept so they aren't garbage-collected mid-send.
_deliveries_in_flight: set = set()


def _start_delivery(delivery_ids: list) -> None:
    """Send now, whatever this request returns: most events aren't registered
    for in-app notifications and end in a 404, and FastAPI skips background
    tasks on error responses."""
    if not delivery_ids:
        return
    task = asyncio.create_task(asyncio.to_thread(deliver_now, delivery_ids))
    _deliveries_in_flight.add(task)
    task.add_done_callback(_deliveries_in_flight.discard)


def _queue_webhooks(event_name: str, data: dict) -> list:
    """Own session, so a webhook problem can't touch the event's transaction."""
    db = SessionLocal()
    try:
        return fan_out(db, event_name, data)
    finally:
        db.close()


@router.post("/event", response_model=EventTriggerResponse)
async def trigger_event(
    request: EventTriggerRequest,
    http_request: Request,
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

    # Outgoing webhooks get every event, whether or not it is registered for
    # in-app notifications. Never let this affect the event's own handling.
    # Only events the services send directly qualify: anything that came in
    # through the public gateway (which always adds X-Trace-Id) could be
    # forged, and webhooks are signed as coming from us.
    if not http_request.headers.get(GATEWAY_HEADER):
        try:
            delivery_ids = await run_in_threadpool(_queue_webhooks, request.event_name, request.data or {})
            _start_delivery(delivery_ids)
        except Exception as e:
            logger.error("Queueing webhooks for '%s' failed: %s", request.event_name, e, exc_info=True)

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
