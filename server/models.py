"""Pydantic models for card-engine API request/response shapes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Generic (Layer 1)
# ---------------------------------------------------------------------------

class CardOut(BaseModel):
    id: UUID
    position: int
    question: str
    properties: dict
    difficulty: str
    source_url: str | None = None
    source_date: datetime | None = None


class DeckSummaryOut(BaseModel):
    id: UUID
    title: str
    kind: str
    properties: dict
    card_count: int
    created_at: datetime


class DeckDetailOut(DeckSummaryOut):
    cards: list[CardOut]


class DecksListOut(BaseModel):
    decks: list[DeckSummaryOut]
    total: int


# ---------------------------------------------------------------------------
# Flashcard adapter (maps to obo-ios / flasherz-ios expected format)
# ---------------------------------------------------------------------------

class FlashcardCardOut(BaseModel):
    position: int
    question: str
    answer: str


class FlashcardDeckOut(BaseModel):
    id: UUID
    topic: str
    age_range: str
    voice: str | None = None
    card_count: int
    created_at: datetime
    cards: list[FlashcardCardOut]


class FlashcardsOut(BaseModel):
    decks: list[FlashcardDeckOut]
    total: int


# ---------------------------------------------------------------------------
# Trivia adapter (maps to alities-mobile expected format)
# ---------------------------------------------------------------------------

class ChallengeOut(BaseModel):
    id: str
    topic: str
    pic: str
    question: str
    answers: list[str]
    correct: str
    explanation: str
    hint: str
    aisource: str
    date: str


class GameDataOut(BaseModel):
    id: str
    generated: str
    challenges: list[ChallengeOut]


class CategoryOut(BaseModel):
    name: str
    pic: str
    count: int


class CategoriesOut(BaseModel):
    categories: list[CategoryOut]
    total: int


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------

class IngestionStatusOut(BaseModel):
    state: str  # stopped, running, paused
    stats: dict
    config: dict
    message: str | None = None


class SourceRunOut(BaseModel):
    id: UUID
    provider_name: str
    started_at: datetime
    finished_at: datetime | None = None
    items_fetched: int
    items_added: int
    items_skipped: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Studio CRUD (write endpoints)
# ---------------------------------------------------------------------------

class CreateDeckIn(BaseModel):
    title: str
    kind: str  # flashcard, trivia, newsquiz
    properties: dict = {}


class UpdateDeckIn(BaseModel):
    title: str | None = None
    properties: dict | None = None


class CreateCardIn(BaseModel):
    question: str
    properties: dict = {}
    difficulty: str = "medium"  # easy, medium, hard


class UpdateCardIn(BaseModel):
    question: str | None = None
    properties: dict | None = None
    difficulty: str | None = None


class ReorderCardsIn(BaseModel):
    card_ids: list[UUID]


class SearchResultOut(BaseModel):
    card_id: UUID
    deck_id: UUID
    deck_title: str
    deck_kind: str
    question: str
    properties: dict
    rank: float


class SearchOut(BaseModel):
    results: list[SearchResultOut]
    total: int
