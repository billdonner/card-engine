"""Trivia adapter routes — backward-compatible with alities-mobile / alities-studio."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from server.db import (
    create_session,
    get_all_decks_with_cards,
    get_categories_with_counts,
    get_player,
    get_player_seen_card_ids,
    record_seen_cards,
)
from server.models import (
    CategoriesOut,
    CategoryOut,
    ChallengeOut,
    GameDataOut,
)

router = APIRouter(prefix="/api/v1/trivia", tags=["trivia"])


def _build_challenges(rows) -> list[ChallengeOut]:
    """Convert raw deck+card JOIN rows into ChallengeOut objects."""
    challenges: list[ChallengeOut] = []
    for r in rows:
        if r["card_id"] is None:
            continue

        raw_props = r["card_props"]
        props = raw_props if isinstance(raw_props, dict) else {}
        raw_deck_props = r["deck_props"]
        deck_props = raw_deck_props if isinstance(raw_deck_props, dict) else {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)

        # Extract answer texts from choice objects
        answer_texts = [
            c["text"] if isinstance(c, dict) else str(c) for c in choices
        ]
        correct_answer = answer_texts[correct_idx] if correct_idx < len(answer_texts) else ""

        challenges.append(
            ChallengeOut(
                id=str(r["card_id"]),
                topic=r["title"],
                pic=deck_props.get("pic", "questionmark.circle"),
                question=r["question"],
                answers=answer_texts,
                correct=correct_answer,
                explanation=props.get("explanation", ""),
                hint=props.get("hint", ""),
                aisource=props.get("aisource", "card-engine"),
                date=r["source_date"].isoformat() if r["source_date"] else "",
                ai_difficulty=props.get("ai_difficulty"),
            )
        )
    return challenges


@router.get("/gamedata", response_model=GameDataOut)
async def get_gamedata(
    tier: str | None = Query(None, description="Filter by deck tier: free, family, premium"),
    categories: str | None = Query(None, description="Comma-separated category names to include"),
    player_id: UUID | None = Query(None, description="Player UUID — enables seen-card exclusion and session creation"),
    count: int | None = Query(None, ge=1, le=500, description="Random sample of N questions"),
    app_id: str = Query("qross", description="App identifier for history tracking"),
):
    """Bulk export trivia content in alities Challenge format.

    When player_id is provided, previously seen cards are excluded,
    a session is auto-created, and the response includes session metadata.
    """
    cat_list = [c.strip() for c in categories.split(",") if c.strip()] if categories else None
    rows = await get_all_decks_with_cards("trivia", tier=tier, categories=cat_list, exclude_quarantined=True)

    challenges = _build_challenges(rows)
    total_available = len(challenges)

    # --- Player-aware filtering ---
    session_id_str: str | None = None
    share_code: str | None = None
    fresh_count: int | None = None
    player = None

    if player_id is not None:
        player = await get_player(player_id)
        if player is not None:
            seen_ids = await get_player_seen_card_ids(player_id, app_id)
            challenges = [c for c in challenges if UUID(c.id) not in seen_ids]
            fresh_count = len(challenges)

    # --- Random sampling ---
    if count is not None and len(challenges) > count:
        challenges = random.sample(challenges, count)

    # --- Auto-create session when player_id provided ---
    if player_id is not None and player is not None:
        dealt_card_ids = [UUID(c.id) for c in challenges]
        await record_seen_cards(player_id, dealt_card_ids, app_id)

        sid, scode = await create_session(player_id, dealt_card_ids, app_id)
        session_id_str = str(sid)
        share_code = scode

    response = GameDataOut(
        id=str(uuid4()),
        generated=datetime.now(timezone.utc).isoformat(),
        challenges=challenges,
    )

    # When player_id provided, add session metadata via JSONResponse
    # to avoid response_model stripping extra fields
    if player_id is not None and session_id_str is not None:
        result = response.model_dump()
        result["session_id"] = session_id_str
        result["share_code"] = share_code
        result["fresh_count"] = fresh_count
        result["total_available"] = total_available
        return JSONResponse(content=result)

    return response


@router.get("/categories", response_model=CategoriesOut)
async def get_categories(tier: str | None = Query(None, description="Filter by deck tier: free, family, premium")):
    """List trivia categories with counts and SF Symbol pics."""
    rows = await get_categories_with_counts(tier=tier)
    categories = [
        CategoryOut(
            name=r["title"],
            pic=r["pic"] or "questionmark.circle",
            count=r["card_count"],
        )
        for r in rows
    ]
    return CategoriesOut(categories=categories, total=len(categories))
