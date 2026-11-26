from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class UserLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Username for authentication")
    password: str = Field(..., min_length=1, description="User password")

class UserResponse(BaseModel):
    name: str
    username: str
    role: str
    workspace_path: str

class UserCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Display name of the user")
    username: str = Field(..., min_length=2, description="Unique username")
    password: str = Field(..., min_length=3, description="Password for the user")
    role: str = Field(default="User", description="Role: 'User' or 'Admin'")

class UserDeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_username: str

class UserPasswordUpdateRequest(BaseModel):
    new_password: str = Field(..., min_length=3, description="New password for the user")

class UserPasswordUpdateResponse(BaseModel):
    success: bool
    message: str
    username: str

class LoadBalancerStats(BaseModel):
    active_requests: int
    queued_requests: int
    max_concurrency: int
    hardware_profile: str
    active_sessions: int
    total_completed_requests: int
    user_request_counts: Dict[str, int]
