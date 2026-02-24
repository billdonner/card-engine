"""Trivia adapter routes — backward-compatible with alities-mobile / alities-studio."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter

from server.db import get_all_decks_with_cards, get_categories_with_counts
from server.models import (
    CategoriesOut,
    CategoryOut,
    ChallengeOut,
    GameDataOut,
)

router = APIRouter(prefix="/api/v1/trivia", tags=["trivia"])


@router.get("/gamedata", response_model=GameDataOut)
async def get_gamedata():
    """Bulk export all trivia content in alities Challenge format."""
    rows = await get_all_decks_with_cards("trivia")

    challenges: list[ChallengeOut] = []
    for r in rows:
        if r["card_id"] is None:
            continue

        props = r["card_props"] or {}
        deck_props = r["deck_props"] or {}
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
            )
        )

    return GameDataOut(
        id=str(uuid4()),
        generated=datetime.now(timezone.utc).isoformat(),
        challenges=challenges,
    )


@router.get("/categories", response_model=CategoriesOut)
async def get_categories():
    """List trivia categories with counts and SF Symbol pics."""
    rows = await get_categories_with_counts()
    categories = [
        CategoryOut(
            name=r["title"],
            pic=r["pic"] or "questionmark.circle",
            count=r["card_count"],
        )
        for r in rows
    ]
    return CategoriesOut(categories=categories, total=len(categories))
