import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import status_api
from app.api import notification_api
from app.api.endpoints.notifications import test_api as notification_test_api
from app.whatsapp_layer.api import whatsapp_router, clawback_notifications_router

from app.core import settings
from app.core.consul_registration import consul_registry
from app.notification_layer.ws_manager import ws_manager
from app.notification_layer.scheduler import run_scheduler

# NOTE: the chat module runs as a *separate* FastAPI process via
# `app.chat_main:app` on its own port (CHAT_SERVICE_PORT, default 8517).
# It is NOT mounted here.

logger = logging.getLogger("app_logger")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    if getattr(settings, "CONSUL_ENABLED", False):
        try:
            consul_registry.register_service(
                service_name=settings.CONSUL_SERVICE_NAME,
                health_check_url=f"{settings.CONSUL_SERVICE_PATH}/health",
                service_path=settings.CONSUL_SERVICE_PATH,
                auth_required=settings.CONSUL_SERVICE_AUTH,
            )
        except Exception as exc:
            logger.warning("Consul registration failed during startup: %s", exc, exc_info=True)

    # Start notification WebSocket Redis subscriber
    try:
        await ws_manager.start_redis_subscriber()
        logger.info("Notification WebSocket Redis subscriber started")
    except Exception as exc:
        logger.warning("Failed to start WS Redis subscriber: %s", exc, exc_info=True)

    # Start notification scheduler background task
    scheduler_task = asyncio.create_task(run_scheduler())
    logger.info("Notification scheduler background task started")

    yield

    # --- Shutdown ---
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

    if getattr(settings, "CONSUL_ENABLED", False):
        try:
            consul_registry.deregister_service()
        except Exception as exc:
            logger.warning("Consul deregistration failed during shutdown: %s", exc, exc_info=True)


app = FastAPI(
    title="Status & Notification API",
    version="2.0.0",
    description="Status Service + Notification Service API",
    docs_url="/model/api/docs",
    lifespan=lifespan,
)

# Configure CORS middleware to allow cross-origin requests
origins = ["*"]  # Allow requests from all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include status API router with WebSocket endpoints
app.include_router(status_api.router, prefix="/status")

# Include notification API router
app.include_router(notification_api.router, prefix="/status/notifications")

# Include test API router (no JWT — for the test dashboard UI)
app.include_router(notification_test_api.router, prefix="/test")

# Clawback WhatsApp notifications (CLAWBACK_WHATSAPP_API_CONTRACT.md §2)
app.include_router(whatsapp_router, prefix="/status/whatsapp")
app.include_router(clawback_notifications_router, prefix="/status/clawback-notifications")

_CONTRACT_422_PREFIXES = ("/status/whatsapp", "/status/clawback-notifications")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """The WhatsApp / clawback APIs report 422 as [{field, message}] (contract
    §0). Every other route keeps FastAPI's default 422 body."""
    if not request.url.path.startswith(_CONTRACT_422_PREFIXES):
        return await request_validation_exception_handler(request, exc)
    detail = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
        detail.append({"field": ".".join(loc) or "body", "message": err.get("msg", "Invalid value")})
    return JSONResponse(status_code=422, content={"detail": detail})


# Health endpoints (gateway + Consul might probe these paths)
@app.get("/status/health")
@app.get("/health")
def health():
    return JSONResponse(
        content={"status": "ok", "service": "status-notification"},
        media_type="application/json",
        status_code=200,
    )
