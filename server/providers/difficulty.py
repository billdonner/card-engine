"""AI difficulty scoring for trivia questions using Claude Haiku."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """\
You are a trivia question difficulty scorer. Rate each question as 'easy', 'medium', or 'hard'.

Rubric:
- easy: Common knowledge most adults would know, straightforward question, clearly distinct answer choices
- medium: Requires some specific knowledge, moderately tricky distractors, or a less familiar sub-topic
- hard: Specialized/obscure knowledge, very similar answer choices, or requires expert-level familiarity

Respond with ONLY one word: easy, medium, or hard."""


async def score_question(
    client: httpx.AsyncClient,
    api_key: str,
    question: str,
    choices: list[dict],
    correct_answer: str,
) -> str | None:
    """Score a single question's difficulty using Claude Haiku."""
    choices_text = "\n".join(
        f"  {chr(65 + i)}) {c['text']}" for i, c in enumerate(choices)
    )
    user_prompt = (
        f"Question: {question}\n"
        f"Choices:\n{choices_text}\n"
        f"Correct answer: {correct_answer}"
    )

    try:
        response = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 10,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"].strip().lower()
        if text in ("easy", "medium", "hard"):
            return text
        logger.warning("Unexpected difficulty response: %s", text)
        return None
    except Exception as e:
        logger.error("Difficulty scoring error: %s", e)
        return None


class DifficultyScorer:
    """Async batch scorer for trivia question difficulty."""

    def __init__(self) -> None:
        self.state = "stopped"  # stopped, running, stopping
        self.stats: dict = {
            "total_scored": 0,
            "total_errors": 0,
            "total_remaining": 0,
            "start_time": None,
            "last_scored_at": None,
        }
        self._task: asyncio.Task | None = None

    @property
    def status(self) -> dict:
        return {"state": self.state, **self.stats}

    async def start(
        self,
        pool,
        api_key: str,
        batch_size: int = 20,
        concurrency: int = 5,
    ) -> None:
        if self.state == "running":
            return
        self.state = "running"
        self.stats = {
            "total_scored": 0,
            "total_errors": 0,
            "total_remaining": 0,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "last_scored_at": None,
        }
        self._task = asyncio.create_task(
            self._run(pool, api_key, batch_size, concurrency)
        )

    async def stop(self) -> None:
        if self.state != "running":
            return
        self.state = "stopping"
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state = "stopped"

    async def _run(
        self,
        pool,
        api_key: str,
        batch_size: int,
        concurrency: int,
    ) -> None:
        try:
            async with httpx.AsyncClient() as client:
                while self.state == "running":
                    # Fetch unscored trivia cards
                    rows = await pool.fetch(
                        """
                        SELECT c.id, c.question, c.properties
                        FROM cards c
                        JOIN decks d ON c.deck_id = d.id
                        WHERE d.kind = 'trivia'
                          AND (c.properties->>'ai_difficulty') IS NULL
                        LIMIT $1
                        """,
                        batch_size,
                    )

                    if not rows:
                        logger.info("All trivia questions scored.")
                        break

                    self.stats["total_remaining"] = await pool.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM cards c
                        JOIN decks d ON c.deck_id = d.id
                        WHERE d.kind = 'trivia'
                          AND (c.properties->>'ai_difficulty') IS NULL
                        """
                    )

                    # Process batch with concurrency limit
                    sem = asyncio.Semaphore(concurrency)

                    async def score_one(row) -> None:
                        async with sem:
                            if self.state != "running":
                                return
                            props = row["properties"] or {}
                            choices = props.get("choices", [])
                            correct_idx = props.get("correct_index", 0)
                            correct_answer = (
                                choices[correct_idx]["text"]
                                if choices and correct_idx < len(choices)
                                else ""
                            )

                            difficulty = await score_question(
                                client, api_key, row["question"], choices, correct_answer
                            )

                            if difficulty:
                                await pool.execute(
                                    "UPDATE cards SET properties = properties || $2 WHERE id = $1",
                                    row["id"],
                                    {"ai_difficulty": difficulty},
                                )
                                self.stats["total_scored"] += 1
                                self.stats["last_scored_at"] = datetime.now(
                                    timezone.utc
                                ).isoformat()
                            else:
                                self.stats["total_errors"] += 1

                    await asyncio.gather(*(score_one(row) for row in rows))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Difficulty scorer error: %s", e)
        finally:
            self.stats["total_remaining"] = 0
            if self.state != "stopping":
                self.state = "stopped"


# Global scorer instance
scorer = DifficultyScorer()
