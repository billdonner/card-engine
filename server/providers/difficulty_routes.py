"""API endpoints for AI difficulty scoring."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from server.db import get_pool
from server.providers.difficulty import scorer

router = APIRouter(prefix="/api/v1/difficulty", tags=["difficulty"])


@router.get("/status")
async def difficulty_status():
    """Current state and stats of the difficulty scorer."""
    pool = get_pool()
    scored = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM cards c JOIN decks d ON c.deck_id = d.id
        WHERE d.kind = 'trivia' AND (c.properties->>'ai_difficulty') IS NOT NULL
        """
    )
    unscored = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM cards c JOIN decks d ON c.deck_id = d.id
        WHERE d.kind = 'trivia' AND (c.properties->>'ai_difficulty') IS NULL
        """
    )
    return {
        **scorer.status,
        "db_scored": scored,
        "db_unscored": unscored,
    }


@router.post("/start")
async def start_scoring():
    """Start the batch difficulty scoring job."""
    api_key = os.environ.get("CE_ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="CE_ANTHROPIC_API_KEY not set")

    batch_size = int(os.environ.get("CE_DIFFICULTY_BATCH_SIZE", "20"))
    concurrency = int(os.environ.get("CE_DIFFICULTY_CONCURRENCY", "5"))

    pool = get_pool()
    await scorer.start(pool, api_key, batch_size, concurrency)
    return {"message": "Difficulty scoring started", **scorer.status}


@router.post("/stop")
async def stop_scoring():
    """Stop the difficulty scoring job."""
    await scorer.stop()
    return {"message": "Difficulty scoring stopped", **scorer.status}
