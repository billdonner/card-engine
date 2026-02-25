"""Family tree API endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from server.db import get_pool
from server.family import db as fdb
from server.family.models import (
    ChatHistoryOut,
    ChatMessageIn,
    ChatResponseOut,
    CreateFamilyIn,
    CreatePersonIn,
    CreateRelationshipIn,
    FamilyOut,
    FamilyTreeOut,
    GenerateDeckIn,
    GenerateDeckOut,
    OpenItemOut,
    PersonOut,
    RelationshipOut,
    UpdatePersonIn,
)

logger = logging.getLogger("card_engine.family.routes")

router = APIRouter(prefix="/api/v1/family", tags=["family"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _family_out(row) -> FamilyOut:
    return FamilyOut(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _person_out(row) -> PersonOut:
    return PersonOut(
        id=row["id"],
        family_id=row["family_id"],
        name=row["name"],
        nickname=row["nickname"],
        maiden_name=row["maiden_name"],
        born=row["born"],
        status=row["status"],
        gender=row["gender"],
        notes=row["notes"],
        player=row["player"],
        placeholder=row["placeholder"],
        photo_url=row["photo_url"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _rel_out(row) -> RelationshipOut:
    return RelationshipOut(
        id=row["id"],
        family_id=row["family_id"],
        type=row["type"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        year=row["year"],
        ended=row["ended"],
        end_reason=row["end_reason"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


async def _build_tree(family_id: str) -> FamilyTreeOut:
    """Build the full tree response for a family."""
    fam = await fdb.get_family(family_id)
    if fam is None:
        raise HTTPException(404, "Family not found")
    people = await fdb.list_people(family_id)
    rels = await fdb.list_relationships(family_id)
    return FamilyTreeOut(
        family=_family_out(fam),
        people=[_person_out(p) for p in people],
        relationships=[_rel_out(r) for r in rels],
    )


# ---------------------------------------------------------------------------
# Family CRUD
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_family(body: CreateFamilyIn) -> FamilyOut:
    """Create a new family."""
    row = await fdb.create_family(body.name)
    return _family_out(row)


@router.get("")
async def list_families() -> list[FamilyOut]:
    """List all families."""
    rows = await fdb.list_families()
    return [_family_out(r) for r in rows]


@router.get("/{family_id}")
async def get_family(family_id: UUID) -> FamilyTreeOut:
    """Get a family with full tree."""
    return await _build_tree(str(family_id))


@router.delete("/{family_id}")
async def delete_family(family_id: UUID) -> dict:
    """Delete a family and all its data."""
    deleted = await fdb.delete_family(str(family_id))
    if not deleted:
        raise HTTPException(404, "Family not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# People CRUD
# ---------------------------------------------------------------------------

@router.post("/{family_id}/people", status_code=201)
async def create_person(family_id: UUID, body: CreatePersonIn) -> PersonOut:
    """Add a person to the family."""
    fam = await fdb.get_family(str(family_id))
    if fam is None:
        raise HTTPException(404, "Family not found")
    if body.status not in ("living", "deceased"):
        raise HTTPException(400, f"Invalid status: {body.status}")
    row = await fdb.create_person(
        family_id=str(family_id),
        name=body.name,
        nickname=body.nickname,
        maiden_name=body.maiden_name,
        born=body.born,
        status=body.status,
        gender=body.gender,
        notes=body.notes,
        player=body.player,
        placeholder=body.placeholder,
        photo_url=body.photo_url,
    )
    return _person_out(row)


@router.patch("/{family_id}/people/{person_id}")
async def update_person(family_id: UUID, person_id: UUID, body: UpdatePersonIn) -> PersonOut:
    """Update a person's details."""
    if body.status is not None and body.status not in ("living", "deceased"):
        raise HTTPException(400, f"Invalid status: {body.status}")
    row = await fdb.update_person(
        str(person_id),
        name=body.name,
        nickname=body.nickname,
        maiden_name=body.maiden_name,
        born=body.born,
        status=body.status,
        notes=body.notes,
        player=body.player,
        placeholder=body.placeholder,
        photo_url=body.photo_url,
    )
    if row is None:
        raise HTTPException(404, "Person not found")
    if str(row["family_id"]) != str(family_id):
        raise HTTPException(404, "Person not in this family")
    return _person_out(row)


@router.delete("/{family_id}/people/{person_id}")
async def delete_person(family_id: UUID, person_id: UUID) -> dict:
    """Delete a person from the family."""
    deleted = await fdb.delete_person(str(person_id))
    if not deleted:
        raise HTTPException(404, "Person not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

@router.post("/{family_id}/relationships", status_code=201)
async def create_relationship(family_id: UUID, body: CreateRelationshipIn) -> RelationshipOut:
    """Add a relationship between two people."""
    fam = await fdb.get_family(str(family_id))
    if fam is None:
        raise HTTPException(404, "Family not found")
    if body.type not in ("married", "parent_of", "divorced"):
        raise HTTPException(400, f"Invalid relationship type: {body.type}")
    row = await fdb.create_relationship(
        family_id=str(family_id),
        rel_type=body.type,
        from_id=str(body.from_id),
        to_id=str(body.to_id),
        year=body.year,
        ended=body.ended,
        end_reason=body.end_reason,
        notes=body.notes,
    )
    return _rel_out(row)


@router.delete("/{family_id}/relationships/{rel_id}")
async def delete_relationship(family_id: UUID, rel_id: UUID) -> dict:
    """Delete a relationship."""
    deleted = await fdb.delete_relationship(str(rel_id))
    if not deleted:
        raise HTTPException(404, "Relationship not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Tree views
# ---------------------------------------------------------------------------

@router.get("/{family_id}/tree")
async def get_tree(family_id: UUID) -> FamilyTreeOut:
    """Get the full family tree."""
    return await _build_tree(str(family_id))


@router.get("/{family_id}/players")
async def get_players(family_id: UUID) -> list[PersonOut]:
    """Get all players in the family."""
    people = await fdb.list_people(str(family_id))
    return [_person_out(p) for p in people if p["player"]]


@router.get("/{family_id}/open_items")
async def get_open_items(family_id: UUID) -> list[OpenItemOut]:
    """Report placeholders and missing fields."""
    people = await fdb.list_people(str(family_id))
    rels = await fdb.list_relationships(str(family_id))

    items: list[OpenItemOut] = []
    people_with_rels: set[str] = set()
    for r in rels:
        people_with_rels.add(str(r["from_id"]))
        people_with_rels.add(str(r["to_id"]))

    for p in people:
        pid = str(p["id"])
        pname = p["name"]

        if p["placeholder"]:
            items.append(OpenItemOut(person_id=p["id"], person_name=pname, issue="placeholder — needs more details"))

        if not p["born"]:
            items.append(OpenItemOut(person_id=p["id"], person_name=pname, issue="missing birth year"))

        if pid not in people_with_rels:
            items.append(OpenItemOut(person_id=p["id"], person_name=pname, issue="no relationships defined"))

    return items


# ---------------------------------------------------------------------------
# Chat builder
# ---------------------------------------------------------------------------

@router.post("/{family_id}/chat")
async def chat_builder(family_id: UUID, body: ChatMessageIn) -> ChatResponseOut:
    """Conversational family tree builder via LLM."""
    from server.family.llm_client import chat as llm_chat

    fam = await fdb.get_family(str(family_id))
    if fam is None:
        raise HTTPException(404, "Family not found")

    # Get current state
    people = await fdb.list_people(str(family_id))
    rels = await fdb.list_relationships(str(family_id))

    # Get chat session + history
    session = await fdb.get_or_create_chat_session(str(family_id))
    raw_messages = session["messages"] if session["messages"] else []
    # Ensure each entry is a dict (guard against double-encoded strings)
    history = []
    for msg in raw_messages:
        if isinstance(msg, dict):
            history.append(msg)
        elif isinstance(msg, str):
            import json as _json
            try:
                parsed = _json.loads(msg)
                if isinstance(parsed, dict):
                    history.append(parsed)
            except (ValueError, TypeError):
                pass

    # Call LLM
    people_dicts = [dict(p) for p in people]
    rels_dicts = [dict(r) for r in rels]
    result = await llm_chat(body.message, people_dicts, rels_dicts, history)

    # Apply patches
    patches_applied = 0
    for patch in result.get("patches", []):
        try:
            applied = await _apply_patch(str(family_id), patch)
            if applied:
                patches_applied += 1
        except Exception as exc:
            logger.warning("Failed to apply patch %s: %s", patch, exc)

    # Save chat messages
    await fdb.append_chat_message(str(session["id"]), "user", body.message)
    await fdb.append_chat_message(str(session["id"]), "assistant", result["reply"])

    # Return updated tree
    tree = await _build_tree(str(family_id))

    return ChatResponseOut(
        reply=result["reply"],
        patches_applied=patches_applied,
        tree=tree,
    )


@router.get("/{family_id}/chat/history")
async def get_chat_history(family_id: UUID) -> ChatHistoryOut:
    """Get chat history for a family."""
    row = await fdb.get_chat_history(str(family_id))
    if row is None:
        raise HTTPException(404, "No chat history found")
    # Normalise messages: handle double-encoded strings from earlier bug
    raw = row["messages"] or []
    messages = []
    for item in raw:
        if isinstance(item, dict):
            messages.append(item)
        elif isinstance(item, str):
            import json as _json
            try:
                parsed = _json.loads(item)
                if isinstance(parsed, dict):
                    messages.append(parsed)
                elif isinstance(parsed, list):
                    messages.extend(m for m in parsed if isinstance(m, dict))
            except (ValueError, TypeError):
                pass
    return ChatHistoryOut(
        session_id=row["id"],
        messages=messages,
    )


async def _apply_patch(family_id: str, patch: dict) -> bool:
    """Apply a single LLM patch to the database. Returns True if applied."""
    op = patch.get("op")

    if op == "add_person":
        name = patch.get("name")
        if not name:
            return False
        # Check if person already exists
        existing = await fdb.get_person_by_name(family_id, name)
        if existing:
            logger.info("Person %s already exists, skipping add", name)
            return False
        await fdb.create_person(
            family_id=family_id,
            name=name,
            nickname=patch.get("nickname"),
            maiden_name=patch.get("maiden_name"),
            born=patch.get("born"),
            status=patch.get("status", "living"),
            gender=patch.get("gender"),
            notes=patch.get("notes"),
            player=patch.get("player", False),
            placeholder=patch.get("placeholder", False),
        )
        return True

    elif op == "update_person":
        name = patch.get("name")
        if not name:
            return False
        person = await fdb.find_person_fuzzy(family_id, name)
        if not person:
            logger.warning("Cannot find person '%s' for update", name)
            return False
        fields = patch.get("fields", {})
        if not fields:
            return False
        await fdb.update_person(str(person["id"]), **fields)
        return True

    elif op == "add_relationship":
        rel_type = patch.get("type")
        from_name = patch.get("from_name")
        to_name = patch.get("to_name")
        if not rel_type or not from_name or not to_name:
            return False
        if rel_type not in ("married", "parent_of", "divorced"):
            return False

        from_person = await fdb.find_person_fuzzy(family_id, from_name)
        to_person = await fdb.find_person_fuzzy(family_id, to_name)
        if not from_person or not to_person:
            logger.warning(
                "Cannot resolve names for relationship: %s -> %s", from_name, to_name
            )
            return False

        await fdb.create_relationship(
            family_id=family_id,
            rel_type=rel_type,
            from_id=str(from_person["id"]),
            to_id=str(to_person["id"]),
            year=patch.get("year"),
            notes=patch.get("notes"),
        )
        return True

    else:
        logger.warning("Unknown patch op: %s", op)
        return False


# ---------------------------------------------------------------------------
# Deck generation
# ---------------------------------------------------------------------------

@router.post("/{family_id}/generate/{player_id}")
async def generate_decks(family_id: UUID, player_id: UUID, body: GenerateDeckIn | None = None) -> GenerateDeckOut:
    """Generate flashcard and/or trivia decks for a player."""
    from server.family.generator import generate_decks as gen

    fam = await fdb.get_family(str(family_id))
    if fam is None:
        raise HTTPException(404, "Family not found")

    people = await fdb.list_people(str(family_id))
    rels = await fdb.list_relationships(str(family_id))

    # Verify player exists and is marked as player
    player = None
    for p in people:
        if str(p["id"]) == str(player_id):
            player = p
            break
    if player is None:
        raise HTTPException(404, "Player not found in this family")
    if not player["player"]:
        raise HTTPException(400, "Person is not marked as a player")

    kinds = body.kinds if body else ["flashcard", "trivia"]
    pool = get_pool()

    people_dicts = [dict(p) for p in people]
    rels_dicts = [dict(r) for r in rels]

    deck_ids, total_cards = await gen(
        pool=pool,
        family_id=str(family_id),
        player_id=str(player_id),
        people=people_dicts,
        relationships=rels_dicts,
        kinds=kinds,
    )

    return GenerateDeckOut(
        deck_ids=deck_ids,
        cards_created=total_cards,
        player_name=player["name"],
    )


@router.get("/{family_id}/deck/{player_id}")
async def get_generated_decks(family_id: UUID, player_id: UUID) -> list[dict]:
    """Get deck IDs previously generated for a player."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, title, kind, card_count, created_at FROM decks "
        "WHERE properties->>'family_id' = $1 AND properties->>'player_id' = $2 "
        "ORDER BY created_at DESC",
        str(family_id), str(player_id),
    )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "kind": r["kind"],
            "card_count": r["card_count"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
