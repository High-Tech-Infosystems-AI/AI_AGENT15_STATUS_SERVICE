"""
Database Configuration Module

This module handles the database connection and session management by:
- Creating the SQLAlchemy engine with the configured database URI and connection pooling
- Setting up the session factory for database operations
- Providing a base class for declarative models
- Managing database session lifecycle

Author: [Supriyo Chowdhury]
Version: 1.0
Last Modified: [2024-05-20]
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.core import settings, log
import logging

logger = logging.getLogger("app_logger")

logger.info("Initializing database configuration")
DB_URI = settings.DB_URI


def masked_db_uri(uri) -> str:
    """The URI with its password replaced by ***, safe to log."""
    try:
        return make_url(uri).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable database URI>"


logger.info(f"Database URI configured successfully, {masked_db_uri(DB_URI)}")

logger.info("Creating database engine with connection pooling")
engine = create_engine(
    DB_URI,
    pool_size=10,              # Number of connections to maintain in the pool
    max_overflow=20,           # Maximum number of connections beyond pool_size
    pool_timeout=30,           # Timeout in seconds for getting a connection from the pool
    pool_recycle=3600,         # Recycle connections after 1 hour (prevents stale connections)
    pool_pre_ping=True,        # Test connections before using them (recommended for production)
    echo=False                 # Set to True for SQL query logging (useful for debugging)
)
logger.info("Database engine created successfully with pooling enabled")

logger.info("Setting up session factory")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
logger.info("Session factory configured successfully")

Base = declarative_base()
logger.info("Declarative base class created")

def get_db():
    """
    Database session dependency generator.
    
    Yields:
        Session: SQLAlchemy database session
    
    Ensures proper cleanup of database sessions after use.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()