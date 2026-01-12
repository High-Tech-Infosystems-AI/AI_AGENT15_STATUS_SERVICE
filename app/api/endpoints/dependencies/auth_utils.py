import requests
from fastapi import HTTPException, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core import settings
import logging

logger = logging.getLogger("app_logger")

security = HTTPBearer()

# Token validation function with role-based access control
async def validate_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Enhanced token validation function that extracts user info and role from JWT token.
    
    Returns:
        dict: Contains token, user_id, role_id, and role_name
    """
    token = credentials.credentials
    try:
        response = requests.post(
            f"{settings.AUTH_SERVICE_URL}",
            params={"token": token},
            headers={"accept": "application/json"}
        )
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired token"
            )
        
        # Extract user info from response
        token_info = response.json()
        user_id = token_info.get('user_id')
        role_id = token_info.get('role_id')
        role_name = token_info.get('role_name')
        
        if not user_id or not role_id or not role_name:
            raise HTTPException(
                status_code=401,
                detail="Token missing required user information"
            )
        
        return {
            'token': token,
            'user_id': user_id,
            'role_id': role_id,
            'role_name': role_name
        }
    except Exception as e:
        logger.error(f"Token validation error: {str(e)}")
        raise HTTPException(
            status_code=401,
            detail="Invalid token or authentication service unavailable"
        )

def check_admin_access(role_name: str) -> bool:
    """
    Check if the user has admin or super_admin access.
    
    Args:
        role_name (str): The role name from the token
        
    Returns:
        bool: True if admin access, False otherwise
    """
    return role_name.lower() in ['admin', 'super_admin']

def check_user_candidate_access(user_id: int, candidate_created_by: int, candidate_assigned_to: int = None) -> bool:
    """
    Check if a user has access to a candidate based on role-based rules.
    
    Args:
        user_id (int): The user ID from the token
        candidate_created_by (int): The user ID who created the candidate
        candidate_assigned_to (int, optional): The user ID assigned to the candidate
        
    Returns:
        bool: True if user has access, False otherwise
    """
    # User can access candidates they created or are assigned to
    return user_id == candidate_created_by or user_id == candidate_assigned_to


