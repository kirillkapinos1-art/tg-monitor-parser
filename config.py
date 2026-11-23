"""Configuration loading and validation for the Telegram lead monitor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


class MonitorConfigError(RuntimeError):
    """Raised when required environment settings are missing or invalid."""


def _parse_csv_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """Validated runtime configuration for Telegram channel monitoring and AI scoring."""

    api_id: int
    api_hash: str
    session_path: Path
    source_chats: tuple[str, ...]
    target_chat: str
    keywords: tuple[str, ...]
    stop_words: tuple[str, ...]
    gemini_api_key: str
    models: tuple[str, ...]
    min_ai_score: int
    db_path: Path


def load_monitor_config(
    env_path: Path | None = None,
    *,
    sources_override: list[str] | None = None,
    target_override: str | None = None,
    min_score_override: int | None = None,
) -> MonitorConfig:
    """Load configuration from .env and process environment variables."""
    target_env = env_path or (BASE_DIR / ".env")
    if target_env.exists():
        load_dotenv(dotenv_path=target_env, override=False)

    raw_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    raw_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not raw_id:
        raise MonitorConfigError("TELEGRAM_API_ID is required.")
    try:
        api_id = int(raw_id)
    except ValueError as exc:
        raise MonitorConfigError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise MonitorConfigError("TELEGRAM_API_ID must be positive.")
    if not raw_hash:
        raise MonitorConfigError("TELEGRAM_API_HASH is required.")

    raw_session = os.environ.get("TELEGRAM_SESSION_PATH", "monitor.session").strip()
    session_path = Path(raw_session)
    if not session_path.is_absolute():
        session_path = BASE_DIR / session_path

    if sources_override:
        sources = tuple(s.strip() for s in sources_override if s.strip())
    else:
        sources = _parse_csv_list(os.environ.get("SOURCE_CHATS", ""))
    if not sources:
        raise MonitorConfigError(
            "No SOURCE_CHATS configured. Set SOURCE_CHATS or pass --sources."
        )

    target_chat = (target_override or os.environ.get("TARGET_CHAT", "me")).strip() or "me"
    keywords = tuple(k.lower() for k in _parse_csv_list(os.environ.get("KEYWORDS", "")))
    stop_words = tuple(s.lower() for s in _parse_csv_list(os.environ.get("STOP_WORDS", "")))

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    preferred_model = os.environ.get("GEMINI_MODEL", "").strip()
    models = (
        tuple(dict.fromkeys([preferred_model, *DEFAULT_GEMINI_MODELS]))
        if preferred_model
        else DEFAULT_GEMINI_MODELS
    )

    raw_score = (
        str(min_score_override)
        if min_score_override is not None
        else os.environ.get("MIN_AI_SCORE", "6")
    )
    try:
        min_score = max(1, min(int(raw_score), 10))
    except ValueError as exc:
        raise MonitorConfigError("MIN_AI_SCORE must be an integer between 1 and 10.") from exc

    raw_db = os.environ.get("DB_PATH", "data/seen_posts.db").strip()
    db_path = Path(raw_db)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return MonitorConfig(
        api_id=api_id,
        api_hash=raw_hash,
        session_path=session_path,
        source_chats=sources,
        target_chat=target_chat,
        keywords=keywords,
        stop_words=stop_words,
        gemini_api_key=gemini_key,
        models=models,
        min_ai_score=min_score,
        db_path=db_path,
    )
