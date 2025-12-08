import os
from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv
from urllib.parse import quote_plus
import logging

# Load environment variables from .env file
load_dotenv(override=True)

logger = logging.getLogger("app_logger")

class Settings(BaseSettings):
    """
    Settings class to manage application configuration.
    Uses Pydantic's BaseSettings to handle environment and default values.
    Values mirrored from .env as of 2024-06.
    """

    # Application
    APP_NAME: str = "Status service"
    DEBUG: bool = False  # Not exposed in .env, but can be referenced

    # Logging
    STATUS_SERVICE_LOG: str = Field(default_factory=lambda: os.getenv("STATUS_AGENT_LOG", "D:\\LOGS"), env="STATUS_AGENT_LOG")

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
    BASE_URL: str = Field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8115"), env="BASE_URL")

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

# Create a global settings instance
settings = Settings()