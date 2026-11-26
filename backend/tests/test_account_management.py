"""
Automated Unit and Integration Tests for Account Management & Multi-User Load Balancer.
Verifies compliance with the 5 Account Management rules:
  1. Only Admin can add or remove user.
  2. Every user should have separate workspace.
  3. Multiple users should be able to log in and use application.
  4. Use a load balancer to smooth operation.
  5. Excel sheet is the sole database for user management.
"""

import os
import shutil
import pytest
from pathlib import Path

from app.core.config import settings, WORKSPACE_DIR
from app.services.account_service import AccountService, account_service
from app.core.load_balancer import load_balancer, MultiUserLoadBalancer
from app.api.accounts import router as accounts_router
from starlette.testclient import TestClient
from app.main import app

client = TestClient(app)


@pytest.fixture
def temp_excel_service(tmp_path):
    """Creates a temporary isolated AccountService using a copy of Account Management.xlsx."""
    excel_src = settings.base_dir / "Account Management.xlsx"
    temp_excel = tmp_path / "Account Management.xlsx"
    shutil.copy(str(excel_src), str(temp_excel))
    return AccountService(excel_path=temp_excel)


def test_rule5_read_users_from_excel(temp_excel_service):
    """Rule 5: Users are loaded directly from Account Management.xlsx without separate DB."""
    users = temp_excel_service.get_all_users(include_passwords=True)
    assert len(users) >= 3

    unames = [u["username"].lower() for u in users]
    assert "achakrabort4" in unames
    assert "crakha1" in unames
    assert "admin" in unames

    # Verify column mappings
    achak = next(u for u in users if u["username"] == "achakrabort4")
    assert achak["name"] == "Archiman Chakraborty"
    assert achak["role"] == "User"
    assert achak["password"] == "Lenovo@123"


def test_rule3_authentication_and_multi_user_login(temp_excel_service):
    """Rule 3: Multiple users can authenticate cleanly."""
    # User 1
    u1 = temp_excel_service.authenticate("achakrabort4", "Lenovo@123")
    assert u1 is not None
    assert u1["username"] == "achakrabort4"
    assert u1["role"] == "User"

    # User 2
    u2 = temp_excel_service.authenticate("crakha1", "Lenovo@123")
    assert u2 is not None
    assert u2["username"] == "crakha1"
    assert u2["role"] == "User"

    # Admin
    u3 = temp_excel_service.authenticate("admin", "admin123")
    assert u3 is not None
    assert u3["username"] == "admin"
    assert u3["role"] == "Admin"

    # Invalid password rejected
    bad_auth = temp_excel_service.authenticate("achakrabort4", "wrongpassword")
    assert bad_auth is None

    # Unknown user rejected
    ghost_auth = temp_excel_service.authenticate("ghost", "pass")
    assert ghost_auth is None


def test_rule2_separate_workspaces(temp_excel_service):
    """Rule 2: Every user has their own separate workspace directory."""
    ws_achak = temp_excel_service.get_user_workspace("achakrabort4")
    ws_crakha = temp_excel_service.get_user_workspace("crakha1")
    ws_admin = temp_excel_service.get_user_workspace("admin")

    assert ws_achak.exists()
    assert ws_crakha.exists()
    assert ws_admin.exists()

    assert ws_achak != ws_crakha
    assert ws_achak.name == "achakrabort4"
    assert ws_crakha.name == "crakha1"
    assert ws_admin.name == "admin"


def test_rule1_admin_only_add_and_remove(temp_excel_service):
    """Rule 1: Only Admin can add or remove users."""
    # 1. Non-admin cannot add user
    with pytest.raises(PermissionError) as exc_add:
        temp_excel_service.add_user(
            name="Alice Wonder",
            username="alice",
            password="secretpassword",
            role="User",
            actor_role="User"  # Non-admin
        )
    assert "Only Admin can add users" in str(exc_add.value)

    # 2. Admin can add user
    new_user = temp_excel_service.add_user(
        name="Alice Wonder",
        username="alice",
        password="secretpassword",
        role="User",
        actor_role="Admin"
    )
    assert new_user["username"] == "alice"
    assert temp_excel_service.get_user_by_username("alice") is not None
    assert temp_excel_service.get_user_workspace("alice").exists()

    # 3. Non-admin cannot remove user
    with pytest.raises(PermissionError) as exc_del:
        temp_excel_service.remove_user("alice", actor_role="User")
    assert "Only Admin can remove users" in str(exc_del.value)

    # 4. Admin can remove user
    removed = temp_excel_service.remove_user("alice", actor_role="Admin")
    assert removed is True
    assert temp_excel_service.get_user_by_username("alice") is None


def test_rule1_admin_only_update_password(temp_excel_service):
    """Admin can update/reset forgotten user passwords in Account Management.xlsx; non-admin blocked."""
    # Non-admin blocked
    with pytest.raises(PermissionError) as exc_upd:
        temp_excel_service.update_password("achakrabort4", "NewPass999", actor_role="User")
    assert "Only Admin can update user passwords" in str(exc_upd.value)

    # Admin updates user password
    res = temp_excel_service.update_password("achakrabort4", "ResetPass@2026", actor_role="Admin")
    assert res["updated"] is True
    assert res["username"] == "achakrabort4"

    # Old password rejected, new password works
    assert temp_excel_service.authenticate("achakrabort4", "Lenovo@123") is None
    authed = temp_excel_service.authenticate("achakrabort4", "ResetPass@2026")
    assert authed is not None
    assert authed["username"] == "achakrabort4"

    # Invalid inputs rejected
    with pytest.raises(ValueError):
        temp_excel_service.update_password("achakrabort4", "ab", actor_role="Admin")
    with pytest.raises(ValueError):
        temp_excel_service.update_password("nonexistent_user", "ValidPass1", actor_role="Admin")


def test_password_reset_api_rbac_and_flow(tmp_path):
    """API PATCH /api/accounts/users/{username}/password enforces Admin RBAC & updates isolated Excel."""
    from unittest.mock import patch
    from app.api import accounts as acc_api_mod

    excel_src = settings.base_dir / "Account Management.xlsx"
    temp_excel = tmp_path / "Account Management.xlsx"
    shutil.copy(str(excel_src), str(temp_excel))
    iso_acc = AccountService(excel_path=temp_excel)

    with patch.object(acc_api_mod, "account_service", iso_acc):
        # Non-admin rejected with 403
        res_forbidden = client.patch(
            "/api/accounts/users/crakha1/password?actor_role=User",
            json={"new_password": "UserTry123"},
        )
        assert res_forbidden.status_code == 403

        # Admin resets password -> 200
        res_ok = client.patch(
            "/api/accounts/users/crakha1/password?actor_role=Admin&actor_username=admin",
            json={"new_password": "FreshPass@456"},
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["success"] is True

        # Verify updated credentials authenticate cleanly
        assert iso_acc.authenticate("crakha1", "Lenovo@123") is None
        assert iso_acc.authenticate("crakha1", "FreshPass@456") is not None


def test_rule4_multi_user_load_balancer():
    """Rule 4: MultiUserLoadBalancer manages concurrency and session tracking."""
    lb = MultiUserLoadBalancer()
    # Register sessions
    s1 = lb.register_session("achakrabort4", "Archiman", "User")
    s2 = lb.register_session("admin", "Admin", "Admin")

    active_sess = lb.get_active_sessions()
    assert len(active_sess) == 2

    # Stats inspection
    stats = lb.get_stats()
    assert "max_concurrency" in stats
    assert stats["max_concurrency"] >= 1
    assert stats["active_sessions"] == 2

    # Unregister
    lb.unregister_session(s1)
    assert len(lb.get_active_sessions()) == 1


def test_accounts_api_endpoints():
    """Verify FastAPI endpoints for authentication, listing, and load balancer stats."""
    # 1. Login endpoint
    res_login = client.post("/api/auth/login", json={"username": "achakrabort4", "password": "Lenovo@123"})
    assert res_login.status_code == 200
    data = res_login.json()
    assert data["status"] == "success"
    assert data["user"]["username"] == "achakrabort4"
    assert "session_id" in data

    # Bad login
    res_bad = client.post("/api/auth/login", json={"username": "achakrabort4", "password": "bad"})
    assert res_bad.status_code == 401

    # 2. List users
    res_users = client.get("/api/accounts/users")
    assert res_users.status_code == 200
    users_list = res_users.json()
    assert len(users_list) >= 3
    # Ensure passwords are never leaked in UserResponse
    for u in users_list:
        assert "password" not in u

    # 3. Rule 1 enforcement in API: Non-admin cannot add user
    res_add_forbidden = client.post(
        "/api/accounts/users?actor_role=User",
        json={"name": "Hacker", "username": "hacker", "password": "123", "role": "Admin"}
    )
    assert res_add_forbidden.status_code == 403

    # 4. Load balancer endpoint (Rule 4)
    res_lb = client.get("/api/accounts/load-balancer")
    assert res_lb.status_code == 200
    lb_data = res_lb.json()
    assert "max_concurrency" in lb_data
    assert "active_requests" in lb_data


def test_chat_isolation_api():
    """Verify chat isolation across users via the FastAPI backend."""
    # 1. User A creates conversation
    res_create = client.post(
        "/api/chat/conversations",
        json={"title": "Private Research Achak", "model": "gemma4:12b", "user_id": "achakrabort4"}
    )
    assert res_create.status_code == 200
    conv_a_id = res_create.json()["id"]

    # 2. User B lists conversations -> conv_a_id must NOT be in User B's list
    res_list_b = client.get("/api/chat/conversations?user_id=crakha1")
    assert res_list_b.status_code == 200
    b_conv_ids = [c["id"] for c in res_list_b.json()]
    assert conv_a_id not in b_conv_ids

    # 3. User B tries to directly fetch User A's conversation -> 404 access restricted
    res_get_b = client.get(f"/api/chat/conversations/{conv_a_id}?user_id=crakha1")
    assert res_get_b.status_code == 404

    # 4. User A fetches their own conversation -> 200 OK
    res_get_a = client.get(f"/api/chat/conversations/{conv_a_id}?user_id=achakrabort4")
    assert res_get_a.status_code == 200

    # Clean up
    client.delete(f"/api/chat/conversations/{conv_a_id}?user_id=achakrabort4")
