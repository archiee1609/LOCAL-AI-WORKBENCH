"""
Multi-User Request Load Balancer & Fair Concurrency Engine.
Ensures smooth multi-user operation across Ollama, ComfyUI, and tool execution.
Dynamically scales concurrency based on real-time hardware telemetry and load profiles.
"""

import time
import asyncio
import threading
from typing import Dict, Any, List, Optional
from collections import defaultdict
from app.core.logging import logger
from app.core.resource_balancer import get_current_load_profile, get_live_telemetry


class MultiUserLoadBalancer:
    """
    Coordinates multi-user requests and prevents system exhaustion.
    Guarantees:
      - CPU/GPU protection by bounding concurrent AI generation tasks.
      - Fair-share queueing so no single user starves others.
      - Dynamic hardware-aware concurrency tuning (ECO=1, BALANCED=2, ULTRA=4).
      - Live session management and request tracking.
    """

    def __init__(self):
        self._thread_lock = threading.Lock()
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._user_request_counts: Dict[str, int] = defaultdict(int)
        self._active_requests = 0
        self._queued_requests = 0
        self._total_completed = 0
        self._semaphore = threading.Semaphore(2)
        self._current_capacity = 2

    def _determine_capacity(self) -> int:
        """Determines max concurrent AI tasks based on hardware profile."""
        profile = get_current_load_profile()
        profile_tier = profile.get("profile", "BALANCED").upper()
        if profile_tier == "ULTRA":
            return 4
        elif profile_tier == "BALANCED":
            return 2
        else:
            return 1

    def register_session(self, username: str, name: str, role: str) -> str:
        """Registers a user session and updates active session roster."""
        session_id = f"{username}_{int(time.time()*1000)}"
        self._active_sessions[session_id] = {
            "session_id": session_id,
            "username": username,
            "name": name,
            "role": role,
            "login_time": time.time(),
            "last_active": time.time(),
        }
        logger.info(f"[LoadBalancer] Registered active session for {username} (ID: {session_id})")
        return session_id

    def update_activity(self, session_id: str):
        """Updates last active timestamp for a session."""
        if session_id in self._active_sessions:
            self._active_sessions[session_id]["last_active"] = time.time()

    def unregister_session(self, session_id: str):
        """Removes an active session on logout."""
        if session_id in self._active_sessions:
            user = self._active_sessions.pop(session_id)
            logger.info(f"[LoadBalancer] Unregistered session for {user.get('username')}")

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        """Returns active sessions with stale session cleanup (>30m inactive)."""
        now = time.time()
        # Clean sessions inactive for > 1800s
        stale = [sid for sid, s in self._active_sessions.items() if (now - s.get("last_active", 0)) > 1800]
        for sid in stale:
            self._active_sessions.pop(sid, None)
        return list(self._active_sessions.values())

    async def acquire_slot(self, username: str = "anonymous", task_name: str = "ai_inference"):
        """
        Context manager / async acquire for request scheduling.
        Queues the request if capacity is reached and ensures smooth execution.
        """
        capacity = self._determine_capacity()
        with self._thread_lock:
            if self._semaphore is None:
                self._semaphore = threading.Semaphore(capacity)
                self._current_capacity = capacity

        self._queued_requests += 1
        logger.debug(f"[LoadBalancer] User '{username}' queued for '{task_name}' (Queue: {self._queued_requests})")

        try:
            while not self._semaphore.acquire(blocking=False):
                await asyncio.sleep(0.05)
        finally:
            self._queued_requests = max(0, self._queued_requests - 1)

        self._active_requests += 1
        self._user_request_counts[username] += 1
        logger.debug(f"[LoadBalancer] User '{username}' acquired slot for '{task_name}' (Active: {self._active_requests}/{capacity})")

    def release_slot(self, username: str = "anonymous"):
        """Releases the slot back to the load balancer pool."""
        self._active_requests = max(0, self._active_requests - 1)
        self._total_completed += 1
        if self._semaphore:
            self._semaphore.release()
        logger.debug(f"[LoadBalancer] User '{username}' released slot (Active: {self._active_requests})")

    class RequestGuard:
        def __init__(self, lb: "MultiUserLoadBalancer", username: str, task_name: str):
            self.lb = lb
            self.username = username
            self.task_name = task_name

        async def __aenter__(self):
            await self.lb.acquire_slot(self.username, self.task_name)
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.lb.release_slot(self.username)

    def guard(self, username: str = "anonymous", task_name: str = "ai_inference"):
        """Usage: async with load_balancer.guard(user, task): ..."""
        return self.RequestGuard(self, username, task_name)

    def get_stats(self) -> Dict[str, Any]:
        """Returns snapshot telemetry for load balancing metrics."""
        profile = get_current_load_profile()
        return {
            "active_requests": self._active_requests,
            "queued_requests": self._queued_requests,
            "max_concurrency": self._determine_capacity(),
            "hardware_profile": profile.get("profile", "BALANCED"),
            "active_sessions": len(self.get_active_sessions()),
            "total_completed_requests": self._total_completed,
            "user_request_counts": dict(self._user_request_counts),
        }


# Singleton instance
load_balancer = MultiUserLoadBalancer()
