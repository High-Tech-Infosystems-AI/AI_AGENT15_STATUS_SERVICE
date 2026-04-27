import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import status_api
from app.api import notification_api
from app.api.endpoints.notifications import test_api as notification_test_api

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


# Health endpoints (gateway + Consul might probe these paths)
@app.get("/status/health")
@app.get("/health")
def health():
    return JSONResponse(
        content={"status": "ok", "service": "status-notification"},
        media_type="application/json",
        status_code=200,
    )
