"""Generic deck/card CRUD routes — Layer 1."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from server.db import get_deck, list_decks
from server.models import CardOut, DeckDetailOut, DeckSummaryOut, DecksListOut

router = APIRouter(prefix="/api/v1/decks", tags=["decks"])


@router.get("", response_model=DecksListOut)
async def list_all_decks(
    kind: str | None = Query(None, description="Filter by kind (flashcard, trivia, newsquiz)"),
    age: str | None = Query(None, description="Filter by age_range property"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List decks with optional kind and age filters."""
    rows, total = await list_decks(kind=kind, age=age, limit=limit, offset=offset)
    decks = [
        DeckSummaryOut(
            id=r["id"],
            title=r["title"],
            kind=r["kind"],
            properties=r["properties"],
            card_count=r["card_count"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return DecksListOut(decks=decks, total=total)


@router.get("/{deck_id}", response_model=DeckDetailOut)
async def get_single_deck(deck_id: UUID):
    """Get a single deck with all its cards."""
    row, card_rows = await get_deck(str(deck_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Deck {deck_id} not found")

    cards = [
        CardOut(
            id=c["id"],
            position=c["position"],
            question=c["question"],
            properties=c["properties"],
            difficulty=c["difficulty"],
            source_url=c["source_url"],
            source_date=c["source_date"],
        )
        for c in card_rows
    ]
    return DeckDetailOut(
        id=row["id"],
        title=row["title"],
        kind=row["kind"],
        properties=row["properties"],
        card_count=row["card_count"],
        created_at=row["created_at"],
        cards=cards,
    )
