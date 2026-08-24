"""Mail Service entry point.

Runs the mail module as a *separate* FastAPI process inside the same codebase
as the Status Service. Registers with Consul under its own service name and
path so the API Gateway routes `/mail/*` to it independently.

Run locally:
    uvicorn app.mail_main:app --host 0.0.0.0 --port 8521

In production this is started by `start.sh` alongside `app.main:app` (status),
`app.chat_main:app` (chat) and `app.ai_chat_main:app` (ai-chat).
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Iterable

import consul
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import settings
from app.core.consul_registration import consul_registry, get_local_ip
from app.mail_layer.api.mail_router import router as mail_router

logger = logging.getLogger("app_logger")

MAIL_SERVICE_NAME = os.getenv("MAIL_SERVICE_NAME", "HRMIS_MAIL_SERVICE")
MAIL_SERVICE_PORT = int(os.getenv("MAIL_SERVICE_PORT", "8521"))
MAIL_SERVICE_PATH = os.getenv("MAIL_SERVICE_PATH", "/mail")
MAIL_SERVICE_AUTH = os.getenv("MAIL_SERVICE_AUTH", "required")

MAIL_NO_AUTH_PATHS: Iterable[str] = [
    "/health",
    f"{MAIL_SERVICE_PATH}/health",
    f"{MAIL_SERVICE_PATH}/model/api/docs",
    f"{MAIL_SERVICE_PATH}/openapi.json",
    f"{MAIL_SERVICE_PATH}/redoc",
]


def _register_mail_with_consul() -> str:
    if not getattr(settings, "CONSUL_ENABLED", False):
        logger.info("Consul disabled — skipping mail registration")
        return ""
    if not consul_registry.consul_client:
        logger.warning("Consul client not initialised — skipping mail registration")
        return ""

    service_address = get_local_ip()
    health_check_address = (
        "127.0.0.1"
        if str(settings.CONSUL_HOST) in {"localhost", "127.0.0.1"}
        else service_address
    )
    service_id = f"{MAIL_SERVICE_NAME}-{service_address}-{MAIL_SERVICE_PORT}"

    tags = [
        "mail-service", "api", "fastapi",
        f"path={MAIL_SERVICE_PATH}", f"auth={MAIL_SERVICE_AUTH}",
    ]
    for p in sorted(set(MAIL_NO_AUTH_PATHS)):
        tags.append(f"no_auth_path={p}")

    check = None
    if getattr(settings, "CONSUL_HEALTH_CHECK_ENABLED", True):
        check = consul.Check.http(
            url=f"http://{health_check_address}:{MAIL_SERVICE_PORT}{MAIL_SERVICE_PATH}/health",
            interval="10s", timeout="5s", deregister="30s",
        )

    register_kwargs = dict(
        name=MAIL_SERVICE_NAME, service_id=service_id, address=service_address,
        port=MAIL_SERVICE_PORT, tags=tags,
    )
    if check is not None:
        register_kwargs["check"] = check

    consul_registry.consul_client.agent.service.register(**register_kwargs)
    logger.info("Mail service registered with Consul: %s (%s:%s, Path: %s)",
                MAIL_SERVICE_NAME, service_address, MAIL_SERVICE_PORT, MAIL_SERVICE_PATH)
    return service_id


def _deregister_mail_from_consul(service_id: str) -> None:
    if not service_id or not consul_registry.consul_client:
        return
    try:
        consul_registry.consul_client.agent.service.deregister(service_id)
        logger.info("Mail service deregistered from Consul: %s", service_id)
    except Exception as exc:
        logger.warning("Mail Consul deregistration failed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service_id = ""
    try:
        service_id = _register_mail_with_consul()
    except Exception as exc:
        logger.warning("Mail Consul registration failed during startup: %s",
                       exc, exc_info=True)
    yield
    _deregister_mail_from_consul(service_id)


app = FastAPI(
    title="Mail Service API",
    version="1.0.0",
    description="Per-user SMTP+IMAP mailboxes with Outlook-style folders for the Recruitment Agent",
    docs_url=f"{MAIL_SERVICE_PATH}/model/api/docs",
    redoc_url=f"{MAIL_SERVICE_PATH}/redoc",
    openapi_url=f"{MAIL_SERVICE_PATH}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(mail_router, prefix=MAIL_SERVICE_PATH)


@app.get(f"{MAIL_SERVICE_PATH}/health")
@app.get("/health")
def health():
    return JSONResponse(status_code=200,
                        content={"status": "ok", "service": "mail",
                                 "port": MAIL_SERVICE_PORT})
