"""
Get Running Resume API Endpoint

This endpoint retrieves running resume parser tasks from task_logs table.
Supports role-based access control:
- Admin/Super Admin: Returns all running resume parser tasks
- User: Returns only their own running resume parser tasks
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.api.endpoints.dependencies.auth_utils import validate_token, check_admin_access
from app.database_Layer.db_config import get_db
from app.database_Layer.db_model import TaskLogs

logger = logging.getLogger("app_logger")

router = APIRouter()


class RunningResumeResponse(BaseModel):
    """Response model for running resume tasks"""
    task_id: str
    status: Optional[str]
    type: str

    class Config:
        from_attributes = True


@router.get("/get_running_resume", response_model=List[RunningResumeResponse])
async def get_running_resume(
    user_info: dict = Depends(validate_token),
    db: Session = Depends(get_db)
):
    """
    Get running resume parser tasks.
    
    For admin/super_admin: Returns all running resume parser tasks.
    For regular users: Returns only their own running resume parser tasks.
    
    Args:
        user_info: User information from JWT token (user_id, role_id, role_name)
        db: Database session
        
    Returns:
        List of running resume parser tasks with task_id, status, and type
    """
    try:
        user_id = user_info.get('user_id')
        role_name = user_info.get('role_name', '').lower()
        
        logger.info(f"Fetching running resume tasks for user_id: {user_id}, role: {role_name}")
        
        # Base query: filter by type='resume_parser' and status not in ['completed', 'failed']
        query = db.query(TaskLogs).filter(
            and_(
                TaskLogs.type == 'resume_parser',
                ~TaskLogs.status.in_(['completed', 'failed'])
            )
        )
        
        # For regular users, filter by key_id matching user_id
        if not check_admin_access(role_name):
            query = query.filter(TaskLogs.key_id == user_id)
            logger.info(f"Filtering by user_id: {user_id}")
        
        # Execute query
        tasks = query.all()
        
        # Format response
        result = [
            RunningResumeResponse(
                task_id=task.task_id,
                status=task.status,
                type=task.type
            )
            for task in tasks
        ]
        
        logger.info(f"Found {len(result)} running resume tasks")
        return result
        
    except Exception as e:
        logger.error(f"Error fetching running resume tasks: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )

