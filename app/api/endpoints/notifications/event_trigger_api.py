"""
Internal Event Trigger API — called by other microservices.
POST /notifications/event
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.dependencies.auth_utils import validate_token
from app.database_Layer.db_config import get_db
from app.notification_layer.event_handler import handle_event
from app.notification_layer.schemas import EventTriggerRequest, EventTriggerResponse

logger = logging.getLogger("app_logger")
router = APIRouter()


@router.post("/event", response_model=EventTriggerResponse)
async def trigger_event(
    request: EventTriggerRequest,
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db),
):
    """
    Trigger an auto-notification event from another microservice.

    Any authenticated service/user can trigger events.
    The event_name must be registered in the notification_events table.
    """
    success, notification_id, message = handle_event(
        db=db,
        event_name=request.event_name,
        data=request.data or {},
    )

    if not success:
        raise HTTPException(status_code=404, detail=message)

    return EventTriggerResponse(
        success=True,
        notification_id=notification_id,
        message=message,
    )
