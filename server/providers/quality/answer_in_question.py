"""Detect trivia questions where the correct answer appears in the question text.

These questions are trivially solvable and should be auto-deleted.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger("card_engine.quality.aiq")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower().strip())


def _answer_in_question(question: str, answer: str) -> bool:
    """Check if the answer text appears as a meaningful substring in the question.

    Uses normalized text. Requires answer to be at least 3 chars to avoid
    false positives on short words like "a", "an", "the".
    """
    norm_q = _normalize(question)
    norm_a = _normalize(answer)

    if len(norm_a) < 3:
        return False

    # Direct substring match
    if norm_a in norm_q:
        return True

    # Also check word-level: all words of the answer appear in the question
    answer_words = set(norm_a.split())
    question_words = set(norm_q.split())
    if len(answer_words) >= 2 and answer_words.issubset(question_words):
        return True

    return False


@dataclass
class AIQMatch:
    card_id: str
    question: str
    correct_answer: str
    topic: str


@dataclass
class AIQResult:
    total_scanned: int = 0
    matches: list[AIQMatch] = field(default_factory=list)
    deleted: int = 0
    elapsed_seconds: float = 0.0
    dry_run: bool = False


async def scan_answer_in_question(
    pool: asyncpg.Pool,
    dry_run: bool = False,
) -> AIQResult:
    """Scan all trivia cards for answer-in-question. Auto-deletes matches unless dry_run."""
    t0 = time.time()

    rows = await pool.fetch(
        "SELECT c.id::text, c.question, c.properties, d.title AS topic "
        "FROM cards c "
        "JOIN decks d ON d.id = c.deck_id "
        "WHERE d.kind = 'trivia' "
        "  AND c.quarantined = FALSE "
        "ORDER BY c.created_at"
    )

    result = AIQResult(total_scanned=len(rows), dry_run=dry_run)

    delete_ids: list[str] = []

    for r in rows:
        raw_props = r["properties"]
        props = raw_props if isinstance(raw_props, dict) else {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)
        correct_answer = ""
        if choices and correct_idx < len(choices):
            c = choices[correct_idx]
            correct_answer = c["text"] if isinstance(c, dict) else str(c)

        if _answer_in_question(r["question"], correct_answer):
            result.matches.append(AIQMatch(
                card_id=r["id"],
                question=r["question"],
                correct_answer=correct_answer,
                topic=r["topic"],
            ))
            delete_ids.append(r["id"])

    if not dry_run and delete_ids:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for card_id in delete_ids:
                    await conn.execute(
                        "DELETE FROM cards WHERE id = $1::uuid",
                        card_id,
                    )
        result.deleted = len(delete_ids)

    result.elapsed_seconds = round(time.time() - t0, 2)
    return result
