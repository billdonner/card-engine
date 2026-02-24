"""Signature + Jaccard deduplication service.

Ported from alities-engine SimilarityService.swift.
Skips AI-based dedup stage — diminishing returns for the complexity.
"""

from __future__ import annotations

import re
import logging

import asyncpg

logger = logging.getLogger("card_engine.dedup")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def _normalize_text(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower().strip())


def _jaccard(a: str, b: str) -> float:
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a and not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


class DedupService:
    """Two-stage duplicate detection: exact signature then Jaccard similarity."""

    def __init__(
        self,
        jaccard_threshold: float = 0.85,
        max_cache: int = 10_000,
        check_limit: int = 1_000,
    ):
        self._jaccard_threshold = jaccard_threshold
        self._max_cache = max_cache
        self._check_limit = check_limit

        # Stage 1: signature → question_id (O(1) lookup)
        self._signatures: dict[str, str] = {}
        self._sig_order: list[str] = []

        # Stage 2: recent normalized texts for Jaccard comparison
        self._texts: list[tuple[str, str]] = []  # (question_id, normalized_text)

    def _make_signature(self, question: str, correct_answer: str) -> str:
        return f"{_normalize_text(question)}|{_normalize_text(correct_answer)}"

    def _evict_if_needed(self) -> None:
        if len(self._signatures) <= self._max_cache:
            return
        evict_count = self._max_cache // 4
        keys_to_remove = self._sig_order[:evict_count]
        for key in keys_to_remove:
            self._signatures.pop(key, None)
        del self._sig_order[:evict_count]
        # Also trim texts list
        if len(self._texts) > self._max_cache:
            self._texts = self._texts[-self._max_cache:]
        logger.debug("Evicted %d entries from dedup cache", evict_count)

    def is_duplicate(self, question: str, correct_answer: str) -> bool:
        """Return True if this question is a duplicate."""
        sig = self._make_signature(question, correct_answer)

        # Stage 1: exact signature match
        if sig in self._signatures:
            return True

        # Stage 2: Jaccard similarity against recent texts
        norm = _normalize_text(question)
        for _, existing_text in self._texts[-self._check_limit:]:
            if _jaccard(norm, existing_text) >= self._jaccard_threshold:
                return True

        return False

    def register(self, question: str, correct_answer: str, question_id: str) -> None:
        """Add a question to the dedup cache."""
        sig = self._make_signature(question, correct_answer)
        self._signatures[sig] = question_id
        self._sig_order.append(sig)
        self._texts.append((question_id, _normalize_text(question)))
        self._evict_if_needed()

    async def load_existing(self, pool: asyncpg.Pool) -> int:
        """Pre-load existing questions from DB into cache. Returns count loaded."""
        rows = await pool.fetch(
            "SELECT id::text, question, properties "
            "FROM cards "
            "WHERE deck_id IN (SELECT id FROM decks WHERE kind = 'trivia') "
            "ORDER BY created_at DESC "
            f"LIMIT {self._max_cache}"
        )
        count = 0
        for row in rows:
            question = row["question"]
            props = row["properties"] or {}
            choices = props.get("choices", [])
            correct_idx = props.get("correct_index", 0)
            correct_answer = ""
            if choices and correct_idx < len(choices):
                c = choices[correct_idx]
                correct_answer = c["text"] if isinstance(c, dict) else str(c)
            self.register(question, correct_answer, row["id"])
            count += 1
        logger.info("Loaded %d existing questions into dedup cache", count)
        return count
