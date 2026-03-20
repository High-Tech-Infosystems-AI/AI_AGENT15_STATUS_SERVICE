import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import status_api

from app.core import settings
from app.core.consul_registration import consul_registry

logger = logging.getLogger("app_logger")


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    yield

    if getattr(settings, "CONSUL_ENABLED", False):
        try:
            consul_registry.deregister_service()
        except Exception as exc:
            logger.warning("Consul deregistration failed during shutdown: %s", exc, exc_info=True)


app = FastAPI(
    title="Status API",
    version="1.0.0",
    description="Status Service API",
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


# Health endpoints (gateway + Consul might probe these paths)
@app.get("/status/health")
@app.get("/health")
def health():
    return JSONResponse(
        content={"status": "ok", "service": "status"},
        media_type="application/json",
        status_code=200,
    )
