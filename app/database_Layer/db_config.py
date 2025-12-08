"""
Database Configuration Module

This module handles the database connection and session management by:
- Creating the SQLAlchemy engine with the configured database URI
- Setting up the session factory for database operations
- Providing a base class for declarative models
- Managing database session lifecycle

Author: [Supriyo Chowdhury]
Version: 1.0
Last Modified: [2024-05-20]
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.core import settings, log
import logging

logger = logging.getLogger("app_logger")

logger.info("Initializing database configuration")
DB_URI = settings.DB_URI
logger.info(f"Database URI configured successfully, {DB_URI}")

logger.info("Creating database engine")
engine = create_engine(DB_URI)
logger.info("Database engine created successfully")

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
    logger.info("Creating new database session")
    db = SessionLocal()
    try:
        yield db
    finally:
        logger.info("Closing database session")
        db.close()