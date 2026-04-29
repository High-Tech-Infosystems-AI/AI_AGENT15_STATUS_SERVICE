import os
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from dotenv import load_dotenv
from urllib.parse import quote_plus
import logging

# Load environment variables from .env file
load_dotenv(override=True)

logger = logging.getLogger("app_logger")


def _parse_port(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        if isinstance(value, str) and ":" in value:
            tail = value.rsplit(":", 1)[-1]
            if tail.isdigit():
                return int(tail)
        logger.warning("Invalid port value '%s'. Falling back to %s", value, default)
        return default

class Settings(BaseSettings):
    """
    Settings class to manage application configuration.
    Uses Pydantic's BaseSettings to handle environment and default values.
    Values mirrored from .env as of 2024-06.
    """

    # Application
    APP_NAME: str = "Status service"
    DEBUG: bool = False

    # Consul service discovery (optional)
    CONSUL_HOST: str = os.getenv("CONSUL_HOST", "localhost")
    CONSUL_PORT: int = int(os.getenv("CONSUL_PORT", "8500"))
    CONSUL_ENABLED: bool = os.getenv("CONSUL_ENABLED", "true").lower() in ("true", "1", "yes")
    CONSUL_HEALTH_CHECK_ENABLED: bool = os.getenv("CONSUL_HEALTH_CHECK_ENABLED", "false").lower() in ("true", "1", "yes")
    CONSUL_SERVICE_NAME: str = os.getenv("STATUS_SERVICE_NAME", "HRMIS_STATUS_SERVICE")
    CONSUL_SERVICE_PORT: int = _parse_port(os.getenv("STATUS_SERVICE_PORT", "8515"), 8515)
    CONSUL_SERVICE_EXTERNAL_PORT: Optional[int] = None
    CONSUL_SERVICE_EXTERNAL_IP: str = os.getenv("CONSUL_SERVICE_EXTERNAL_IP", "")
    CONSUL_SERVICE_PATH: str = os.getenv("STATUS_SERVICE_PATH", "/status")
    CONSUL_SERVICE_AUTH: str = os.getenv("CONSUL_SERVICE_AUTH", "mixed")

    @field_validator("CONSUL_SERVICE_EXTERNAL_PORT", mode="before")
    @classmethod
    def _validate_consul_service_external_port(cls, v):
        if v in (None, ""):
            return None
        return int(v)

    # Logging configuration
    STATUS_AGENT_LOG: str = os.getenv("LOG_FILE_PATH", "./logs")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_TO_CONSOLE: bool = os.getenv("LOG_TO_CONSOLE", "true").lower() in ("true", "1", "yes")
    AUTH_SERVICE_URL: str = os.getenv("AUTH_SERVICE_URL")
    ACCESS_TOKEN_EXPIRE_HOURS: int = os.getenv("ACCESS_TOKEN_EXPIRE_HOURS")
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM")
    DB_HOST: str = os.getenv("DB_HOST")
    DB_PORT: str = os.getenv("DB_PORT")
    DB_NAME: str = os.getenv("DB_NAME")
    DB_USER: str = os.getenv("DB_USER")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD")
    REDIS_HOST: str = os.getenv("REDIS_HOST")
    REDIS_PORT: int = os.getenv("REDIS_PORT")
    REDIS_DB: int = int(os.getenv("REDIS_DB"))
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD")

    BASE_URL: str = os.getenv("BASE_URL")

    # AWS / S3 — chat attachments
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    AWS_S3_ENDPOINT_URL: str = os.getenv("AWS_S3_ENDPOINT_URL", "")
    AWS_S3_BUCKET_CHAT: str = os.getenv("AWS_S3_BUCKET_CHAT", "")
    AWS_S3_BUCKET_PROFILES: str = os.getenv("AWS_S3_BUCKET_PROFILES", "")
    AWS_S3_PRESIGNED_TTL_SECONDS: int = int(os.getenv("AWS_S3_PRESIGNED_TTL_SECONDS", "3600"))

    # Web Push (VAPID) — used by chat to deliver browser push notifications.
    # If both keys are empty, the service auto-generates a keypair on first
    # access and persists it to VAPID_KEYS_FILE so subsequent restarts reuse
    # the same identity (avoids invalidating user subscriptions).
    VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_SUBJECT: str = os.getenv("VAPID_SUBJECT", "mailto:admin@hrmis.local")
    VAPID_KEYS_FILE: str = os.getenv("VAPID_KEYS_FILE", "./vapid_keys.json")
    WEB_PUSH_FRONTEND_BASE_URL: str = os.getenv("WEB_PUSH_FRONTEND_BASE_URL", "http://localhost:5173")

    # AI Chatbot (Gemini, MCP, quotas)
    GEMINI_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GEMINI_PRO_MODEL: str = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
    GEMINI_FLASH_MODEL: str = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
    AI_DEFAULT_DAILY_LIMIT: int = int(os.getenv("AI_DEFAULT_DAILY_LIMIT", "50000"))
    AI_DEFAULT_MONTHLY_LIMIT: int = int(os.getenv("AI_DEFAULT_MONTHLY_LIMIT", "1000000"))
    AI_REDIS_DB: int = int(os.getenv("AI_REDIS_DB", "6"))
    AI_SESSION_TTL_SECONDS: int = int(os.getenv("AI_SESSION_TTL_SECONDS", "86400"))
    AI_MAX_TOOL_ITER: int = int(os.getenv("AI_MAX_TOOL_ITER", "5"))
    AI_TOOL_TIMEOUT_SECONDS: int = int(os.getenv("AI_TOOL_TIMEOUT_SECONDS", "8"))
    MCP_MYSQL_COMMAND: str = os.getenv("MCP_MYSQL_COMMAND", "")
    MCP_MYSQL_ARGS: str = os.getenv("MCP_MYSQL_ARGS", "")

    @property
    def DB_URI(self) -> str:
        # Use mysql+mysqlconnector as ORM dialect and driver
        encoded_password = quote_plus(self.DB_PASSWORD)
        uri = f"mysql+mysqlconnector://{self.DB_USER}:{encoded_password}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        logger.debug(f"Database URI (password masked): {uri.replace(encoded_password, '****')}")
        return uri

    class Config:
        """
        Pydantic settings config.
        """
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

# Create a global settings instance
settings = Settings()