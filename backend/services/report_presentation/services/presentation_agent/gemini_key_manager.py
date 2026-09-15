from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv


# This module lives at:
# backend/services/report_presentation/services/presentation_agent/gemini_key_manager.py
# parents[4] is the InsightFlow backend/ directory where users create .env.
_BACKEND_DIR = Path(__file__).resolve().parents[4]
_BACKEND_ENV_FILE = _BACKEND_DIR / '.env'
_PLACEHOLDERS = {'', 'your_api_key_here', 'your_google_ai_studio_api_key_here', 'changeme'}


def load_backend_dotenv(path: Path | None = None) -> None:
    """Load backend/.env into os.environ without overriding real environment variables.

    Using an absolute path makes configuration independent of the directory from
    which Uvicorn is started. The Google ADK/GenAI SDK resolves API keys from
    environment variables, so loading the file here also makes the values visible
    to the SDK when the agent runs.
    """
    env_path = path or _BACKEND_ENV_FILE
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


# Load once when the Presentation Agent configuration module is imported.
# A Uvicorn restart/reload re-imports the module and picks up .env changes.
load_backend_dotenv()


def load_api_keys() -> list[str]:
    raw = os.environ.get('GEMINI_API_KEYS', '')
    values = raw.split(',') if raw.strip() else [os.environ.get('GEMINI_API_KEY', '')]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if key.lower() in _PLACEHOLDERS or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def model_name() -> str:
    return os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'


class GeminiKeyManager:
    def __init__(self, keys: list[str] | None = None, cooldown_seconds: int | None = None):
        self.keys = list(keys if keys is not None else load_api_keys())
        self.cooldown_seconds = cooldown_seconds or int(os.environ.get('GEMINI_KEY_COOLDOWN_SECONDS', '30') or 30)
        self._cursor = 0
        self._cooldown_until: dict[int, float] = {}
        self._lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.keys)

    def next_key(self) -> tuple[int, str] | None:
        if not self.keys:
            return None
        now = time.monotonic()
        with self._lock:
            for offset in range(len(self.keys)):
                idx = (self._cursor + offset) % len(self.keys)
                if self._cooldown_until.get(idx, 0) <= now:
                    self._cursor = (idx + 1) % len(self.keys)
                    return idx, self.keys[idx]
        return None

    def cooldown(self, index: int) -> None:
        with self._lock:
            self._cooldown_until[index] = time.monotonic() + max(1, self.cooldown_seconds)

    def safe_label(self, index: int) -> str:
        return f'gemini-key-{index + 1}'
