"""
Account Management & Authentication REST Endpoints.
Backed directly by Account Management.xlsx without separate database.
Implements Rule 1: Only Admin can add or remove users.
Implements Rule 2: Separate workspace provisioning per user.
Implements Rule 3: Concurrent multi-user session support.
Implements Rule 4: Multi-user load balancer metrics.
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.account import (
    UserLoginRequest,
    UserResponse,
    UserCreateRequest,
    UserDeleteResponse,
    UserPasswordUpdateRequest,
    UserPasswordUpdateResponse,
    LoadBalancerStats
)
from app.services.account_service import account_service
from app.services.audit_service import audit_service
from app.core.load_balancer import load_balancer

router = APIRouter(tags=["Account Management & Auth"])


@router.post("/auth/login", response_model=dict)
async def login_endpoint(payload: UserLoginRequest):
    """Authenticates credentials against Account Management.xlsx."""
    user = account_service.authenticate(payload.username, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )
    session_id = load_balancer.register_session(user["username"], user["name"], user["role"])
    audit_service.record_event(
        username=user["username"],
        user_role=user["role"],
        event_type="login",
        details="REST API login",
    )
    return {
        "status": "success",
        "user": user,
        "session_id": session_id,
        "workspace": user["workspace_path"]
    }


@router.post("/auth/logout")
async def logout_endpoint(session_id: Optional[str] = Query(default=None)):
    """Logs out user session and frees load balancer tracking."""
    if session_id:
        sess_info = next((s for s in load_balancer.get_active_sessions() if s["session_id"] == session_id), None)
        load_balancer.unregister_session(session_id)
        if sess_info:
            audit_service.record_event(
                username=sess_info.get("username", "anonymous"),
                user_role=sess_info.get("role", "User"),
                event_type="logout",
                details="REST API logout",
            )
    return {"status": "success", "message": "Logged out successfully"}


@router.get("/accounts/users", response_model=List[UserResponse])
async def list_users_endpoint():
    """Returns all active users registered in Account Management.xlsx."""
    users = account_service.get_all_users(include_passwords=False)
    return [
        UserResponse(
            name=u["name"],
            username=u["username"],
            role=u["role"],
            workspace_path=u["workspace_path"]
        )
        for u in users
    ]


@router.post("/accounts/users", response_model=UserResponse)
async def create_user_endpoint(
    payload: UserCreateRequest,
    actor_role: str = Query(default="Admin", description="Role of the operating user (Rule 1: Admin only)")
):
    """
    Adds a new user to Account Management.xlsx and provisions private workspace.
    Rule 1: Only Admin can add or remove users.
    """
    if actor_role.strip().lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Only Admin can add users (Account Management Rule 1)."
        )
    try:
        created = account_service.add_user(
            name=payload.name,
            username=payload.username,
            password=payload.password,
            role=payload.role,
            actor_role=actor_role
        )
        audit_service.record_event(
            username="admin",
            user_role=actor_role,
            event_type="admin_user_add",
            details=f"Added user @{created['username']} ({created['role']})",
        )
        return UserResponse(
            name=created["name"],
            username=created["username"],
            role=created["role"],
            workspace_path=created["workspace_path"]
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.delete("/accounts/users/{username}", response_model=UserDeleteResponse)
async def delete_user_endpoint(
    username: str,
    actor_role: str = Query(default="Admin", description="Role of the operating user (Rule 1: Admin only)")
):
    """
    Removes a user from Account Management.xlsx.
    Rule 1: Only Admin can add or remove users.
    """
    if actor_role.strip().lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Only Admin can remove users (Account Management Rule 1)."
        )
    try:
        account_service.remove_user(username=username, actor_role=actor_role)
        audit_service.record_event(
            username="admin",
            user_role=actor_role,
            event_type="admin_user_remove",
            details=f"Removed user @{username}",
        )
        return UserDeleteResponse(
            success=True,
            message=f"User '{username}' was successfully removed from Account Management.xlsx.",
            deleted_username=username
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.patch("/accounts/users/{username}/password", response_model=UserPasswordUpdateResponse)
async def update_user_password_endpoint(
    username: str,
    payload: UserPasswordUpdateRequest,
    actor_role: str = Query(default="Admin", description="Role of the operating user (Rule 1: Admin only)"),
    actor_username: str = Query(default="admin", description="Username of operating Admin"),
):
    """
    Updates/resets a user's password inside Account Management.xlsx.
    Rule 1: Only Admin can update user passwords.
    """
    if actor_role.strip().lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Only Admin can update user passwords (Account Management Rule 1).",
        )
    try:
        updated = account_service.update_password(
            username=username,
            new_password=payload.new_password,
            actor_role=actor_role,
        )
        audit_service.record_event(
            username=actor_username,
            user_role=actor_role,
            event_type="admin_password_update",
            details=f"Updated password for @{updated['username']}",
        )
        return UserPasswordUpdateResponse(
            success=True,
            message=f"Password for '@{updated['username']}' updated successfully in Account Management.xlsx.",
            username=updated["username"],
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/accounts/load-balancer", response_model=LoadBalancerStats)
async def get_load_balancer_stats():
    """Returns real-time load balancer and multi-user concurrency telemetry (Rule 4)."""
    stats = load_balancer.get_stats()
    return LoadBalancerStats(**stats)


@router.get("/accounts/audit")
async def get_admin_audit_analytics(
    actor_role: str = Query(default="User", description="Role of the requesting user (Admin only)"),
    days: Optional[int] = Query(default=30, ge=0, le=3650, description="Lookback window in days (0 = all time)"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Returns aggregated usage audit analytics and user activity breakdown (Admin only)."""
    if actor_role.strip().lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Only Admin can access Usage Audit & Analytics.",
        )
    users = account_service.get_all_users(include_passwords=False)
    active_sessions = load_balancer.get_active_sessions()
    effective_days = None if days == 0 else days
    return audit_service.get_dashboard_analytics(
        days=effective_days,
        users=users,
        active_sessions=active_sessions,
        event_limit=limit,
    )
