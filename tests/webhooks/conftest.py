"""Fixtures for the outgoing webhook tests.

No real DB / Redis / network: settings get dummy values, DB tests use
in-memory SQLite with only the webhooks_layer tables, and HTTP is faked.
"""
import os
import tempfile

from cryptography.fernet import Fernet

# Dummy settings so `app.core` imports without a .env (never real credentials).
_DUMMY_ENV = {
    "APP_ENV": "dev", "DB_HOST": "127.0.0.1", "DB_PORT": "3306", "DB_NAME": "test",
    "DB_USER": "test", "DB_PASSWORD": "test", "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "6399",
    "REDIS_DB": "0", "REDIS_PASSWORD": "", "CONSUL_ENABLED": "false",
    "AUTH_SERVICE_URL": "http://127.0.0.1:9/verify", "ACCESS_TOKEN_EXPIRE_HOURS": "1",
    "JWT_SECRET_KEY": "test", "JWT_ALGORITHM": "HS256", "BASE_URL": "http://127.0.0.1:9",
    "WHATSAPP_PROVIDER_MODE": "mock",
    "WHATSAPP_CRED_KEY": Fernet.generate_key().decode(),
    "JOB_SERVICE_URL": "http://job.invalid", "ORG_SERVICE_URL": "http://org.invalid",
    "INTERNAL_SERVICE_TOKEN": "test-token", "PUBLIC_API_BASE_URL": "https://api.example.test",
}
for _k, _v in _DUMMY_ENV.items():
    os.environ.setdefault(_k, _v)
os.environ.setdefault("LOG_FILE_PATH", os.path.join(tempfile.gettempdir(), "status_service_test_logs"))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture
def wh_db():
    from app.database_Layer.db_config import Base
    from app.webhooks_layer import models as m

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine, tables=[m.WebhookEndpoint.__table__, m.WebhookDelivery.__table__])
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise."""
    import socket

    def fake(host, port, *a, **kw):
        address = {"internal.example.test": "10.0.0.5", "localhost": "127.0.0.1"}.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
