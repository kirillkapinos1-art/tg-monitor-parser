"""Keyword/stopword pre-filtering, text hashing, and Telegram message link builders."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Outcome of deterministic keyword and stop-word filtering."""

    passed: bool
    matched_keywords: tuple[str, ...]
    blocked_by: str | None = None


def normalize_text(text: str) -> str:
    """Collapse repeated whitespace for consistent hashing and matching."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def text_fingerprint(text: str) -> str:
    """Return a stable SHA-256 hex digest of normalized post content."""
    normalized = normalize_text(text).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evaluate_post_filters(
    text: str,
    *,
    keywords: Sequence[str] = (),
    stop_words: Sequence[str] = (),
) -> FilterResult:
    """Apply stop-word rejection and keyword inclusion checks before AI scoring."""
    normalized = normalize_text(text).lower()
    if not normalized:
        return FilterResult(passed=False, matched_keywords=(), blocked_by="empty_text")

    for bad in stop_words:
        token = bad.strip().lower()
        if token and token in normalized:
            return FilterResult(passed=False, matched_keywords=(), blocked_by=token)

    clean_keywords = [kw.strip().lower() for kw in keywords if kw.strip()]
    if not clean_keywords:
        return FilterResult(passed=True, matched_keywords=())

    matched = tuple(kw for kw in clean_keywords if kw in normalized)
    if not matched:
        return FilterResult(passed=False, matched_keywords=(), blocked_by="no_keyword_match")
    return FilterResult(passed=True, matched_keywords=matched)


def build_message_link(
    *,
    chat_id: int,
    message_id: int,
    username: str | None = None,
) -> str | None:
    """Construct a clickable t.me link for public channels or private supergroups."""
    if message_id <= 0:
        return None
    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"
    raw_id = str(chat_id)
    if raw_id.startswith("-100") and len(raw_id) > 4:
        return f"https://t.me/c/{raw_id[4:]}/{message_id}"
    return None
