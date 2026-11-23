"""Telethon channel monitor and lead parser with keyword filtering, AI scoring, and deduplication."""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import sys
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient, events, utils

from config import MonitorConfig, MonitorConfigError, load_monitor_config
from dedup import SeenPostsStore
from filters import build_message_link, evaluate_post_filters, normalize_text, text_fingerprint
from scorer import GeminiLeadScorer, LeadAssessment

logger = logging.getLogger("tg_monitor")


def _parse_chat_target(ref: str) -> int | str:
    cleaned = ref.strip()
    if cleaned.lower() == "me":
        return "me"
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


def _entity_title(entity: Any) -> str:
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(getattr(entity, "id", "Channel"))
    )


def _entity_peer_id(entity: Any) -> int:
    try:
        return int(utils.get_peer_id(entity))
    except Exception:
        return int(getattr(entity, "id", 0) or 0)


def format_lead_alert(
    *,
    source_title: str,
    text: str,
    matched_keywords: tuple[str, ...],
    assessment: LeadAssessment | None,
    link: str | None,
) -> str:
    """Format a matched Telegram post as a concise HTML notification."""
    snippet = normalize_text(text)
    if len(snippet) > 900:
        snippet = snippet[:900].rstrip() + "..."

    lines = [f"🎯 <b>Новый потенциальный лид</b> · <i>{html.escape(source_title)}</i>"]
    if assessment is not None:
        lines.append(
            f"📊 <b>Оценка:</b> {assessment.score}/10 · "
            f"<b>Категория:</b> {html.escape(assessment.category)}"
        )
        if assessment.summary:
            lines.append(f"📝 <b>Суть:</b> {html.escape(assessment.summary)}")
    if matched_keywords:
        lines.append(f"🏷 <b>Ключевые слова:</b> {html.escape(', '.join(matched_keywords))}")
    lines.append(f"\n<blockquote>{html.escape(snippet)}</blockquote>")
    if link:
        lines.append(f"🔗 <a href=\"{html.escape(link, quote=True)}\">Открыть сообщение</a>")
    return "\n".join(lines)


class TelegramLeadMonitor:
    """Coordinates Telethon ingestion, pre-filtering, Gemini scoring, and forwarding."""

    def __init__(
        self,
        config: MonitorConfig,
        *,
        store: SeenPostsStore | None = None,
        scorer: GeminiLeadScorer | None = None,
        client_factory: Callable[[MonitorConfig], Any] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.store = store or SeenPostsStore(config.db_path)
        if scorer is not None:
            self.scorer: GeminiLeadScorer | None = scorer
        elif config.gemini_api_key:
            self.scorer = GeminiLeadScorer(config.gemini_api_key, config.models)
        else:
            self.scorer = None
        self._client_factory = client_factory
        self.dry_run = dry_run
        self._client: Any | None = None
        self._sources: dict[int, Any] = {}
        self._target_entity: Any | None = None

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.config)
        return TelegramClient(
            str(self.config.session_path),
            self.config.api_id,
            self.config.api_hash,
        )

    async def setup_entities(self) -> None:
        """Resolve source channels and the forwarding destination entity."""
        assert self._client is not None
        self._sources.clear()
        for src in self.config.source_chats:
            entity = await self._client.get_entity(_parse_chat_target(src))
            peer_id = _entity_peer_id(entity)
            self._sources[peer_id] = entity
            logger.info("Watching source: %s (id=%s)", _entity_title(entity), peer_id)
        self._target_entity = await self._client.get_entity(
            _parse_chat_target(self.config.target_chat)
        )

    async def process_message(self, msg: Any, source_entity: Any) -> bool:
        """Evaluate a single Telegram message and forward it if qualified."""
        text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
        msg_id = int(getattr(msg, "id", 0) or 0)
        chat_id = _entity_peer_id(source_entity)
        if not text.strip() or msg_id <= 0:
            return False

        digest = text_fingerprint(text)
        if self.store.is_seen(chat_id, msg_id, digest):
            return False

        filter_res = evaluate_post_filters(
            text,
            keywords=self.config.keywords,
            stop_words=self.config.stop_words,
        )
        if not filter_res.passed:
            self.store.mark_seen(
                chat_id=chat_id, message_id=msg_id, text_hash=digest, forwarded=False, score=0
            )
            return False

        assessment: LeadAssessment | None = None
        score_value = 10
        if self.scorer is not None:
            assessment = await self.scorer.score_post(text, filter_res.matched_keywords)
            score_value = assessment.score
            if not assessment.relevant or assessment.score < self.config.min_ai_score:
                self.store.mark_seen(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text_hash=digest,
                    forwarded=False,
                    score=score_value,
                )
                return False

        link = build_message_link(
            chat_id=chat_id,
            message_id=msg_id,
            username=getattr(source_entity, "username", None),
        )
        alert = format_lead_alert(
            source_title=_entity_title(source_entity),
            text=text,
            matched_keywords=filter_res.matched_keywords,
            assessment=assessment,
            link=link,
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Matched lead from %s:\n%s", _entity_title(source_entity), alert)
        else:
            assert self._client is not None and self._target_entity is not None
            await self._client.send_message(
                self._target_entity, alert, parse_mode="html", link_preview=False
            )

        self.store.mark_seen(
            chat_id=chat_id,
            message_id=msg_id,
            text_hash=digest,
            forwarded=True,
            score=score_value,
        )
        return True

    async def backfill(self, limit: int = 20) -> int:
        """Scan the latest `limit` messages from each configured source channel."""
        assert self._client is not None
        forwarded_count = 0
        for entity in self._sources.values():
            messages = [msg async for msg in self._client.iter_messages(entity, limit=limit)]
            for msg in reversed(messages):
                if await self.process_message(msg, entity):
                    forwarded_count += 1
        return forwarded_count

    async def run(self, *, backfill_limit: int = 0, once: bool = False) -> int:
        """Connect to Telegram, run optional backfill, and listen for new posts."""
        self._client = self._build_client()
        await self._client.connect()
        try:
            if not await self._client.is_user_authorized():
                raise MonitorConfigError(
                    f"Session '{self.config.session_path}' is not authorized. Sign in via Telethon first."
                )
            await self.setup_entities()

            forwarded = 0
            if backfill_limit > 0:
                forwarded = await self.backfill(limit=backfill_limit)
                logger.info("Backfill complete: %s lead(s) matched.", forwarded)

            if once:
                return forwarded

            @self._client.on(events.NewMessage(chats=list(self._sources.keys())))
            async def _on_new_post(event: Any) -> None:
                chat_id = getattr(event, "chat_id", None)
                source = self._sources.get(int(chat_id)) if chat_id is not None else None
                if source is None:
                    return
                await self.process_message(event.message, source)

            logger.info("Live monitor running across %s source(s).", len(self._sources))
            await self._client.run_until_disconnected()
            return forwarded
        finally:
            if self._client.is_connected():
                await self._client.disconnect()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for tg-monitor-parser."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Monitor Telegram channels/chats for freelance leads using keywords + Gemini AI."
    )
    parser.add_argument("--sources", nargs="+", default=None, help="Source @usernames or chat IDs")
    parser.add_argument("--target", default=None, help="Destination chat ('me', @username, or ID)")
    parser.add_argument("--min-score", type=int, default=None, help="Minimum AI score (1-10)")
    parser.add_argument("--backfill", type=int, default=0, help="Scan last N messages on startup")
    parser.add_argument("--once", action="store_true", help="Exit after backfill without live loop")
    parser.add_argument("--dry-run", action="store_true", help="Log matches instead of sending")
    args = parser.parse_args(argv)

    try:
        cfg = load_monitor_config(
            sources_override=args.sources,
            target_override=args.target,
            min_score_override=args.min_score,
        )
        monitor = TelegramLeadMonitor(cfg, dry_run=args.dry_run)
        asyncio.run(monitor.run(backfill_limit=args.backfill, once=args.once))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
