"""Flashcard adapter routes — backward-compatible with obo-ios / flasherz-ios."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter, HTTPException

from server.db import get_all_decks_with_cards, get_deck
from server.models import FlashcardCardOut, FlashcardDeckOut, FlashcardsOut

router = APIRouter(prefix="/api/v1/flashcards", tags=["flashcards"])


def _build_deck(deck_info: dict, cards: list[dict]) -> FlashcardDeckOut:
    """Build a FlashcardDeckOut from raw deck info and card rows."""
    return FlashcardDeckOut(
        id=deck_info["id"],
        topic=deck_info["title"],
        age_range=deck_info["properties"].get("age_range", ""),
        voice=deck_info["properties"].get("voice"),
        card_count=deck_info["card_count"],
        created_at=deck_info["created_at"],
        cards=[
            FlashcardCardOut(
                position=c["position"],
                question=c["question"],
                answer=c["properties"].get("answer", ""),
            )
            for c in cards
        ],
    )


@router.get("", response_model=FlashcardsOut)
async def list_flashcard_decks():
    """Bulk-fetch all flashcard decks with cards in one call (solves N+1)."""
    rows = await get_all_decks_with_cards("flashcard")

    # Group rows by deck
    decks_map: dict[UUID, dict] = {}
    cards_map: defaultdict[UUID, list[dict]] = defaultdict(list)

    for r in rows:
        did = r["deck_id"]
        if did not in decks_map:
            decks_map[did] = {
                "id": did,
                "title": r["title"],
                "properties": r["deck_props"],
                "card_count": r["card_count"],
                "created_at": r["deck_created"],
            }
        if r["card_id"] is not None:
            cards_map[did].append({
                "position": r["position"],
                "question": r["question"],
                "properties": r["card_props"],
            })

    decks = [
        _build_deck(info, cards_map.get(did, []))
        for did, info in decks_map.items()
    ]
    return FlashcardsOut(decks=decks, total=len(decks))


@router.get("/{deck_id}", response_model=FlashcardDeckOut)
async def get_flashcard_deck(deck_id: UUID):
    """Get a single flashcard deck with cards."""
    row, card_rows = await get_deck(str(deck_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Deck {deck_id} not found")
    if row["kind"] != "flashcard":
        raise HTTPException(status_code=404, detail=f"Deck {deck_id} is not a flashcard deck")

    deck_info = {
        "id": row["id"],
        "title": row["title"],
        "properties": row["properties"],
        "card_count": row["card_count"],
        "created_at": row["created_at"],
    }
    cards = [
        {"position": c["position"], "question": c["question"], "properties": c["properties"]}
        for c in card_rows
    ]
    return _build_deck(deck_info, cards)
