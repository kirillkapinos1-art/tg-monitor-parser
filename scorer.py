"""AI relevance scorer using Google Gemini structured JSON output."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger("tg_monitor.scorer")
_RETRYABLE_CODES = {404, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class LeadAssessment:
    """Structured relevance evaluation returned for a candidate Telegram post."""

    relevant: bool
    score: int
    category: str
    summary: str
    reason: str


class GeminiLeadScorer:
    """Scores candidate Telegram posts for freelance/lead relevance via Gemini."""

    def __init__(
        self,
        api_key: str,
        models: Sequence[str],
        *,
        genai_client: Any | None = None,
    ) -> None:
        self._client = genai_client or genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=20000,
                retry_options=genai_types.HttpRetryOptions(attempts=1),
            ),
        )
        self._models = tuple(models)

    @staticmethod
    def _build_prompt(text: str, matched_keywords: Sequence[str]) -> str:
        kw_str = ", ".join(matched_keywords) if matched_keywords else "нет (общий поток)"
        return (
            "Оцени публикацию из Telegram на предмет того, является ли она реальным запросом "
            "или заказом на фриланс / разработку / 3D / CAD (а не рекламой курсов, спамом или мемом).\n"
            f"Совпавшие ключевые слова: {kw_str}\n\n"
            f"Текст поста:\n{text[:3500]}\n\n"
            "Верни строго JSON со следующими полями:\n"
            "- relevant (boolean): true если это реальный заказ/запрос услуги или вакансия\n"
            "- score (integer 1..10): оценка полезности и релевантности лида\n"
            "- category (string): краткая категория (например: 'Telegram Bot', 'Python', 'Blender 3D', 'AutoCAD', 'Roblox')\n"
            "- summary (string): 1-2 предложения сути задачи\n"
            "- reason (string): короткое обоснование оценки"
        )

    @staticmethod
    def parse_response_json(raw_text: str | None) -> LeadAssessment:
        """Parse and clamp JSON output into a LeadAssessment instance."""
        if not raw_text:
            return LeadAssessment(
                relevant=False,
                score=1,
                category="unknown",
                summary="",
                reason="Empty model response",
            )
        data = json.loads(raw_text)
        raw_score = int(data.get("score", 1))
        clamped_score = max(1, min(raw_score, 10))
        return LeadAssessment(
            relevant=bool(data.get("relevant", False)),
            score=clamped_score,
            category=str(data.get("category", "General")).strip() or "General",
            summary=str(data.get("summary", "")).strip(),
            reason=str(data.get("reason", "")).strip(),
        )

    def _score_sync(
        self, model: str, text: str, matched_keywords: Sequence[str]
    ) -> LeadAssessment:
        prompt = self._build_prompt(text, matched_keywords)
        schema = {
            "type": "OBJECT",
            "properties": {
                "relevant": {"type": "BOOLEAN"},
                "score": {"type": "INTEGER"},
                "category": {"type": "STRING"},
                "summary": {"type": "STRING"},
                "reason": {"type": "STRING"},
            },
            "required": ["relevant", "score", "category", "summary", "reason"],
        }
        response = self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return self.parse_response_json(getattr(response, "text", None))

    async def score_post(
        self, text: str, matched_keywords: Sequence[str] = ()
    ) -> LeadAssessment:
        """Score a post trying configured Gemini models with automatic fallback."""
        last_error: Exception | None = None
        for model in self._models:
            try:
                return await asyncio.to_thread(
                    self._score_sync, model, text, matched_keywords
                )
            except genai_errors.APIError as exc:
                last_error = exc
                if exc.code in _RETRYABLE_CODES:
                    logger.warning(
                        "Model '%s' unavailable (code=%s), trying next fallback...",
                        model,
                        exc.code,
                    )
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("No Gemini models configured.")
