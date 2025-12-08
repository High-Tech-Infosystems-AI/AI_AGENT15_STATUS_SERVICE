"""
Database Schema Module

This module contains all SQLAlchemy ORM models for the application.

Author: [Supriyo Chowdhury]
Version: 1.0
Last Modified: [2024-05-20]
"""

import logging
from sqlalchemy import Column, Integer, String, DateTime, TIMESTAMP, Text, Boolean, Date, DECIMAL, ForeignKey, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.mysql import TINYINT
from app.database_Layer.db_config import Base

logger = logging.getLogger("app_logger")


class TaskLogs(Base):
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(100), nullable=False)
    type = Column(String(100), nullable=False)
    key_id = Column(Integer, nullable=True)
    status = Column(String(50), nullable=True)
    error = Column(String(250), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class Role(Base):
    """Role model for storing user roles"""
    __tablename__ = 'roles'

    logger.info("Configuring Role model fields")
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=True)
    logger.info("Role model configured successfully")


class User(Base):
    """User model for storing user account information"""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    updated_at = Column(TIMESTAMP)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    role_id = Column(Integer, ForeignKey('roles.id'), nullable=True, index=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(Integer)

    # Explicit foreign key relationships
    role = relationship("Role", foreign_keys=[role_id])
    logger.info("User model configured successfully")


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(String(64), nullable=False, unique=True)
    company_name = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    city = Column(String(100))
    state = Column(String(100))
    country = Column(String(100))
    industry = Column(String(150), nullable=False)
    employee_count = Column(String(50))
    website = Column(String(2083))
    status = Column(String(50), default="Active")
    remarks = Column(Text)
    created_at = Column(TIMESTAMP, server_default=func.now())
    created_by = Column(Integer, ForeignKey("users.id"))
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, ForeignKey("users.id"))
    deleted_at = Column(DateTime)
    deleted_by = Column(Integer, ForeignKey("users.id"))

    spocs = relationship("CompanySpoc", back_populates="company")
    created_by_user = relationship("User", foreign_keys=[created_by])
    updated_by_user = relationship("User", foreign_keys=[updated_by])


class CompanySpoc(Base):
    __tablename__ = "company_spoc"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    spoc_name = Column(String(150))
    spoc_email = Column(String(254))
    spoc_ph_number = Column(String(20))
    escalation_matrix = Column(Integer)
    created_at = Column(TIMESTAMP, server_default=func.now())
    created_by = Column(Integer, ForeignKey("users.id"))
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    updated_by = Column(Integer, ForeignKey("users.id"))
    deleted_at = Column(DateTime)
    deleted_by = Column(Integer, ForeignKey("users.id"))

    company = relationship("Company", back_populates="spocs")
    created_by_user = relationship("User", foreign_keys=[created_by])
    updated_by_user = relationship("User", foreign_keys=[updated_by])


class UserSession(Base):
    """Session model for tracking user login sessions"""
    __tablename__ = 'sessions'

    logger.info("Configuring Session model fields")
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    session_id = Column(String(255), unique=True, nullable=False, index=True)
    login_at = Column(TIMESTAMP, default="CURRENT_TIMESTAMP")
    is_active = Column(Boolean, default=True)
    user = relationship("User")
    logger.info("Session model configured successfully")


class JobOpenings(Base):
    """Job Openings model for storing job posting information"""
    __tablename__ = 'job_openings'

    logger.info("Configuring JobOpenings model fields")

    # Primary key
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # External/public identifier
    job_id = Column(String(100), unique=True, nullable=False, index=True)

    # Foreign keys
    company_id = Column(Integer, ForeignKey('companies.id'), nullable=False, index=True)
    main_spoc_id = Column(Integer, ForeignKey('company_spoc.id'), nullable=True, index=True)
    pipeline_id = Column(Integer, ForeignKey('pipelines.id'), nullable=True, index=True)

    # Basic job information
    title = Column(String(150), nullable=False)
    location = Column(String(150), nullable=False)
    internal_spoc_id = Column(Integer, nullable=True)
    stage = Column(String(50), nullable=True)
    deadline = Column(Date, nullable=False)
    job_type = Column(String(50), nullable=False)  # FULL_TIME, PART_TIME, CONTRACT
    remote = Column(TINYINT(1), nullable=False, default=0)  # true/false
    openings = Column(Integer, nullable=False)  # number of vacancies
    work_mode = Column(String(30), nullable=True)  # ONSITE, REMOTE, HYBRID
    status = Column(String(30), nullable=False, default='ACTIVE')

    # Salary information
    salary_type = Column(String(30), nullable=True)  # YEARLY, MONTHLY, HOURLY
    currency = Column(String(3), nullable=True)  # ISO-4217
    min_salary = Column(DECIMAL(12, 2), nullable=True)
    max_salary = Column(DECIMAL(12, 2), nullable=True)

    # Requirements
    skills_required = Column(Text, nullable=True)
    min_exp = Column(DECIMAL(4, 1), nullable=True)
    max_exp = Column(DECIMAL(4, 1), nullable=True)
    min_age = Column(TINYINT, nullable=True)
    max_age = Column(TINYINT, nullable=True)
    education_qualification = Column(String(120), nullable=True)
    educational_specialization = Column(String(120), nullable=True)
    gender_preference = Column(String(20), nullable=True)
    communication = Column(TINYINT(1), nullable=True)
    cooling_period = Column(DECIMAL(4, 1), nullable=True)  # months

    # Additional fields
    bulk = Column(TINYINT(1), nullable=True)
    remarks = Column(String(255), nullable=True)

    # Audit fields
    created_at = Column(DateTime, nullable=False, default=func.current_timestamp())
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    updated_at = Column(DateTime, nullable=True)
    updated_by = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(Integer, nullable=True)

    # Relationships (only User relationships since other tables don't exist yet)
    creator = relationship("User", foreign_keys=[created_by])
    updater = relationship("User", foreign_keys=[updated_by])

    logger.info("JobOpenings model configured successfully")


class JD(Base):
    __tablename__ = 'job_descriptions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    jd_id = Column(String(255), nullable=False)                # Logical identifier used to group versions
    job_id = Column(Integer, ForeignKey("job_openings.id"), nullable=False)
    jd_version = Column(Integer, nullable=False, default=1)
    update_version = Column(Integer, nullable=False, default=0)
    status = Column(String(50), nullable=False)
    jd = Column(String, nullable=False)
    created_by = Column(Integer, nullable=True)
    created_on = Column(TIMESTAMP, server_default="CURRENT_TIMESTAMP")
    updated_by = Column(Integer, nullable=True)
    updated_on = Column(TIMESTAMP, nullable=True)

