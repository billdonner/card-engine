"""Pydantic models for the family tree feature."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Family CRUD
# ---------------------------------------------------------------------------

class CreateFamilyIn(BaseModel):
    name: str


class FamilyOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# People CRUD
# ---------------------------------------------------------------------------

class CreatePersonIn(BaseModel):
    name: str
    nickname: str | None = None
    maiden_name: str | None = None
    born: int | None = None
    status: str = "living"
    notes: str | None = None
    player: bool = False
    placeholder: bool = False
    photo_url: str | None = None


class UpdatePersonIn(BaseModel):
    name: str | None = None
    nickname: str | None = None
    maiden_name: str | None = None
    born: int | None = None
    status: str | None = None
    notes: str | None = None
    player: bool | None = None
    placeholder: bool | None = None
    photo_url: str | None = None


class PersonOut(BaseModel):
    id: UUID
    family_id: UUID
    name: str
    nickname: str | None = None
    maiden_name: str | None = None
    born: int | None = None
    status: str
    notes: str | None = None
    player: bool
    placeholder: bool
    photo_url: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

class CreateRelationshipIn(BaseModel):
    type: str  # married, parent_of, divorced
    from_id: UUID
    to_id: UUID
    year: int | None = None
    ended: bool = False
    end_reason: str | None = None
    notes: str | None = None


class RelationshipOut(BaseModel):
    id: UUID
    family_id: UUID
    type: str
    from_id: UUID
    to_id: UUID
    year: int | None = None
    ended: bool
    end_reason: str | None = None
    notes: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Composite tree view
# ---------------------------------------------------------------------------

class FamilyTreeOut(BaseModel):
    family: FamilyOut
    people: list[PersonOut]
    relationships: list[RelationshipOut]


# ---------------------------------------------------------------------------
# Open items (placeholders / missing fields)
# ---------------------------------------------------------------------------

class OpenItemOut(BaseModel):
    person_id: UUID
    person_name: str
    issue: str  # e.g. "placeholder", "missing birth year", "no relationships"


# ---------------------------------------------------------------------------
# Chat interface
# ---------------------------------------------------------------------------

class ChatMessageIn(BaseModel):
    message: str


class ChatResponseOut(BaseModel):
    reply: str
    patches_applied: int
    tree: FamilyTreeOut


class ChatHistoryOut(BaseModel):
    session_id: UUID
    messages: list[dict]


# ---------------------------------------------------------------------------
# Deck generation
# ---------------------------------------------------------------------------

class GenerateDeckIn(BaseModel):
    kinds: list[str] = ["flashcard", "trivia"]


class GenerateDeckOut(BaseModel):
    deck_ids: list[UUID]
    cards_created: int
    player_name: str
