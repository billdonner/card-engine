"""Studio CRUD endpoints for content management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from server import db
from server.models import (
    CardOut,
    CreateCardIn,
    CreateDeckIn,
    DeckDetailOut,
    DeckSummaryOut,
    ReorderCardsIn,
    SearchOut,
    SearchResultOut,
    UpdateCardIn,
    UpdateDeckIn,
)

router = APIRouter(prefix="/api/v1/studio", tags=["studio"])


# ---------------------------------------------------------------------------
# Decks
# ---------------------------------------------------------------------------

@router.post("/decks", status_code=201)
async def create_deck(body: CreateDeckIn) -> DeckSummaryOut:
    """Create a new deck."""
    if body.kind not in ("flashcard", "trivia", "newsquiz"):
        raise HTTPException(400, f"Invalid kind: {body.kind}")
    row = await db.create_deck(body.title, body.kind, body.properties)
    return DeckSummaryOut(
        id=row["id"],
        title=row["title"],
        kind=row["kind"],
        properties=row["properties"] or {},
        card_count=row["card_count"],
        created_at=row["created_at"],
    )


@router.patch("/decks/{deck_id}")
async def update_deck(deck_id: UUID, body: UpdateDeckIn) -> DeckSummaryOut:
    """Update deck metadata."""
    row = await db.update_deck(str(deck_id), body.title, body.properties)
    if row is None:
        raise HTTPException(404, "Deck not found")
    return DeckSummaryOut(
        id=row["id"],
        title=row["title"],
        kind=row["kind"],
        properties=row["properties"] or {},
        card_count=row["card_count"],
        created_at=row["created_at"],
    )


@router.post("/decks/{deck_id}/publish")
async def publish_deck(deck_id: UUID) -> DeckSummaryOut:
    """Publish a deck so it appears in the catalog."""
    deck_row, _ = await db.get_deck(str(deck_id))
    if deck_row is None:
        raise HTTPException(404, "Deck not found")
    props = dict(deck_row["properties"] or {})
    props["status"] = "published"
    row = await db.update_deck(str(deck_id), None, props)
    return DeckSummaryOut(
        id=row["id"], title=row["title"], kind=row["kind"],
        properties=row["properties"] or {}, card_count=row["card_count"],
        created_at=row["created_at"],
    )


@router.post("/decks/{deck_id}/unpublish")
async def unpublish_deck(deck_id: UUID) -> DeckSummaryOut:
    """Revert a deck to draft status."""
    deck_row, _ = await db.get_deck(str(deck_id))
    if deck_row is None:
        raise HTTPException(404, "Deck not found")
    props = dict(deck_row["properties"] or {})
    props["status"] = "draft"
    row = await db.update_deck(str(deck_id), None, props)
    return DeckSummaryOut(
        id=row["id"], title=row["title"], kind=row["kind"],
        properties=row["properties"] or {}, card_count=row["card_count"],
        created_at=row["created_at"],
    )


@router.delete("/decks/{deck_id}")
async def delete_deck(deck_id: UUID) -> dict:
    """Delete a deck and all its cards."""
    deleted = await db.delete_deck(str(deck_id))
    if not deleted:
        raise HTTPException(404, "Deck not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

@router.post("/decks/{deck_id}/cards", status_code=201)
async def create_card(deck_id: UUID, body: CreateCardIn) -> CardOut:
    """Create a new card in a deck."""
    # Verify deck exists
    deck_row, _ = await db.get_deck(str(deck_id))
    if deck_row is None:
        raise HTTPException(404, "Deck not found")

    if body.difficulty not in ("easy", "medium", "hard"):
        raise HTTPException(400, f"Invalid difficulty: {body.difficulty}")

    row = await db.create_card(str(deck_id), body.question, body.properties, body.difficulty)
    return CardOut(
        id=row["id"],
        position=row["position"],
        question=row["question"],
        properties=row["properties"] or {},
        difficulty=row["difficulty"],
        source_url=row["source_url"],
        source_date=row["source_date"],
    )


@router.patch("/decks/{deck_id}/cards/{card_id}")
async def update_card(deck_id: UUID, card_id: UUID, body: UpdateCardIn) -> CardOut:
    """Update a card."""
    if body.difficulty is not None and body.difficulty not in ("easy", "medium", "hard"):
        raise HTTPException(400, f"Invalid difficulty: {body.difficulty}")

    row = await db.update_card(str(card_id), body.question, body.properties, body.difficulty)
    if row is None:
        raise HTTPException(404, "Card not found")
    if str(row["deck_id"]) != str(deck_id):
        raise HTTPException(404, "Card not found in this deck")

    return CardOut(
        id=row["id"],
        position=row["position"],
        question=row["question"],
        properties=row["properties"] or {},
        difficulty=row["difficulty"],
        source_url=row["source_url"],
        source_date=row["source_date"],
    )


@router.delete("/decks/{deck_id}/cards/{card_id}")
async def delete_card(deck_id: UUID, card_id: UUID) -> dict:
    """Delete a card."""
    deleted = await db.delete_card(str(card_id))
    if not deleted:
        raise HTTPException(404, "Card not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

@router.post("/decks/{deck_id}/cards/reorder")
async def reorder_cards(deck_id: UUID, body: ReorderCardsIn) -> DeckDetailOut:
    """Reorder cards in a deck."""
    deck_row, _ = await db.get_deck(str(deck_id))
    if deck_row is None:
        raise HTTPException(404, "Deck not found")

    card_id_strs = [str(cid) for cid in body.card_ids]
    cards = await db.reorder_cards(str(deck_id), card_id_strs)
    return DeckDetailOut(
        id=deck_row["id"],
        title=deck_row["title"],
        kind=deck_row["kind"],
        properties=deck_row["properties"] or {},
        card_count=deck_row["card_count"],
        created_at=deck_row["created_at"],
        cards=[
            CardOut(
                id=c["id"],
                position=c["position"],
                question=c["question"],
                properties=c["properties"] or {},
                difficulty=c["difficulty"],
                source_url=c["source_url"],
                source_date=c["source_date"],
            )
            for c in cards
        ],
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get("/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)) -> SearchOut:
    """Full-text search across all cards."""
    rows, total = await db.search_cards(q, limit)
    return SearchOut(
        results=[
            SearchResultOut(
                card_id=r["card_id"],
                deck_id=r["deck_id"],
                deck_title=r["deck_title"],
                deck_kind=r["deck_kind"],
                question=r["question"],
                properties=r["properties"] or {},
                rank=float(r["rank"]),
            )
            for r in rows
        ],
        total=total,
    )
