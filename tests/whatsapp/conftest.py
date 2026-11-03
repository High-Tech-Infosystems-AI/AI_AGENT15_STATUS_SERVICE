"""Fixtures for the clawback WhatsApp tests.

No real DB / Redis / provider is ever touched: settings get dummy values,
the provider runs in mock mode, and DB tests use in-memory SQLite with only
the whatsapp_layer tables.
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
    # Throwaway Fernet key generated per test run.
    "WHATSAPP_CRED_KEY": Fernet.generate_key().decode(),
    "JOB_SERVICE_URL": "http://job.invalid", "ORG_SERVICE_URL": "http://org.invalid",
    "INTERNAL_SERVICE_TOKEN": "test-token", "PUBLIC_API_BASE_URL": "https://api.example.test",
}
for _k, _v in _DUMMY_ENV.items():
    os.environ.setdefault(_k, _v)
os.environ.setdefault("LOG_FILE_PATH", os.path.join(tempfile.gettempdir(), "status_service_test_logs"))

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture
def wa_db():
    """In-memory SQLite session with only the whatsapp_layer tables.
    pysqlite needs the BEGIN/SAVEPOINT workaround for begin_nested()."""
    from app.database_Layer.db_config import Base
    from app.whatsapp_layer import models as m

    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    tables = [mdl.__table__ for mdl in (
        m.WhatsAppProviderAccount, m.WhatsAppTemplate, m.ClawbackNotificationSettings,
        m.ClawbackNotificationRule, m.ClawbackDigestRule, m.ClawbackHealthCheck,
        m.WhatsAppMessageLog, m.WhatsAppWebhookEvent, m.WhatsAppOptOut)]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
