"""
Gemini API key pool manager with round-robin rotation and cooldown handling.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional


def mask_api_key(key: str) -> str:
    """Mask an API key for logs/status output (never expose the full key)."""
    if not key:
        return "***"
    key = key.strip()
    if len(key) <= 8:
        return f"{key[:2]}...{key[-2:]}"
    return f"{key[:4]}...{key[-4:]}"


class GeminiKeyManager:
    """
    Thread-safe manager for Gemini API keys.

    Features:
    - Round-robin key selection
    - Per-key cooldown after failures
    - Automatic cooldown expiration
    - Safe status reporting without exposing real keys
    """

    def __init__(
        self,
        api_keys: list[str],
        cooldown_seconds: int = 30,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        self.api_keys: list[str] = list(api_keys)
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._time_fn = time_fn or time.time
        self._lock = threading.Lock()
        self._next_index = 0
        self._cooldown_until: dict[str, float] = {}

    def _now(self) -> float:
        return float(self._time_fn())

    def _refresh_expired_cooldowns(self, now: float) -> None:
        expired = [k for k, until in self._cooldown_until.items() if until <= now]
        for key in expired:
            self._cooldown_until.pop(key, None)

    def get_next_key(self) -> str | None:
        """Return the next currently available key using round-robin rotation."""
        with self._lock:
            if not self.api_keys:
                return None

            now = self._now()
            self._refresh_expired_cooldowns(now)

            total = len(self.api_keys)
            for offset in range(total):
                idx = (self._next_index + offset) % total
                key = self.api_keys[idx]
                if key not in self._cooldown_until:
                    self._next_index = (idx + 1) % total
                    return key

            return None

    def _error_text(self, error: Exception) -> str:
        return f"{type(error).__name__} {str(error)}"

    def _extract_retry_delay_seconds(self, error: Exception) -> int | None:
        """Extract retry delay from Gemini errors when available."""
        text = self._error_text(error)

        # Pattern like: "Please retry in 24.75718562s."
        m = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s", text, re.IGNORECASE)
        if m:
            try:
                return max(1, int(float(m.group(1))))
            except Exception:
                pass

        # Pattern like: "'retryDelay': '43s'"
        m = re.search(r"retryDelay['\"]?\s*:\s*['\"]([0-9]+)s['\"]", text, re.IGNORECASE)
        if m:
            try:
                return max(1, int(m.group(1)))
            except Exception:
                pass

        return None

    def extract_error_context(self, error: Exception) -> dict[str, str | int | None]:
        """Extract helpful quota/rate-limit context for logging."""
        text = self._error_text(error)

        quota_id = None
        quota_metric = None

        m_id = re.search(r"quotaId['\"]?\s*:\s*['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
        if m_id:
            quota_id = m_id.group(1)

        m_metric = re.search(r"quotaMetric['\"]?\s*:\s*['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
        if m_metric:
            quota_metric = m_metric.group(1)

        return {
            "retry_delay_seconds": self._extract_retry_delay_seconds(error),
            "quota_id": quota_id,
            "quota_metric": quota_metric,
        }

    def mark_key_failed(self, key: str, error: Exception) -> int:
        """
        Place a key in cooldown after a failure.

        Returns the cooldown duration (seconds) that was applied.
        """
        retry_delay = self._extract_retry_delay_seconds(error)
        applied_cooldown = max(self.cooldown_seconds, retry_delay or 0)

        with self._lock:
            if key in self.api_keys:
                self._cooldown_until[key] = self._now() + applied_cooldown

        return applied_cooldown

    def mark_key_success(self, key: str) -> None:
        """Mark a key as healthy/available."""
        with self._lock:
            self._cooldown_until.pop(key, None)

    def key_label(self, key: str) -> str:
        """Human-friendly masked key label for logs."""
        try:
            idx = self.api_keys.index(key) + 1
        except ValueError:
            idx = -1
        if idx > 0:
            return f"#{idx} ({mask_api_key(key)})"
        return mask_api_key(key)

    def get_status(self) -> dict:
        """Return current manager status without exposing real keys."""
        with self._lock:
            now = self._now()
            self._refresh_expired_cooldowns(now)

            keys_status = []
            for idx, key in enumerate(self.api_keys, start=1):
                cooldown_until = self._cooldown_until.get(key)
                if cooldown_until is not None and cooldown_until > now:
                    status = "cooldown"
                    cooldown_remaining = int(max(0, cooldown_until - now))
                else:
                    status = "available"
                    cooldown_remaining = 0

                keys_status.append(
                    {
                        "index": idx,
                        "masked": mask_api_key(key),
                        "status": status,
                        "cooldown_remaining_seconds": cooldown_remaining,
                    }
                )

            cooldown_keys = sum(1 for item in keys_status if item["status"] == "cooldown")
            available_keys = len(self.api_keys) - cooldown_keys

            return {
                "total_keys": len(self.api_keys),
                "available_keys": available_keys,
                "cooldown_keys": cooldown_keys,
                "keys": keys_status,
            }
