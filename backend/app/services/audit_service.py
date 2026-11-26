"""
Admin Usage Tracking & Audit Analytics Service.
Persists real-time user actions (logins, chats, reports, coding tasks, web searches,
vision queries, model pulls, and admin changes) in SQLite WAL storage and aggregates
usage analytics for the Admin Panel dashboard.
"""

import csv
import datetime
import io
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from app.core.logging import logger
from app.database.connection import db

ACTION_EVENT_TYPES = {"chat", "report", "code", "search", "vision", "pull", "tips"}
AUTH_EVENT_TYPES = {"login", "logout"}
ADMIN_EVENT_TYPES = {"admin_user_add", "admin_user_remove"}

FEATURE_LABELS = {
    "chat": "💬 Chat",
    "report": "📄 Executive Report",
    "code": "💻 Coding Agent",
    "search": "🌐 Web Search",
    "vision": "👁️ Vision",
    "pull": "📥 Model Pull",
    "tips": "📚 Lenovo Tips & KB",
    "login": "🔐 Sign In",
    "logout": "🚪 Sign Out",
    "admin_user_add": "➕ Admin Added User",
    "admin_user_remove": "🗑️ Admin Removed User",
}

_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS usage_audit_log (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    user_role TEXT DEFAULT 'User',
    event_type TEXT NOT NULL,
    model_used TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    tokens_est INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    status TEXT DEFAULT 'success',
    details TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_usage_audit_user ON usage_audit_log(username);
CREATE INDEX IF NOT EXISTS idx_usage_audit_created ON usage_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_audit_event ON usage_audit_log(event_type);
"""


def estimate_tokens(text: Optional[str]) -> int:
    """Fast character-based token estimate (~4 chars/token) for telemetry."""
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


class AuditService:
    """Service responsible for recording and analyzing application usage."""

    def __init__(self) -> None:
        self._verified_db_path: Optional[str] = None

    def ensure_table(self) -> None:
        """Creates usage_audit_log table and indexes if missing for active DB path."""
        cur_path = str(db.db_path)
        if self._verified_db_path == cur_path:
            return
        try:
            db._executescript_sync(_ENSURE_TABLE_SQL)
            self._verified_db_path = cur_path
        except Exception as exc:
            logger.debug(f"[AuditService] ensure_table notice: {exc}")

    def record_event(
        self,
        username: str,
        event_type: str,
        user_role: str = "User",
        model_used: str = "",
        conversation_id: str = "",
        tokens_est: int = 0,
        duration_ms: int = 0,
        status: str = "success",
        details: str = "",
        created_at: Optional[str] = None,
    ) -> str:
        """Records a single usage or audit event synchronously in SQLite WAL."""
        self.ensure_table()
        event_id = str(uuid.uuid4())
        clean_user = (username or "anonymous").strip().lower()
        clean_event = (event_type or "chat").strip().lower()
        timestamp = created_at or datetime.datetime.now().isoformat()
        params = (
            event_id,
            clean_user,
            str(user_role or "User"),
            clean_event,
            str(model_used or ""),
            str(conversation_id or ""),
            max(0, int(tokens_est or 0)),
            max(0, int(duration_ms or 0)),
            str(status or "success"),
            str(details or "")[:500],
            timestamp,
        )
        insert_sql = """
            INSERT INTO usage_audit_log (
                id, username, user_role, event_type, model_used,
                conversation_id, tokens_est, duration_ms, status, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            db._execute_sync(insert_sql, params)
        except Exception as exc:
            if "no such table" in str(exc).lower():
                self._verified_db_path = None
                self.ensure_table()
                try:
                    db._execute_sync(insert_sql, params)
                except Exception as retry_exc:
                    logger.warning(f"[AuditService] Failed to record event on retry: {retry_exc}")
            else:
                logger.warning(f"[AuditService] Failed to record event: {exc}")
        return event_id

    def _fetch_audit_rows(self, cutoff_iso: Optional[str]) -> List[Dict[str, Any]]:
        if cutoff_iso:
            return db._fetchall_sync(
                "SELECT * FROM usage_audit_log WHERE created_at >= ? ORDER BY created_at DESC",
                (cutoff_iso,),
            )
        return db._fetchall_sync("SELECT * FROM usage_audit_log ORDER BY created_at DESC")

    def _fetch_historical_chat_rows(self) -> Dict[str, Any]:
        """Fetches aggregate conversation and message history as baseline usage context."""
        conv_rows = db._fetchall_sync(
            """
            SELECT COALESCE(NULLIF(TRIM(LOWER(user_id)), ''), 'anonymous') AS username,
                   model,
                   COUNT(*) AS conv_count,
                   MAX(updated_at) AS last_updated,
                   MIN(created_at) AS first_created
            FROM conversations
            GROUP BY COALESCE(NULLIF(TRIM(LOWER(user_id)), ''), 'anonymous'), model
            """
        )
        msg_rows = db._fetchall_sync(
            """
            SELECT COALESCE(NULLIF(TRIM(LOWER(c.user_id)), ''), 'anonymous') AS username,
                   c.model AS model,
                   m.role AS role,
                   m.content AS content,
                   m.created_at AS created_at
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            ORDER BY m.created_at DESC
            """
        )
        return {"conv_rows": conv_rows, "msg_rows": msg_rows}

    def get_dashboard_analytics(
        self,
        days: Optional[int] = 30,
        users: Optional[List[Dict[str, Any]]] = None,
        active_sessions: Optional[List[Dict[str, Any]]] = None,
        event_limit: int = 100,
    ) -> Dict[str, Any]:
        """Computes KPI metrics, per-user usage breakdown, trends, and audit trail."""
        self.ensure_table()

        cutoff_iso: Optional[str] = None
        if days is not None and days > 0:
            cutoff_iso = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()

        try:
            audit_rows = self._fetch_audit_rows(cutoff_iso)
            all_time_users_rows = db._fetchall_sync(
                "SELECT DISTINCT LOWER(username) AS username FROM usage_audit_log"
            )
            earliest_audit_row = db._fetchone_sync(
                "SELECT MIN(created_at) AS first_ts FROM usage_audit_log WHERE event_type IN ('chat','report','code','search','vision','pull','tips')"
            )
            hist = self._fetch_historical_chat_rows()
        except Exception as exc:
            logger.warning(f"[AuditService] Analytics query notice: {exc}")
            audit_rows = []
            all_time_users_rows = []
            earliest_audit_row = None
            hist = {"conv_rows": [], "msg_rows": []}

        earliest_audit_ts = (earliest_audit_row or {}).get("first_ts")
        registered_users = users or []
        sessions = active_sessions or []

        # Map registered account metadata cleanly
        user_meta: Dict[str, Dict[str, Any]] = {}
        for u in registered_users:
            uname = str(u.get("username", "")).strip().lower()
            if not uname:
                continue
            user_meta[uname] = {
                "name": u.get("name", uname),
                "username": uname,
                "role": u.get("role", "User"),
                "is_registered": True,
            }

        # Active sessions by username
        online_by_user: Dict[str, float] = {}
        for sess in sessions:
            uname = str(sess.get("username", "")).strip().lower()
            if not uname:
                continue
            last_act = float(sess.get("last_active", 0) or 0)
            if uname not in online_by_user or last_act > online_by_user[uname]:
                online_by_user[uname] = last_act
            if uname not in user_meta:
                user_meta[uname] = {
                    "name": sess.get("name", uname),
                    "username": uname,
                    "role": sess.get("role", "User"),
                    "is_registered": False,
                }

        # Aggregate historical conversations & messages
        conv_counts: Dict[str, int] = defaultdict(int)
        msg_user_counts: Dict[str, int] = defaultdict(int)
        total_saved_messages = 0
        total_saved_conversations = 0
        conv_last_ts: Dict[str, str] = {}
        hist_models_by_user: Dict[str, Counter] = defaultdict(Counter)
        pre_audit_model_counts: Counter = Counter()
        pre_audit_model_tokens: Counter = Counter()
        pre_audit_models_by_user: Dict[str, Counter] = defaultdict(Counter)
        pre_audit_actions_by_user: Dict[str, int] = defaultdict(int)
        pre_audit_tokens_by_user: Dict[str, int] = defaultdict(int)
        pre_audit_users: set = set()
        pre_audit_actions_count = 0
        pre_audit_total_tokens = 0
        pre_audit_daily: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"actions": 0, "logins": 0, "users": set(), "tokens_est": 0}
        )

        for cr in hist["conv_rows"]:
            uname = cr["username"]
            cnt = int(cr["conv_count"] or 0)
            conv_counts[uname] += cnt
            total_saved_conversations += cnt
            model_name = cr.get("model") or ""
            if model_name:
                hist_models_by_user[uname][model_name] += cnt
            last_upd = cr.get("last_updated") or ""
            if last_upd and (uname not in conv_last_ts or last_upd > conv_last_ts[uname]):
                conv_last_ts[uname] = last_upd
            if uname not in user_meta and uname != "anonymous":
                user_meta[uname] = {"name": uname, "username": uname, "role": "User", "is_registered": False}

        earliest_norm = earliest_audit_ts.replace(" ", "T") if earliest_audit_ts else None
        cutoff_norm = cutoff_iso.replace(" ", "T") if cutoff_iso else None
        for mr in hist["msg_rows"]:
            total_saved_messages += 1
            uname = mr["username"]
            created = mr.get("created_at") or ""
            created_norm = created.replace(" ", "T")
            in_window = (cutoff_norm is None) or (created_norm >= cutoff_norm)
            is_legacy = (earliest_norm is None) or (created_norm < earliest_norm)
            tok_est = estimate_tokens(mr.get("content"))
            role = mr.get("role")
            if role == "user":
                msg_user_counts[uname] += 1
                # Include legacy chats predating usage_audit_log without double-counting new events
                if in_window and is_legacy:
                    day_key = created[:10] if len(created) >= 10 else "Unknown"
                    pre_audit_daily[day_key]["actions"] += 1
                    pre_audit_daily[day_key]["users"].add(uname)
                    pre_audit_daily[day_key]["tokens_est"] += tok_est
                    pre_audit_actions_count += 1
                    pre_audit_actions_by_user[uname] += 1
                    pre_audit_tokens_by_user[uname] += tok_est
                    pre_audit_total_tokens += tok_est
                    if uname != "anonymous":
                        pre_audit_users.add(uname)
                    if mr.get("model"):
                        pre_audit_model_counts[mr["model"]] += 1
                        pre_audit_model_tokens[mr["model"]] += tok_est
                        pre_audit_models_by_user[uname][mr["model"]] += 1
            elif role == "assistant" and in_window and is_legacy:
                # Include assistant reply tokens from pre-audit conversations for accurate token totals
                day_key = created[:10] if len(created) >= 10 else "Unknown"
                pre_audit_daily[day_key]["tokens_est"] += tok_est
                pre_audit_tokens_by_user[uname] += tok_est
                pre_audit_total_tokens += tok_est
                if mr.get("model"):
                    pre_audit_model_tokens[mr["model"]] += tok_est

        # Aggregate live audit events in selected window
        user_logins: Dict[str, int] = defaultdict(int)
        user_actions: Dict[str, int] = defaultdict(int)
        user_tokens: Dict[str, int] = defaultdict(int)
        user_latencies: Dict[str, List[int]] = defaultdict(list)
        user_errors: Dict[str, int] = defaultdict(int)
        user_models: Dict[str, Counter] = defaultdict(Counter)
        user_last_audit_ts: Dict[str, str] = {}

        feature_counts: Counter = Counter()
        model_stats: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"requests": 0, "tokens_est": 0, "latencies": []}
        )
        daily_map: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"actions": 0, "logins": 0, "users": set(), "tokens_est": 0}
        )
        # Seed legacy daily data
        for d_key, d_val in pre_audit_daily.items():
            daily_map[d_key]["actions"] += d_val["actions"]
            daily_map[d_key]["tokens_est"] += d_val["tokens_est"]
            daily_map[d_key]["users"].update(d_val["users"])

        total_logins = 0
        total_actions = pre_audit_actions_count
        total_tokens_est = pre_audit_total_tokens
        total_errors = 0
        all_latencies: List[int] = []
        active_users_window = set(online_by_user.keys()) | pre_audit_users
        if pre_audit_actions_count > 0:
            feature_counts["chat"] += pre_audit_actions_count
        for uname, cnt in pre_audit_actions_by_user.items():
            user_actions[uname] += cnt
        for uname, tok_val in pre_audit_tokens_by_user.items():
            user_tokens[uname] += tok_val
        for uname, m_counter in pre_audit_models_by_user.items():
            user_models[uname].update(m_counter)

        for row in audit_rows:
            uname = str(row.get("username") or "anonymous").strip().lower()
            ev_type = str(row.get("event_type") or "chat").strip().lower()
            model = str(row.get("model_used") or "").strip()
            tok = int(row.get("tokens_est") or 0)
            dur = int(row.get("duration_ms") or 0)
            stat = str(row.get("status") or "success").strip().lower()
            created = str(row.get("created_at") or "")
            day_key = created[:10] if len(created) >= 10 else "Unknown"

            if uname not in user_meta and uname != "anonymous":
                user_meta[uname] = {
                    "name": uname,
                    "username": uname,
                    "role": row.get("user_role", "User"),
                    "is_registered": False,
                }

            if uname not in user_last_audit_ts or created > user_last_audit_ts[uname]:
                user_last_audit_ts[uname] = created

            feature_counts[ev_type] += 1
            if stat == "error":
                total_errors += 1
                user_errors[uname] += 1

            if ev_type == "login":
                total_logins += 1
                user_logins[uname] += 1
                active_users_window.add(uname)
                daily_map[day_key]["logins"] += 1
                daily_map[day_key]["users"].add(uname)
            elif ev_type in ACTION_EVENT_TYPES:
                total_actions += 1
                user_actions[uname] += 1
                user_tokens[uname] += tok
                total_tokens_est += tok
                active_users_window.add(uname)

                daily_map[day_key]["actions"] += 1
                daily_map[day_key]["tokens_est"] += tok
                daily_map[day_key]["users"].add(uname)

                if dur > 0:
                    all_latencies.append(dur)
                    user_latencies[uname].append(dur)

                if model:
                    user_models[uname][model] += 1
                    model_stats[model]["requests"] += 1
                    model_stats[model]["tokens_est"] += tok
                    if dur > 0:
                        model_stats[model]["latencies"].append(dur)

        # Include legacy model counts & tokens (strictly pre-audit, so zero double-counting)
        for legacy_model, legacy_cnt in pre_audit_model_counts.items():
            model_stats[legacy_model]["requests"] += legacy_cnt
        for legacy_model, legacy_tok in pre_audit_model_tokens.items():
            model_stats[legacy_model]["tokens_est"] += legacy_tok

        # Determine lifetime active users (audit all-time OR conversations OR live online)
        lifetime_active_set = {r["username"] for r in all_time_users_rows if r.get("username")}
        lifetime_active_set.update(uname for uname, cnt in conv_counts.items() if cnt > 0 and uname != "anonymous")
        lifetime_active_set.update(online_by_user.keys())
        lifetime_active_set.discard("anonymous")

        # Build unified Per-User Audit Table
        per_user_stats: List[Dict[str, Any]] = []
        registered_who_used = 0
        for uname, meta in sorted(user_meta.items(), key=lambda kv: kv[0]):
            is_online = uname in online_by_user
            last_ts = user_last_audit_ts.get(uname, "")
            if conv_last_ts.get(uname, "") > last_ts:
                last_ts = conv_last_ts[uname]
            if is_online:
                online_iso = datetime.datetime.fromtimestamp(online_by_user[uname]).isoformat()
                if online_iso > last_ts:
                    last_ts = online_iso

            lats = user_latencies.get(uname, [])
            avg_lat = round(sum(lats) / len(lats)) if lats else 0

            fav_counter = user_models.get(uname) or hist_models_by_user.get(uname)
            fav_model = fav_counter.most_common(1)[0][0] if fav_counter else "—"

            logins_cnt = user_logins[uname]
            actions_cnt = user_actions[uname]
            convs_cnt = conv_counts[uname]
            msgs_cnt = msg_user_counts[uname]
            has_used = is_online or logins_cnt > 0 or actions_cnt > 0 or convs_cnt > 0 or msgs_cnt > 0 or (uname in lifetime_active_set)
            if meta.get("is_registered") and has_used:
                registered_who_used += 1

            per_user_stats.append({
                "Name": meta["name"],
                "Username": f"@{uname}",
                "raw_username": uname,
                "Role": meta["role"],
                "Status": "🟢 Online" if is_online else ("🔵 Active User" if has_used else "⚪ Unused"),
                "Logins": logins_cnt,
                "Usage Actions": actions_cnt,
                "Saved Chats": convs_cnt,
                "Prompts History": msgs_cnt,
                "Est. Tokens": user_tokens[uname],
                "Avg Latency (ms)": avg_lat,
                "Errors": user_errors[uname],
                "Top Model": fav_model,
                "Last Active": last_ts[:19].replace("T", " ") if last_ts else "Never",
                "has_used": has_used,
            })

        per_user_stats.sort(key=lambda r: (r["Status"] != "🟢 Online", -r["Usage Actions"], -r["Prompts History"], -r["Logins"], r["Name"]))

        reg_count = len([m for m in user_meta.values() if m.get("is_registered")])
        adoption_pct = round((registered_who_used / reg_count) * 100.0, 1) if reg_count > 0 else 0.0
        avg_latency_ms = round(sum(all_latencies) / len(all_latencies)) if all_latencies else 0
        error_rate_pct = round((total_errors / max(1, total_actions + total_logins)) * 100.0, 1) if (total_actions + total_logins) > 0 else 0.0

        daily_activity = [
            {
                "Date": day,
                "Actions": vals["actions"],
                "Logins": vals["logins"],
                "Unique Users": len(vals["users"]),
                "Est. Tokens": vals["tokens_est"],
            }
            for day, vals in sorted(daily_map.items())
            if day != "Unknown"
        ]

        feature_breakdown = [
            {"Feature": FEATURE_LABELS.get(ev, ev.title()), "event_type": ev, "Count": cnt}
            for ev, cnt in feature_counts.most_common()
        ]

        model_usage = [
            {
                "Model": m,
                "Requests": st_val["requests"],
                "Est. Tokens": st_val["tokens_est"],
                "Avg Latency (ms)": round(sum(st_val["latencies"]) / len(st_val["latencies"])) if st_val["latencies"] else 0,
            }
            for m, st_val in sorted(model_stats.items(), key=lambda kv: -kv[1]["requests"])
        ]

        return {
            "summary": {
                "registered_users_count": reg_count,
                "online_users_count": len(online_by_user),
                "active_sessions_count": len(sessions),
                "active_users_in_period": len([u for u in active_users_window if u != "anonymous"]),
                "lifetime_active_users": len(lifetime_active_set),
                "adoption_rate_pct": adoption_pct,
                "total_logins": total_logins,
                "total_actions": total_actions,
                "total_saved_conversations": total_saved_conversations,
                "total_saved_messages": total_saved_messages,
                "total_tokens_est": total_tokens_est,
                "avg_latency_ms": avg_latency_ms,
                "error_count": total_errors,
                "error_rate_pct": error_rate_pct,
            },
            "per_user_stats": per_user_stats,
            "daily_activity": daily_activity,
            "feature_breakdown": feature_breakdown,
            "model_usage": model_usage,
            "recent_events": audit_rows[:event_limit],
        }

    def export_audit_csv(self, events: List[Dict[str, Any]], per_user_stats: List[Dict[str, Any]]) -> bytes:
        """Generates a UTF-8 CSV report containing per-user summary and detailed event log."""
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow(["=== USER USAGE SUMMARY ==="])
        writer.writerow([
            "Name", "Username", "Role", "Status", "Logins",
            "Usage Actions", "Saved Chats", "Prompts History",
            "Est. Tokens", "Avg Latency (ms)", "Errors", "Top Model", "Last Active"
        ])
        for u in per_user_stats:
            writer.writerow([
                u.get("Name", ""),
                u.get("Username", ""),
                u.get("Role", ""),
                u.get("Status", ""),
                u.get("Logins", 0),
                u.get("Usage Actions", 0),
                u.get("Saved Chats", 0),
                u.get("Prompts History", 0),
                u.get("Est. Tokens", 0),
                u.get("Avg Latency (ms)", 0),
                u.get("Errors", 0),
                u.get("Top Model", ""),
                u.get("Last Active", ""),
            ])

        writer.writerow([])
        writer.writerow(["=== DETAILED AUDIT EVENTS ==="])
        writer.writerow([
            "Timestamp", "Username", "Role", "Event Type",
            "Model Used", "Est. Tokens", "Duration (ms)", "Status", "Details"
        ])
        for ev in events:
            writer.writerow([
                ev.get("created_at", ""),
                ev.get("username", ""),
                ev.get("user_role", ""),
                ev.get("event_type", ""),
                ev.get("model_used", ""),
                ev.get("tokens_est", 0),
                ev.get("duration_ms", 0),
                ev.get("status", ""),
                ev.get("details", ""),
            ])

        return buf.getvalue().encode("utf-8")


audit_service = AuditService()
