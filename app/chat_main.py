"""Chat Service entry point.

Runs the chat module as a *separate* FastAPI process inside the same codebase
as the Status Service. Registers with Consul under its own service name and
path so the API Gateway routes `/chat/*` to it independently.

Run locally:
    uvicorn app.chat_main:app --host 0.0.0.0 --port 8517

In production this is started by `start.sh` alongside `app.main:app` (status)
and the notification UI.
"""
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Iterable

import consul
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.chat_layer.api.chat_router import router as chat_router
from app.chat_layer.ws_manager import ws_manager as chat_ws_manager
from app.core import settings
from app.core.consul_registration import (
    consul_registry, get_local_ip,
)

logger = logging.getLogger("app_logger")


CHAT_SERVICE_NAME = os.getenv("CHAT_SERVICE_NAME", "HRMIS_CHAT_SERVICE")
CHAT_SERVICE_PORT = int(os.getenv("CHAT_SERVICE_PORT", "8517"))
CHAT_SERVICE_PATH = os.getenv("CHAT_SERVICE_PATH", "/chat")
CHAT_SERVICE_AUTH = os.getenv("CHAT_SERVICE_AUTH", "mixed")

# Paths under /chat that don't need JWT (the WS uses its own ?token= query
# auth, the gateway's JWT middleware should let it through).
CHAT_NO_AUTH_PATHS: Iterable[str] = [
    "/health",
    f"{CHAT_SERVICE_PATH}/health",
    f"{CHAT_SERVICE_PATH}/model/api/docs",
    f"{CHAT_SERVICE_PATH}/openapi.json",
    f"{CHAT_SERVICE_PATH}/redoc",
    f"{CHAT_SERVICE_PATH}/ws",
]


def _register_chat_with_consul() -> str:
    """Register the chat service with Consul under its own name/path/port."""
    if not getattr(settings, "CONSUL_ENABLED", False):
        logger.info("Consul disabled — skipping chat registration")
        return ""
    if not consul_registry.consul_client:
        logger.warning("Consul client not initialised — skipping chat registration")
        return ""

    service_address = get_local_ip()
    health_check_address = (
        "127.0.0.1"
        if str(settings.CONSUL_HOST) in {"localhost", "127.0.0.1"}
        else service_address
    )
    service_id = f"{CHAT_SERVICE_NAME}-{service_address}-{CHAT_SERVICE_PORT}"

    tags = [
        "chat-service",
        "api",
        "fastapi",
        f"path={CHAT_SERVICE_PATH}",
        f"auth={CHAT_SERVICE_AUTH}",
    ]
    for p in sorted(set(CHAT_NO_AUTH_PATHS)):
        tags.append(f"no_auth_path={p}")

    health_check_enabled = getattr(settings, "CONSUL_HEALTH_CHECK_ENABLED", True)
    check = None
    if health_check_enabled:
        check = consul.Check.http(
            url=f"http://{health_check_address}:{CHAT_SERVICE_PORT}{CHAT_SERVICE_PATH}/health",
            interval="10s",
            timeout="5s",
            deregister="30s",
        )

    register_kwargs = dict(
        name=CHAT_SERVICE_NAME,
        service_id=service_id,
        address=service_address,
        port=CHAT_SERVICE_PORT,
        tags=tags,
    )
    if check is not None:
        register_kwargs["check"] = check

    consul_registry.consul_client.agent.service.register(**register_kwargs)
    logger.info(
        "Chat service registered with Consul: %s (ID: %s, %s:%s, Path: %s)",
        CHAT_SERVICE_NAME, service_id, service_address, CHAT_SERVICE_PORT, CHAT_SERVICE_PATH,
    )
    return service_id


def _deregister_chat_from_consul(service_id: str) -> None:
    if not service_id or not consul_registry.consul_client:
        return
    try:
        consul_registry.consul_client.agent.service.deregister(service_id)
        logger.info("Chat service deregistered from Consul: %s", service_id)
    except Exception as exc:
        logger.warning("Chat Consul deregistration failed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    chat_service_id = ""
    try:
        chat_service_id = _register_chat_with_consul()
    except Exception as exc:
        logger.warning("Chat Consul registration failed during startup: %s",
                       exc, exc_info=True)

    try:
        await chat_ws_manager.start_redis_subscriber()
        logger.info("Chat WebSocket Redis subscriber started")
    except Exception as exc:
        logger.warning("Failed to start Chat WS Redis subscriber: %s",
                       exc, exc_info=True)

    yield

    # ---- shutdown ----
    _deregister_chat_from_consul(chat_service_id)


app = FastAPI(
    title="Chat Service API",
    version="1.0.0",
    description="In-platform chat (DM, team, #general) for the Recruitment Agent",
    docs_url=f"{CHAT_SERVICE_PATH}/model/api/docs",
    redoc_url=f"{CHAT_SERVICE_PATH}/redoc",
    openapi_url=f"{CHAT_SERVICE_PATH}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount chat routes under /chat
app.include_router(chat_router, prefix=CHAT_SERVICE_PATH)


@app.get(f"{CHAT_SERVICE_PATH}/health")
@app.get("/health")
def health():
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "chat", "port": CHAT_SERVICE_PORT},
    )
