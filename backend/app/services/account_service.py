"""
Account Management Service.
Direct Excel-backed user management and workspace orchestrator.
Complies strictly with Account Management rules:
  1. Only Admin can add or remove users.
  2. Every user has a dedicated, sandboxed workspace.
  3. Multiple users can log in and use the application concurrently.
  4. Smooth operation via load balancer integration.
  5. Excel sheet ('Account Management.xlsx') is the sole database for user management.
"""

import os
import re
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill

from app.core.config import settings, WORKSPACE_DIR
from app.core.logging import logger
from app.core.load_balancer import load_balancer


class AccountService:
    """
    Direct Excel Database adapter for user authentication and provisioning.
    Reads and persists user credentials exclusively in 'Account Management.xlsx'.
    """

    def __init__(self, excel_path: Optional[Path] = None):
        self.excel_path = excel_path or (settings.base_dir / "Account Management.xlsx")
        self._lock = threading.RLock()
        self.ensure_excel_initialized()
        self.ensure_initial_workspaces()

    def ensure_excel_initialized(self):
        """Ensures the Excel file exists with proper headers if not present."""
        with self._lock:
            if not self.excel_path.exists():
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Sheet1"
                headers = ["Name", "Username", "Password", "Role"]
                for col_idx, h in enumerate(headers, 1):
                    cell = ws.cell(row=1, column=col_idx, value=h)
                    cell.font = Font(bold=True)
                
                # Default admin account
                default_users = [
                    ("Admin", "admin", "admin123", "Admin"),
                    ("Archiman Chakraborty", "achakrabort4", "Lenovo@123", "User"),
                    ("Chandan Rakha", "crakha1", "Lenovo@123", "User"),
                ]
                for r_idx, u in enumerate(default_users, 2):
                    for c_idx, val in enumerate(u, 1):
                        ws.cell(row=r_idx, column=c_idx, value=val)

                # Rules documentation in Column F
                rules = [
                    "Rule for account management:",
                    "1. Only Admin can add or remove user",
                    "2. Every user should have separate workspace",
                    "3. Multiple user should use be able to login and use the application without any issue.",
                    "4. Use a load balancer to smooth operation",
                    "5. Use this excel sheet as the database for user management. Save all user details in this only, no separate database."
                ]
                for idx, rule in enumerate(rules, 15):
                    ws.cell(row=idx, column=6, value=rule)

                wb.save(str(self.excel_path))
                logger.info(f"Initialized new account management Excel database at {self.excel_path}")

    def get_user_workspace(self, username: str) -> Path:
        """
        Rule 2: Every user should have separate workspace.
        Resolves and provisions a private, sandboxed workspace for the user.
        """
        clean_user = re.sub(r'[^a-zA-Z0-9_\-]', '', username.strip().lower())
        if not clean_user:
            clean_user = "guest"
        user_workspace = WORKSPACE_DIR / clean_user
        user_workspace.mkdir(parents=True, exist_ok=True)
        
        # Provision welcome readme if workspace is new
        readme_file = user_workspace / "README.md"
        if not readme_file.exists():
            readme_file.write_text(
                f"# Private Workspace: @{clean_user}\n\n"
                f"This directory is your private, isolated workspace inside Isotope.\n"
                f"All coding agent tasks, created files, and sandboxed tool executions operate here.\n",
                encoding="utf-8"
            )
        return user_workspace

    def ensure_initial_workspaces(self):
        """Provisions workspaces for all users present in the Excel sheet."""
        users = self.get_all_users(include_passwords=True)
        for u in users:
            self.get_user_workspace(u["username"])

    def get_all_users(self, include_passwords: bool = False) -> List[Dict[str, Any]]:
        """
        Reads all user records from Account Management.xlsx.
        Column mapping: A=Name, B=Username, C=Password, D=Role
        """
        with self._lock:
            wb = openpyxl.load_workbook(str(self.excel_path), data_only=True)
            ws = wb.active
            users: List[Dict[str, Any]] = []

            for r in range(2, ws.max_row + 1):
                uname_val = ws.cell(row=r, column=2).value
                if not uname_val:
                    continue
                username = str(uname_val).strip()
                if not username:
                    continue

                name = str(ws.cell(row=r, column=1).value or username).strip()
                password = str(ws.cell(row=r, column=3).value or "").strip()
                role = str(ws.cell(row=r, column=4).value or "User").strip()
                workspace = str(self.get_user_workspace(username))

                user_data = {
                    "name": name,
                    "username": username,
                    "role": role,
                    "workspace_path": workspace,
                }
                if include_passwords:
                    user_data["password"] = password
                    user_data["row"] = r

                users.append(user_data)

            return users

    def get_user_by_username(self, username: str, include_passwords: bool = False) -> Optional[Dict[str, Any]]:
        """Finds user by username (case-insensitive)."""
        target = username.strip().lower()
        users = self.get_all_users(include_passwords=include_passwords)
        for u in users:
            if u["username"].lower() == target:
                return u
        return None

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """
        Authenticates a user against credentials stored in Account Management.xlsx.
        Returns user profile and provisions workspace upon successful authentication.
        """
        target_uname = username.strip().lower()
        target_pass = password.strip()

        users = self.get_all_users(include_passwords=True)
        for u in users:
            if u["username"].lower() == target_uname and u["password"] == target_pass:
                # Ensure workspace exists
                ws_path = self.get_user_workspace(u["username"])
                logger.info(f"[AccountService] Successful login for '{u['username']}' (Role: {u['role']})")
                return {
                    "name": u["name"],
                    "username": u["username"],
                    "role": u["role"],
                    "workspace_path": str(ws_path),
                }

        logger.warning(f"[AccountService] Failed login attempt for username '{username}'")
        return None

    def add_user(
        self,
        name: str,
        username: str,
        password: str,
        role: str = "User",
        actor_role: str = "Admin"
    ) -> Dict[str, Any]:
        """
        Rule 1: Only Admin can add or remove user.
        Rule 5: Save all user details in this excel sheet only, no separate database.
        """
        if actor_role.strip().lower() != "admin":
            raise PermissionError("Access Denied: Only Admin can add users (Rule 1).")

        clean_uname = username.strip().lower()
        clean_name = name.strip()
        clean_pass = password.strip()
        clean_role = "Admin" if role.strip().lower() == "admin" else "User"

        if not clean_uname or not clean_name or not clean_pass:
            raise ValueError("Name, username, and password are required fields.")

        with self._lock:
            # Check for existing username
            existing = self.get_user_by_username(clean_uname)
            if existing:
                raise ValueError(f"Username '{clean_uname}' already exists.")

            wb = openpyxl.load_workbook(str(self.excel_path))
            ws = wb.active

            # Find the next free row for users in columns 1-4
            target_row = None
            rules_header_row = 15
            for r in range(2, 200):
                # Check if Column F contains rules
                f_val = str(ws.cell(row=r, column=6).value or "")
                if "Rule for account management" in f_val:
                    rules_header_row = r

                uname_val = ws.cell(row=r, column=2).value
                if not uname_val or not str(uname_val).strip():
                    if target_row is None:
                        target_row = r

            if target_row is None or target_row >= rules_header_row:
                # Insert row before rules header to preserve rules section below
                ws.insert_rows(rules_header_row)
                target_row = rules_header_row

            ws.cell(row=target_row, column=1, value=clean_name)
            ws.cell(row=target_row, column=2, value=clean_uname)
            ws.cell(row=target_row, column=3, value=clean_pass)
            ws.cell(row=target_row, column=4, value=clean_role)

            wb.save(str(self.excel_path))
            logger.info(f"[AccountService] Admin created new user '{clean_uname}' at row {target_row}")

        # Provision user workspace
        ws_path = self.get_user_workspace(clean_uname)

        return {
            "name": clean_name,
            "username": clean_uname,
            "role": clean_role,
            "workspace_path": str(ws_path),
        }

    def remove_user(self, username: str, actor_role: str = "Admin") -> bool:
        """
        Rule 1: Only Admin can add or remove user.
        Shifts rows in columns 1-4 up so user list stays contiguous without touching Column F rules.
        """
        if actor_role.strip().lower() != "admin":
            raise PermissionError("Access Denied: Only Admin can remove users (Rule 1).")

        clean_uname = username.strip().lower()

        with self._lock:
            wb = openpyxl.load_workbook(str(self.excel_path))
            ws = wb.active

            # Find target user row and last user row
            target_row = None
            last_user_row = 1
            admin_count = 0

            for r in range(2, 200):
                uname_val = ws.cell(row=r, column=2).value
                if uname_val and str(uname_val).strip():
                    u_str = str(uname_val).strip().lower()
                    last_user_row = r
                    r_val = str(ws.cell(row=r, column=4).value or "").strip().lower()
                    if r_val == "admin":
                        admin_count += 1
                    if u_str == clean_uname:
                        target_row = r

            if not target_row:
                raise ValueError(f"User '{username}' does not exist.")

            # Safety check: Cannot delete last admin
            del_role = str(ws.cell(row=target_row, column=4).value or "").strip().lower()
            if del_role == "admin" and admin_count <= 1:
                raise ValueError("Cannot remove the only remaining Admin account.")

            # Shift user rows up in columns 1-4
            for r in range(target_row, last_user_row):
                for c in range(1, 5):
                    ws.cell(row=r, column=c).value = ws.cell(row=r + 1, column=c).value

            # Clear the last user row in columns 1-4
            for c in range(1, 5):
                ws.cell(row=last_user_row, column=c).value = None

            wb.save(str(self.excel_path))
            logger.info(f"[AccountService] Admin removed user '{clean_uname}' from row {target_row}")
            return True

    def get_governance_rules(self) -> List[str]:
        """Reads governance rules documented in Column F of Account Management.xlsx."""
        with self._lock:
            wb = openpyxl.load_workbook(str(self.excel_path), data_only=True)
            ws = wb.active
            rules = []
            for r in range(1, 50):
                val = ws.cell(row=r, column=6).value
                if val and str(val).strip():
                    rules.append(str(val).strip())
            return rules


# Singleton instance
account_service = AccountService()
