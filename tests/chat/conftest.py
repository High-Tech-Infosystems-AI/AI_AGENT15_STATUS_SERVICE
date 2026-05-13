"""Shared pytest fixtures for chat module tests."""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")


@pytest.fixture(scope="session")
def sqlite_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(sqlite_engine):
    """Per-test transactional session that rolls back on teardown."""
    from app.database_Layer.db_config import Base
    Base.metadata.create_all(sqlite_engine)
    SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(sqlite_engine)


@pytest.fixture
def fake_user_active():
    return {"id": 100, "name": "Alice", "username": "alice", "enable": 1,
            "deleted_at": None, "role_id": 3, "role_name": "Recruiter"}


@pytest.fixture
def fake_user_admin():
    return {"id": 1, "name": "Admin", "username": "admin", "enable": 1,
            "deleted_at": None, "role_id": 1, "role_name": "SuperAdmin"}
