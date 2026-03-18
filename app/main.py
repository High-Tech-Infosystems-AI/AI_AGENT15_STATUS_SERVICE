from fastapi import FastAPI
from app.api import status_api
from fastapi.middleware.cors import CORSMiddleware

from app.core import settings
from app.core.consul_registration import consul_registry

app = FastAPI(
    title="Status API",
    version="1.0.0",
    description="Status Service API",
    docs_url="/model/api/docs"
)


@app.on_event("startup")
async def _startup():
    consul_registry.register_service(
        service_name=settings.CONSUL_SERVICE_NAME,
        service_path=settings.CONSUL_SERVICE_PATH,
    )


@app.on_event("shutdown")
async def _shutdown():
    consul_registry.deregister_service()

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
    return {"status": "ok", "service": "status"}




