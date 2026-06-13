"""Central configuration: merges config.yaml (non-secret) with .env (secrets)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data_store"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = ROOT / "logs"

for _d in (DATA_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


class Settings:
    """Lightweight settings object. YAML for structure, env for secrets."""

    def __init__(self) -> None:
        cfg_path = ROOT / "config.yaml"
        with open(cfg_path, "r", encoding="utf-8") as fh:
            self.yaml: dict[str, Any] = yaml.safe_load(fh)

        # --- secrets / env ---
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.llm_primary = os.getenv("LLM_PRIMARY", "gemini").strip().lower()
        self.fred_api_key = os.getenv("FRED_API_KEY", "").strip()

        self.reddit_client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
        self.reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
        self.reddit_user_agent = os.getenv("REDDIT_USER_AGENT", "ctg-india-alpha/0.1").strip()

        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "587") or 587)
        self.smtp_user = os.getenv("SMTP_USER", "").strip()
        self.smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        self.alert_email_to = os.getenv("ALERT_EMAIL_TO", "").strip()

        self.web_host = os.getenv("WEB_HOST", "127.0.0.1").strip()
        self.web_port = int(os.getenv("WEB_PORT", "8799") or 8799)
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    # convenient typed accessors -------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.yaml
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def timezone(self) -> str:
        return self.yaml.get("timezone", "Asia/Kolkata")

    # capability flags -----------------------------------------------
    @property
    def has_llm(self) -> bool:
        return bool(self.gemini_api_key or self.groq_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_email(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.alert_email_to)

    @property
    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def has_fred(self) -> bool:
        return bool(self.fred_api_key)

    def capability_report(self) -> dict[str, bool]:
        return {
            "llm": self.has_llm,
            "gemini": bool(self.gemini_api_key),
            "groq": bool(self.groq_api_key),
            "fred": self.has_fred,
            "reddit": self.has_reddit,
            "telegram": self.has_telegram,
            "email": self.has_email,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
