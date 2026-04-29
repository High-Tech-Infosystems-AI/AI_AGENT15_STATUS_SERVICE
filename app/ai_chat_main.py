"""AI Chat Service entry point.

Sibling to `chat_main.py` (port 8517). This process owns the AI chatbot
endpoints under `/ai-chat`. Runs on port 8518 with its own Consul
registration so the API gateway routes `/ai-chat/*` to it independently.

Run locally:
    uvicorn app.ai_chat_main:app --host 0.0.0.0 --port 8518
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Iterable

import consul
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai_chat_layer.api.ai_router import router as ai_router
from app.ai_chat_layer.system_bot import ensure_ai_bot_user
from app.core import settings
from app.core.consul_registration import consul_registry, get_local_ip
from app.database_Layer.db_config import SessionLocal

logger = logging.getLogger("app_logger")

AI_CHAT_SERVICE_NAME = os.getenv("AI_CHAT_SERVICE_NAME", "HRMIS_AI_CHAT_SERVICE")
AI_CHAT_SERVICE_PORT = int(os.getenv("AI_CHAT_SERVICE_PORT", "8518"))
AI_CHAT_SERVICE_PATH = os.getenv("AI_CHAT_SERVICE_PATH", "/ai-chat")
AI_CHAT_SERVICE_AUTH = os.getenv("AI_CHAT_SERVICE_AUTH", "jwt")

AI_CHAT_NO_AUTH_PATHS: Iterable[str] = [
    "/health",
    f"{AI_CHAT_SERVICE_PATH}/health",
    f"{AI_CHAT_SERVICE_PATH}/model/api/docs",
    f"{AI_CHAT_SERVICE_PATH}/openapi.json",
    f"{AI_CHAT_SERVICE_PATH}/redoc",
]


def _register_with_consul() -> str:
    if not getattr(settings, "CONSUL_ENABLED", False):
        logger.info("Consul disabled — skipping AI chat registration")
        return ""
    if not consul_registry.consul_client:
        logger.warning("Consul client not initialised — skipping AI chat registration")
        return ""

    service_address = get_local_ip()
    health_check_address = (
        "127.0.0.1"
        if str(settings.CONSUL_HOST) in {"localhost", "127.0.0.1"}
        else service_address
    )
    service_id = f"{AI_CHAT_SERVICE_NAME}-{service_address}-{AI_CHAT_SERVICE_PORT}"

    tags = [
        "ai-chat-service", "api", "fastapi",
        f"path={AI_CHAT_SERVICE_PATH}",
        f"auth={AI_CHAT_SERVICE_AUTH}",
    ]
    for p in sorted(set(AI_CHAT_NO_AUTH_PATHS)):
        tags.append(f"no_auth_path={p}")

    health_check_enabled = getattr(settings, "CONSUL_HEALTH_CHECK_ENABLED", True)
    check = None
    if health_check_enabled:
        check = consul.Check.http(
            url=f"http://{health_check_address}:{AI_CHAT_SERVICE_PORT}{AI_CHAT_SERVICE_PATH}/health",
            interval="10s", timeout="5s", deregister="30s",
        )

    register_kwargs = dict(
        name=AI_CHAT_SERVICE_NAME,
        service_id=service_id,
        address=service_address,
        port=AI_CHAT_SERVICE_PORT,
        tags=tags,
    )
    if check is not None:
        register_kwargs["check"] = check

    consul_registry.consul_client.agent.service.register(**register_kwargs)
    logger.info(
        "AI chat service registered with Consul: %s (%s:%s, Path: %s)",
        AI_CHAT_SERVICE_NAME, service_address, AI_CHAT_SERVICE_PORT, AI_CHAT_SERVICE_PATH,
    )
    return service_id


def _deregister(service_id: str) -> None:
    if not service_id or not consul_registry.consul_client:
        return
    try:
        consul_registry.consul_client.agent.service.deregister(service_id)
        logger.info("AI chat service deregistered from Consul: %s", service_id)
    except Exception as exc:
        logger.warning("AI chat Consul deregistration failed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service_id = ""
    try:
        service_id = _register_with_consul()
    except Exception as exc:
        logger.warning("AI chat Consul registration failed: %s", exc, exc_info=True)

    # Provision the synthetic AI Assistant user so it can be a sender_id.
    try:
        db = SessionLocal()
        try:
            ensure_ai_bot_user(db)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("AI bot bootstrap failed: %s", exc, exc_info=True)

    yield
    _deregister(service_id)


app = FastAPI(
    title="AI Chat Service API",
    version="1.0.0",
    description="LLM-powered Ask Your Data assistant for the Recruitment Agent",
    docs_url=f"{AI_CHAT_SERVICE_PATH}/model/api/docs",
    redoc_url=f"{AI_CHAT_SERVICE_PATH}/redoc",
    openapi_url=f"{AI_CHAT_SERVICE_PATH}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ai_router, prefix=AI_CHAT_SERVICE_PATH)


@app.get(f"{AI_CHAT_SERVICE_PATH}/health")
@app.get("/health")
def health():
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "ai-chat", "port": AI_CHAT_SERVICE_PORT},
    )
