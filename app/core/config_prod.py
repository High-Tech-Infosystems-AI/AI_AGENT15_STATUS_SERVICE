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
    Settings class to manage application configuration
    Uses Pydantic's BaseSettings to handle environment and default values.
    Values mirrored from .env as of 2024-06.
    """

    # Application
    APP_NAME: str = "Status service"
    DEBUG: bool = False  # Not exposed in .env, but can be referenced

    # Logging configuration
    STATUS_AGENT_LOG: str = Field("./logs", env="LOG_FILE_PATH")
    LOG_LEVEL: str = Field("INFO", env="LOG_LEVEL")
    LOG_TO_CONSOLE: bool = Field(True, env="LOG_TO_CONSOLE")

    # Auth Service
    AUTH_SERVICE_URL: str = Field(default_factory=lambda: os.getenv("AUTH_SERVICE_URL", "http://localhost:8085/ats/verify-token"), env="AUTH_SERVICE_URL")

    # JWT
    ACCESS_TOKEN_EXPIRE_HOURS: int = Field(default_factory=lambda: int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "24")), env="ACCESS_TOKEN_EXPIRE_HOURS")
    JWT_SECRET_KEY: str = Field(default_factory=lambda: os.getenv("JWT_SECRET_KEY", "h7ahasye8172#as819adh1COD797mTdAAA"), env="JWT_SECRET_KEY")
    JWT_ALGORITHM: str = Field(default_factory=lambda: os.getenv("JWT_ALGORITHM", "HS256"), env="JWT_ALGORITHM")

    # Database
    DB_HOST: str = Field(default_factory=lambda: os.getenv("DB_HOST", "localhost"), env="DB_HOST")
    DB_PORT: str = Field(default_factory=lambda: os.getenv("DB_PORT", "3306"), env="DB_PORT")
    DB_NAME: str = Field(default_factory=lambda: os.getenv("DB_NAME", "ats_main"), env="DB_NAME")
    DB_USER: str = Field(default_factory=lambda: os.getenv("DB_USER", "root"), env="DB_USER")
    DB_PASSWORD: str = Field(default_factory=lambda: os.getenv("DB_PASSWORD", "hti@123"), env="DB_PASSWORD")

    # Redis
    REDIS_HOST: str = Field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"), env="REDIS_HOST")
    REDIS_PORT: int = Field(default_factory=lambda: int(os.getenv("REDIS_PORT", "6380")), env="REDIS_PORT")
    REDIS_DB: int = Field(default_factory=lambda: int(os.getenv("REDIS_DB", "0")), env="REDIS_DB")
    REDIS_PASSWORD: str = Field(default_factory=lambda: os.getenv("REDIS_PASSWORD", ""), env="REDIS_PASSWORD")

    # File Storage (not present in prod .env, left as blank default)
    FILE_STORING_PATH: str = Field(default_factory=lambda: os.getenv("FILE_STORING_PATH", ""), env="FILE_STORING_PATH")

    # Base URL
    BASE_URL: str = Field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8515"), env="BASE_URL")

    # AWS / S3 — chat attachments
    AWS_ACCESS_KEY_ID: str = Field(default_factory=lambda: os.getenv("AWS_ACCESS_KEY_ID", ""), env="AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str = Field(default_factory=lambda: os.getenv("AWS_SECRET_ACCESS_KEY", ""), env="AWS_SECRET_ACCESS_KEY")
    AWS_REGION: str = Field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-1"), env="AWS_REGION")
    AWS_S3_ENDPOINT_URL: str = Field(default_factory=lambda: os.getenv("AWS_S3_ENDPOINT_URL", ""), env="AWS_S3_ENDPOINT_URL")
    AWS_S3_BUCKET_CHAT: str = Field(default_factory=lambda: os.getenv("AWS_S3_BUCKET_CHAT", ""), env="AWS_S3_BUCKET_CHAT")
    AWS_S3_PRESIGNED_TTL_SECONDS: int = Field(default_factory=lambda: int(os.getenv("AWS_S3_PRESIGNED_TTL_SECONDS", "3600")), env="AWS_S3_PRESIGNED_TTL_SECONDS")

    # Consul service discovery (optional)
    CONSUL_HOST: str = Field(default_factory=lambda: os.getenv("CONSUL_HOST", "localhost"), env="CONSUL_HOST")
    CONSUL_PORT: int = Field(default_factory=lambda: int(os.getenv("CONSUL_PORT", "8500")), env="CONSUL_PORT")
    CONSUL_ENABLED: bool = Field(default_factory=lambda: os.getenv("CONSUL_ENABLED", "true").lower() in ("true", "1", "yes"), env="CONSUL_ENABLED")
    CONSUL_HEALTH_CHECK_ENABLED: bool = Field(default_factory=lambda: os.getenv("CONSUL_HEALTH_CHECK_ENABLED", "false").lower() in ("true", "1", "yes"), env="CONSUL_HEALTH_CHECK_ENABLED")
    CONSUL_SERVICE_NAME: str = Field(default_factory=lambda: os.getenv("STATUS_SERVICE_NAME", "HRMIS_STATUS_SERVICE"), env="STATUS_SERVICE_NAME")
    CONSUL_SERVICE_PORT: int = Field(default_factory=lambda: _parse_port(os.getenv("STATUS_SERVICE_PORT", "8115"), 8115), env="STATUS_SERVICE_PORT")
    CONSUL_SERVICE_EXTERNAL_PORT: Optional[int] = None
    CONSUL_SERVICE_EXTERNAL_IP: str = Field(default_factory=lambda: os.getenv("CONSUL_SERVICE_EXTERNAL_IP", ""), env="CONSUL_SERVICE_EXTERNAL_IP")
    CONSUL_SERVICE_PATH: str = Field(default_factory=lambda: os.getenv("STATUS_SERVICE_PATH", "/status"), env="STATUS_SERVICE_PATH")
    CONSUL_SERVICE_AUTH: str = Field(default_factory=lambda: os.getenv("CONSUL_SERVICE_AUTH", "mixed"), env="CONSUL_SERVICE_AUTH")

    @field_validator("CONSUL_SERVICE_EXTERNAL_PORT", mode="before")
    @classmethod
    def _validate_consul_service_external_port(cls, v):
        if v in (None, ""):
            return None
        return int(v)

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