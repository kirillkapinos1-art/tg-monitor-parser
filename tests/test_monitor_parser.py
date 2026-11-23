"""Unit tests for tg-monitor-parser filters, dedup, AI scorer, and monitor workflow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors

from config import MonitorConfigError, load_monitor_config
from dedup import SeenPostsStore
from filters import build_message_link, evaluate_post_filters, text_fingerprint
from monitor import TelegramLeadMonitor
from scorer import GeminiLeadScorer


def test_filters_and_message_links() -> None:
    res_ok = evaluate_post_filters(
        "Нужен Python разработчик для Telegram бота на aiogram",
        keywords=("python", "blender"),
        stop_words=("казино", "крипта"),
    )
    assert res_ok.passed is True
    assert res_ok.matched_keywords == ("python",)

    res_stop = evaluate_post_filters(
        "Python бот для казино",
        keywords=("python",),
        stop_words=("казино",),
    )
    assert res_stop.passed is False
    assert res_stop.blocked_by == "казино"

    res_miss = evaluate_post_filters(
        "Ищем таргетолога в Instagram",
        keywords=("python", "autocad"),
    )
    assert res_miss.passed is False
    assert res_miss.blocked_by == "no_keyword_match"

    assert (
        build_message_link(chat_id=-100123456, message_id=42, username="jobs_feed")
        == "https://t.me/jobs_feed/42"
    )
    assert (
        build_message_link(chat_id=-100998877, message_id=15, username=None)
        == "https://t.me/c/998877/15"
    )


def test_seen_posts_dedup_by_id_and_hash(tmp_path: Path) -> None:
    store = SeenPostsStore(tmp_path / "seen.db")
    digest = text_fingerprint("Нужен   бот   на Python")
    assert not store.is_seen(-1001, 10, digest)

    store.mark_seen(chat_id=-1001, message_id=10, text_hash=digest, forwarded=True, score=9)
    assert store.is_seen(-1001, 10, digest)
    # Cross-posted duplicate in another channel with extra whitespace is also caught.
    assert store.is_seen(-1002, 55, text_fingerprint("Нужен бот на Python"))
    assert store.count_forwarded() == 1


def test_config_and_gemini_scorer_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("SOURCE_CHATS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    env_file = tmp_path / ".env"
    with pytest.raises(MonitorConfigError):
        load_monitor_config(env_file)

    env_file.write_text(
        "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=abc123\nSOURCE_CHATS=@feed\n",
        encoding="utf-8",
    )
    cfg = load_monitor_config(env_file, min_score_override=7)
    assert cfg.api_id == 12345
    assert cfg.source_chats == ("@feed",)
    assert cfg.min_ai_score == 7

    class StubModels:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
            self.calls.append(model)
            if model == "gemini-2.5-flash":
                raise genai_errors.ServerError(503, {"error": {"message": "busy"}}, None)
            return SimpleNamespace(
                text='{"relevant": true, "score": 9, "category": "Telegram Bot", "summary": "Нужен бот заявок", "reason": "Чёткое ТЗ и бюджет"}'
            )

    stub = SimpleNamespace(models=StubModels())
    scorer = GeminiLeadScorer(
        "fake_key", ("gemini-2.5-flash", "gemini-2.5-flash-lite"), genai_client=stub
    )
    assessment = asyncio.run(scorer.score_post("Нужен Telegram-бот для записи", ("telegram",)))
    assert assessment.relevant is True
    assert assessment.score == 9
    assert stub.models.calls == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def test_monitor_backfill_pipeline_with_stub_telethon(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=abc123\nSOURCE_CHATS=@freelance\n"
        "KEYWORDS=python,blender\nSTOP_WORDS=спам\nMIN_AI_SCORE=7\n",
        encoding="utf-8",
    )
    cfg = load_monitor_config(env_file)
    store = SeenPostsStore(tmp_path / "seen.db")

    source_entity = SimpleNamespace(id=-100777, title="Freelance Radar", username="freelance")
    target_entity = SimpleNamespace(id=1, username="owner")
    posts = [
        SimpleNamespace(id=3, message="Ищу Blender 3D моделлера для игры Roblox"),
        SimpleNamespace(id=2, message="Спам реклама казино на Python"),
        SimpleNamespace(id=1, message="Нерелевантный мем дня"),
    ]

    class StubTelethon:
        def __init__(self) -> None:
            self.connected = False
            self.sent: list[dict[str, Any]] = []

        def is_connected(self) -> bool:
            return self.connected

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def is_user_authorized(self) -> bool:
            return True

        async def get_entity(self, ref: Any) -> Any:
            return target_entity if ref == "me" else source_entity

        async def iter_messages(self, entity: Any, limit: int = 20):
            for post in posts[:limit]:
                yield post

        async def send_message(self, entity: Any, text: str, **kwargs: Any) -> None:
            self.sent.append({"entity": entity, "text": text, **kwargs})

    class StubScorer:
        async def score_post(self, text: str, matched: Any = ()):
            from scorer import LeadAssessment

            return LeadAssessment(
                relevant=True,
                score=9,
                category="Blender 3D",
                summary="Нужна 3D модель для Roblox",
                reason="Целевой заказ",
            )

    stub_tg = StubTelethon()
    monitor = TelegramLeadMonitor(
        cfg,
        store=store,
        scorer=StubScorer(),  # type: ignore[arg-type]
        client_factory=lambda _: stub_tg,
    )
    matched = asyncio.run(monitor.run(backfill_limit=10, once=True))
    assert matched == 1
    assert len(stub_tg.sent) == 1
    assert "Freelance Radar" in stub_tg.sent[0]["text"]
    assert "https://t.me/freelance/3" in stub_tg.sent[0]["text"]
