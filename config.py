"""SENTINEL runtime configuration.

All settings come from environment variables (loaded from .env if present).
The app is fully functional with no Grok key — a deterministic local-reasoning
fallback kicks in, and the UI marks reasoning as "local" vs "Grok".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    xai_api_key: str = field(default_factory=lambda: _env("XAI_API_KEY", ""))
    xai_base_url: str = field(default_factory=lambda: _env("XAI_BASE_URL", "https://api.x.ai/v1"))
    xai_model: str = field(default_factory=lambda: _env("XAI_MODEL", "grok-4-fast"))
    xai_model_large: str = field(default_factory=lambda: _env("XAI_MODEL_LARGE", "grok-4"))

    demo_speed: float = field(default_factory=lambda: _env_float("DEMO_SPEED", 1.0))
    demo_host: str = field(default_factory=lambda: _env("DEMO_HOST", "0.0.0.0"))
    demo_port: int = field(default_factory=lambda: int(_env_float("DEMO_PORT", 8000)))
    demo_autoconnect: bool = field(default_factory=lambda: _env_bool("DEMO_AUTOCONNECT", True))

    # Tick cadence: emit vitals every REAL_SECONDS, advancing simulated time.
    tick_seconds: float = 1.0

    @property
    def grok_enabled(self) -> bool:
        return bool(self.xai_api_key)


settings = Settings()