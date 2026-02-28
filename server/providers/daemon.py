"""Ingestion daemon — async background task that generates trivia via OpenAI.

Ported from alities-engine TriviaGenDaemon.swift.
Runs as an asyncio background task started in FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from datetime import datetime, timezone

import asyncpg

from server.providers.categories import CANONICAL_CATEGORIES, symbol_for
from server.providers.dedup import DedupService
from server.providers.openai_provider import fetch_questions

logger = logging.getLogger("card_engine.daemon")


class IngestionConfig:
    """Configuration from environment variables (CE_ prefix)."""

    def __init__(self) -> None:
        self.openai_api_key: str = os.environ.get("CE_OPENAI_API_KEY", "")
        self.cycle_seconds: int = int(os.environ.get("CE_INGEST_CYCLE_SECONDS", "60"))
        self.batch_size: int = int(os.environ.get("CE_INGEST_BATCH_SIZE", "10"))
        self.auto_start: bool = os.environ.get("CE_INGEST_AUTO_START", "").lower() in (
            "1",
            "true",
            "yes",
        )
        self.concurrent_batches: int = int(
            os.environ.get("CE_INGEST_CONCURRENT_BATCHES", "5")
        )

    def to_dict(self) -> dict:
        return {
            "cycle_seconds": self.cycle_seconds,
            "batch_size": self.batch_size,
            "auto_start": self.auto_start,
            "concurrent_batches": self.concurrent_batches,
            "has_api_key": bool(self.openai_api_key),
        }


class IngestionDaemon:
    """Background trivia ingestion orchestrator."""

    def __init__(self, pool: asyncpg.Pool, config: IngestionConfig | None = None):
        self._pool = pool
        self._config = config or IngestionConfig()
        self._dedup = DedupService()
        self._task: asyncio.Task | None = None
        self.state: str = "stopped"  # stopped | running | paused
        self.stats: dict = {
            "start_time": None,
            "total_fetched": 0,
            "items_added": 0,
            "duplicates_skipped": 0,
            "errors": 0,
            "cycles_completed": 0,
            "provider_stats": {},
        }

    # ------------------------------------------------------------------
    # Control methods
    # ------------------------------------------------------------------

    async def start(self) -> str:
        if self.state == "running":
            return "already running"
        if not self._config.openai_api_key:
            return "CE_OPENAI_API_KEY not set"

        # Pre-load dedup cache
        await self._dedup.load_existing(self._pool)

        self.state = "running"
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Ingestion daemon started")
        return "started"

    async def stop(self) -> str:
        if self.state == "stopped":
            return "already stopped"
        self.state = "stopped"
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Ingestion daemon stopped")
        return "stopped"

    def pause(self) -> str:
        if self.state != "running":
            return f"cannot pause from state={self.state}"
        self.state = "paused"
        logger.info("Ingestion daemon paused")
        return "paused"

    def resume(self) -> str:
        if self.state != "paused":
            return f"cannot resume from state={self.state}"
        self.state = "running"
        logger.info("Ingestion daemon resumed")
        return "running"

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "stats": dict(self.stats),
            "config": self._config.to_dict(),
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self.state == "running":
            try:
                await self._run_cycle()
                self.stats["cycles_completed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Cycle error: %s", exc)
                self.stats["errors"] += 1

            # Sleep in 1-second increments so stop/pause is responsive
            for _ in range(self._config.cycle_seconds):
                if self.state != "running":
                    break
                await asyncio.sleep(1)

            # If paused, wait until resumed or stopped
            while self.state == "paused":
                await asyncio.sleep(1)

    async def _run_cycle(self) -> None:
        """One ingestion cycle: fetch from OpenAI, dedup, insert into DB."""
        provider_id = await self._ensure_provider()
        run_id = await self._create_run(provider_id)

        fetched = 0
        added = 0
        skipped = 0
        error_msg: str | None = None

        try:
            questions = await fetch_questions(
                api_key=self._config.openai_api_key,
                categories=CANONICAL_CATEGORIES,
                batch_size=self._config.batch_size,
                concurrent_batches=self._config.concurrent_batches,
            )
            fetched = len(questions)
            self.stats["total_fetched"] += fetched
            logger.info("Fetched %d questions from OpenAI", fetched)

            for q in questions:
                if self.state != "running":
                    break

                question_text = q["question"]
                choices = q["choices"]
                correct_idx = q["correct_index"]
                correct_answer = ""
                if choices and correct_idx < len(choices):
                    correct_answer = choices[correct_idx].get("text", "")

                if self._dedup.is_duplicate(question_text, correct_answer):
                    skipped += 1
                    self.stats["duplicates_skipped"] += 1
                    continue

                try:
                    card_id = await self._insert_card(q)
                    self._dedup.register(question_text, correct_answer, str(card_id))
                    added += 1
                    self.stats["items_added"] += 1
                except Exception as exc:
                    logger.error("Failed to insert card: %s", exc)
                    self.stats["errors"] += 1

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Cycle fetch failed: %s", exc)
            self.stats["errors"] += 1

        await self._finish_run(run_id, fetched, added, skipped, error_msg)
        logger.info(
            "Cycle complete: fetched=%d added=%d skipped=%d errors=%s",
            fetched, added, skipped, error_msg or "none",
        )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _ensure_provider(self) -> uuid.UUID:
        """Get or create the 'openai' source_providers row."""
        row = await self._pool.fetchrow(
            "SELECT id FROM source_providers WHERE name = $1", "openai"
        )
        if row:
            return row["id"]
        new_id = uuid.uuid4()
        await self._pool.execute(
            "INSERT INTO source_providers (id, name, type) VALUES ($1, $2, $3::source_type)",
            new_id, "openai", "api",
        )
        return new_id

    async def _create_run(self, provider_id: uuid.UUID) -> uuid.UUID:
        """Create a source_runs row for this cycle."""
        run_id = uuid.uuid4()
        await self._pool.execute(
            "INSERT INTO source_runs (id, provider_id, started_at) VALUES ($1, $2, $3)",
            run_id, provider_id, datetime.now(timezone.utc),
        )
        return run_id

    async def _finish_run(
        self,
        run_id: uuid.UUID,
        fetched: int,
        added: int,
        skipped: int,
        error: str | None,
    ) -> None:
        """Update the source_runs row with final counts."""
        await self._pool.execute(
            "UPDATE source_runs SET finished_at = $1, items_fetched = $2, "
            "items_added = $3, items_skipped = $4, error = $5 WHERE id = $6",
            datetime.now(timezone.utc), fetched, added, skipped, error, run_id,
        )

    async def _get_or_create_deck(self, category: str) -> uuid.UUID:
        """Get or create a trivia deck for this category."""
        row = await self._pool.fetchrow(
            "SELECT id FROM decks WHERE kind = 'trivia' AND title = $1", category
        )
        if row:
            return row["id"]
        new_id = uuid.uuid4()
        pic = symbol_for(category)
        await self._pool.execute(
            "INSERT INTO decks (id, title, kind, properties, tier) "
            "VALUES ($1, $2, $3::deck_kind, $4, 'free'::deck_tier)",
            new_id, category, "trivia", {"pic": pic},
        )
        logger.info("Created trivia deck: %s (pic=%s)", category, pic)
        return new_id

    async def _insert_card(self, q: dict) -> uuid.UUID:
        """Insert a card into the cards table. Returns the new card id."""
        category = q["category"]
        deck_id = await self._get_or_create_deck(category)

        provider_row = await self._pool.fetchrow(
            "SELECT id FROM source_providers WHERE name = $1", "openai"
        )
        source_id = provider_row["id"] if provider_row else None

        # Get next position in this deck
        max_pos = await self._pool.fetchval(
            "SELECT COALESCE(MAX(position), -1) FROM cards WHERE deck_id = $1", deck_id
        )
        position = (max_pos or 0) + 1

        card_id = uuid.uuid4()
        properties = {
            "choices": q["choices"],
            "correct_index": q["correct_index"],
            "explanation": q.get("explanation", ""),
            "hint": q.get("hint", ""),
            "aisource": "openai",
        }

        await self._pool.execute(
            "INSERT INTO cards (id, deck_id, position, question, properties, difficulty, source_id, source_date) "
            "VALUES ($1, $2, $3, $4, $5, $6::difficulty, $7, $8)",
            card_id, deck_id, position, q["question"], properties,
            q.get("difficulty", "medium"), source_id, datetime.now(timezone.utc),
        )
        return card_id
