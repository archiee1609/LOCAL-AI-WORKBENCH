"""
Automated Tests for Admin Usage Audit & User Analytics Dashboard.
Covers:
  - AuditService event recording and aggregation on isolated SQLite storage
  - Active vs Unused account tracking and adoption rate calculation
  - CSV audit report generation
  - Admin-only RBAC on /api/accounts/audit
  - Streamlit Admin Panel Audit & Analytics UI rendering & non-admin hiding
"""

import datetime
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.database.connection import Database
from app.database.migrations import SCHEMA_SQL
from app.main import app
from app.services import audit_service as audit_mod
from app.services.audit_service import AuditService, estimate_tokens


def _setup_isolated_db(tmp_path: Path) -> Database:
    test_db = Database(db_path=tmp_path / "audit_test.db")
    test_db._executescript_sync(SCHEMA_SQL)
    return test_db


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 100) == 25


def test_audit_service_tracks_active_and_unused_users(tmp_path):
    iso_db = _setup_isolated_db(tmp_path)
    svc = AuditService()

    users = [
        {"name": "Admin", "username": "admin", "role": "Admin"},
        {"name": "Archiman", "username": "achakrabort4", "role": "User"},
        {"name": "Chandan", "username": "crakha1", "role": "User"},
    ]
    active_sessions = [
        {
            "session_id": "achak_1",
            "username": "achakrabort4",
            "name": "Archiman",
            "role": "User",
            "last_active": datetime.datetime.now().timestamp(),
        }
    ]

    with patch.object(audit_mod, "db", iso_db):
        svc.record_event("achakrabort4", "login", user_role="User", details="Portal sign in")
        svc.record_event(
            "achakrabort4",
            "chat",
            user_role="User",
            model_used="gemma4:12b",
            tokens_est=320,
            duration_ms=850,
            details="Chat completion",
        )
        svc.record_event(
            "achakrabort4",
            "report",
            user_role="User",
            model_used="gemma4:12b",
            tokens_est=680,
            duration_ms=1450,
            status="success",
        )
        svc.record_event(
            "admin",
            "code",
            user_role="Admin",
            model_used="qwen2.5-coder:14b",
            tokens_est=200,
            duration_ms=900,
            status="error",
            details="Syntax error test",
        )

        analytics = svc.get_dashboard_analytics(
            days=30,
            users=users,
            active_sessions=active_sessions,
            event_limit=50,
        )

    summary = analytics["summary"]
    assert summary["registered_users_count"] == 3
    assert summary["online_users_count"] == 1
    assert summary["lifetime_active_users"] == 2
    assert summary["active_users_in_period"] == 2
    assert summary["adoption_rate_pct"] == 66.7
    assert summary["total_logins"] == 1
    assert summary["total_actions"] == 3
    assert summary["total_tokens_est"] == 1200
    assert summary["avg_latency_ms"] == 1067
    assert summary["error_count"] == 1

    per_user = {r["raw_username"]: r for r in analytics["per_user_stats"]}
    assert per_user["achakrabort4"]["Status"] == "🟢 Online"
    assert per_user["achakrabort4"]["Usage Actions"] == 2
    assert per_user["achakrabort4"]["Top Model"] == "gemma4:12b"
    assert per_user["admin"]["Status"] == "🔵 Active User"
    assert per_user["admin"]["Errors"] == 1
    assert per_user["crakha1"]["Status"] == "⚪ Unused"
    assert per_user["crakha1"]["has_used"] is False

    csv_bytes = svc.export_audit_csv(analytics["recent_events"], analytics["per_user_stats"])
    csv_text = csv_bytes.decode("utf-8")
    assert "USER USAGE SUMMARY" in csv_text
    assert "@achakrabort4" in csv_text
    assert "DETAILED AUDIT EVENTS" in csv_text
    iso_db.close()


def test_audit_service_merges_legacy_history_without_double_counting(tmp_path):
    iso_db = _setup_isolated_db(tmp_path)
    svc = AuditService()

    users = [
        {"name": "Admin", "username": "admin", "role": "Admin"},
        {"name": "Archiman", "username": "achakrabort4", "role": "User"},
        {"name": "Shubham", "username": "sshinde5", "role": "User"},
        {"name": "Chandan", "username": "crakha1", "role": "User"},
    ]

    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    later = (datetime.datetime.now() + datetime.timedelta(seconds=5)).isoformat()

    iso_db._execute_sync(
        "INSERT INTO conversations (id, user_id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("conv_leg_1", "achakrabort4", "Legacy Chat 1", "gemma4:12b", yesterday, yesterday),
    )
    iso_db._execute_sync(
        "INSERT INTO conversations (id, user_id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("conv_leg_2", "sshinde5", "Legacy Chat 2", "llama3.2:latest", yesterday, yesterday),
    )
    # 2 legacy user messages + 1 legacy assistant reply strictly predating audit events
    iso_db._execute_sync(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("m1", "conv_leg_1", "user", "a" * 40, yesterday),
    )
    iso_db._execute_sync(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("m2", "conv_leg_1", "assistant", "b" * 80, yesterday),
    )
    iso_db._execute_sync(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("m3", "conv_leg_2", "user", "c" * 20, yesterday),
    )

    with patch.object(audit_mod, "db", iso_db):
        # Record a login and 1 new audited chat event
        svc.record_event("admin", "login", user_role="Admin", details="Admin sign in")
        svc.record_event(
            "achakrabort4",
            "chat",
            user_role="User",
            model_used="gemma4:12b",
            tokens_est=100,
            duration_ms=500,
            details="Audited chat",
        )
        # Simulate the DB persistence of that post-audit chat message: must NOT be double-counted
        iso_db._execute_sync(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ("m4", "conv_leg_1", "user", "d" * 40, later),
        )

        analytics = svc.get_dashboard_analytics(days=30, users=users, active_sessions=[], event_limit=50)

    summary = analytics["summary"]
    assert summary["registered_users_count"] == 4
    assert summary["total_logins"] == 1
    assert summary["total_actions"] == 3  # 2 legacy + 1 audited (post-audit m4 not double-counted)
    assert summary["total_tokens_est"] == 135  # 10 + 20 + 5 + 100
    assert summary["active_users_in_period"] == 3  # admin (login), achakrabort4, sshinde5
    assert summary["lifetime_active_users"] == 3

    per_user = {r["raw_username"]: r for r in analytics["per_user_stats"]}
    assert per_user["achakrabort4"]["Usage Actions"] == 2
    assert per_user["achakrabort4"]["Est. Tokens"] == 130
    assert per_user["sshinde5"]["Usage Actions"] == 1
    assert per_user["sshinde5"]["Est. Tokens"] == 5
    assert per_user["crakha1"]["Status"] == "⚪ Unused"

    model_map = {m["Model"]: m for m in analytics["model_usage"]}
    assert model_map["gemma4:12b"]["Requests"] == 2
    assert model_map["gemma4:12b"]["Est. Tokens"] == 130
    assert model_map["llama3.2:latest"]["Requests"] == 1
    assert model_map["llama3.2:latest"]["Est. Tokens"] == 5

    feat_map = {f["event_type"]: f["Count"] for f in analytics["feature_breakdown"]}
    assert feat_map["chat"] == 3
    iso_db.close()


def test_admin_audit_api_endpoint_rbac():
    client = TestClient(app)

    # Non-admin must be rejected (403)
    res_user = client.get("/api/accounts/audit?actor_role=User")
    assert res_user.status_code == 403

    # Admin succeeds with full analytics structure
    res_admin = client.get("/api/accounts/audit?actor_role=Admin&days=30")
    assert res_admin.status_code == 200
    payload = res_admin.json()
    for key in ["summary", "per_user_stats", "daily_activity", "feature_breakdown", "model_usage", "recent_events"]:
        assert key in payload
    assert payload["summary"]["registered_users_count"] >= 3


def test_streamlit_admin_audit_dashboard_renders_cleanly():
    app_path = Path(__file__).parent.parent.parent / "streamlit_app.py"

    # 1. Admin sees Audit & Analytics nav and dashboard metrics/charts
    at_admin = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_admin.session_state["authenticated_user"] = {
        "name": "Admin",
        "username": "admin",
        "role": "Admin",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "admin"),
    }
    at_admin.run()
    assert len(at_admin.exception) == 0

    admin_nav = at_admin.sidebar.radio[0].options
    assert any("Audit & Analytics" in opt for opt in admin_nav)

    audit_opt = next(opt for opt in admin_nav if "Audit & Analytics" in opt)
    at_admin.sidebar.radio[0].set_value(audit_opt).run()
    assert len(at_admin.exception) == 0

    audit_md = [m.value for m in at_admin.markdown if "Usage Audit & User Analytics Dashboard" in m.value]
    assert len(audit_md) > 0
    metric_labels = [m.label for m in at_admin.metric]
    assert "Registered Accounts" in metric_labels
    assert "Users Using App (All-Time)" in metric_labels
    assert "Tracked Prompts & Actions" in metric_labels

    # 2. Regular User must NOT see Audit & Analytics in navigation
    at_user = AppTest.from_file(str(app_path.resolve()), default_timeout=30)
    at_user.session_state["authenticated_user"] = {
        "name": "Archiman Chakraborty",
        "username": "achakrabort4",
        "role": "User",
        "workspace_path": str(Path(__file__).parent.parent.parent / "workspace" / "achakrabort4"),
    }
    at_user.run()
    assert len(at_user.exception) == 0
    user_nav = at_user.sidebar.radio[0].options
    assert not any("Audit & Analytics" in opt for opt in user_nav)
