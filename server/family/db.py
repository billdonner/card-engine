"""Database query helpers for family tree tables."""

from __future__ import annotations

import random
import string
import uuid

import asyncpg
from asyncpg import UniqueViolationError

from server.db import get_pool


def _generate_invite_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

async def create_family(name: str) -> asyncpg.Record:
    p = get_pool()
    fid = uuid.uuid4()
    return await p.fetchrow(
        "INSERT INTO families (id, name) VALUES ($1, $2) "
        "RETURNING id, name, created_at, updated_at",
        fid, name,
    )


async def get_family(family_id: str) -> asyncpg.Record | None:
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, name, created_at, updated_at FROM families WHERE id = $1",
        family_id,
    )


async def list_families() -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, name, created_at, updated_at FROM families ORDER BY created_at DESC"
    )


async def delete_family(family_id: str) -> bool:
    p = get_pool()
    result = await p.execute("DELETE FROM families WHERE id = $1", family_id)
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

async def create_person(
    family_id: str,
    name: str,
    nickname: str | None = None,
    maiden_name: str | None = None,
    born: int | None = None,
    status: str = "living",
    gender: str | None = None,
    notes: str | None = None,
    player: bool = False,
    placeholder: bool = False,
    photo_url: str | None = None,
) -> asyncpg.Record:
    p = get_pool()
    pid = uuid.uuid4()
    return await p.fetchrow(
        "INSERT INTO family_people "
        "(id, family_id, name, nickname, maiden_name, born, status, gender, notes, player, placeholder, photo_url) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::person_status, $8, $9, $10, $11, $12) "
        "RETURNING id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
        "player, placeholder, photo_url, created_at, updated_at",
        pid, family_id, name, nickname, maiden_name, born, status, gender, notes,
        player, placeholder, photo_url,
    )


async def update_person(person_id: str, **kwargs) -> asyncpg.Record | None:
    p = get_pool()
    allowed = {
        "name", "nickname", "maiden_name", "born", "status", "gender", "notes",
        "player", "placeholder", "photo_url",
    }
    sets: list[str] = []
    params: list = []
    idx = 1

    for key, val in kwargs.items():
        if key not in allowed or val is None:
            continue
        if key == "status":
            sets.append(f"status = ${idx}::person_status")
        else:
            sets.append(f"{key} = ${idx}")
        params.append(val)
        idx += 1

    if not sets:
        return await p.fetchrow(
            "SELECT id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
            "player, placeholder, photo_url, created_at, updated_at "
            "FROM family_people WHERE id = $1",
            person_id,
        )

    params.append(person_id)
    sql = (
        f"UPDATE family_people SET {', '.join(sets)} "
        f"WHERE id = ${idx} "
        f"RETURNING id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
        f"player, placeholder, photo_url, created_at, updated_at"
    )
    return await p.fetchrow(sql, *params)


async def delete_person(person_id: str) -> bool:
    p = get_pool()
    result = await p.execute("DELETE FROM family_people WHERE id = $1", person_id)
    return result == "DELETE 1"


async def list_people(family_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
        "player, placeholder, photo_url, created_at, updated_at "
        "FROM family_people WHERE family_id = $1 ORDER BY name",
        family_id,
    )


async def get_person_by_name(family_id: str, name: str) -> asyncpg.Record | None:
    """Fuzzy name lookup — case-insensitive prefix match."""
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
        "player, placeholder, photo_url, created_at, updated_at "
        "FROM family_people WHERE family_id = $1 AND LOWER(name) = LOWER($2)",
        family_id, name,
    )


async def find_person_fuzzy(family_id: str, name: str) -> asyncpg.Record | None:
    """Try exact match first, then case-insensitive LIKE."""
    row = await get_person_by_name(family_id, name)
    if row:
        return row
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, family_id, name, nickname, maiden_name, born, status, gender, notes, "
        "player, placeholder, photo_url, created_at, updated_at "
        "FROM family_people WHERE family_id = $1 AND LOWER(name) LIKE '%' || LOWER($2) || '%' "
        "LIMIT 1",
        family_id, name,
    )


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

async def create_relationship(
    family_id: str,
    rel_type: str,
    from_id: str,
    to_id: str,
    year: int | None = None,
    ended: bool = False,
    end_reason: str | None = None,
    notes: str | None = None,
) -> asyncpg.Record:
    p = get_pool()
    rid = uuid.uuid4()
    return await p.fetchrow(
        "INSERT INTO family_relationships "
        "(id, family_id, type, from_id, to_id, year, ended, end_reason, notes) "
        "VALUES ($1, $2, $3::relationship_type, $4, $5, $6, $7, $8, $9) "
        "RETURNING id, family_id, type, from_id, to_id, year, ended, end_reason, notes, created_at",
        rid, family_id, rel_type, from_id, to_id, year, ended, end_reason, notes,
    )


async def delete_relationship(rel_id: str) -> bool:
    p = get_pool()
    result = await p.execute("DELETE FROM family_relationships WHERE id = $1", rel_id)
    return result == "DELETE 1"


async def list_relationships(family_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, family_id, type, from_id, to_id, year, ended, end_reason, notes, created_at "
        "FROM family_relationships WHERE family_id = $1 ORDER BY created_at",
        family_id,
    )


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

async def get_or_create_chat_session(family_id: str) -> asyncpg.Record:
    """Return the most recent chat session, or create one."""
    p = get_pool()
    row = await p.fetchrow(
        "SELECT id, family_id, messages, created_at, updated_at "
        "FROM family_chat_sessions WHERE family_id = $1 ORDER BY updated_at DESC LIMIT 1",
        family_id,
    )
    if row:
        return row
    sid = uuid.uuid4()
    return await p.fetchrow(
        "INSERT INTO family_chat_sessions (id, family_id, messages) VALUES ($1, $2, '[]'::jsonb) "
        "RETURNING id, family_id, messages, created_at, updated_at",
        sid, family_id,
    )


async def append_chat_message(session_id: str, role: str, content: str) -> None:
    """Append a message to the JSONB messages array."""
    p = get_pool()
    # Use jsonb_build_object + array append to avoid asyncpg double-encoding
    await p.execute(
        "UPDATE family_chat_sessions "
        "SET messages = messages || jsonb_build_array(jsonb_build_object('role', $1::text, 'content', $2::text)), "
        "    updated_at = now() "
        "WHERE id = $3",
        role, content, session_id,
    )


async def get_chat_history(family_id: str) -> asyncpg.Record | None:
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, family_id, messages, created_at, updated_at "
        "FROM family_chat_sessions WHERE family_id = $1 ORDER BY updated_at DESC LIMIT 1",
        family_id,
    )


# ---------------------------------------------------------------------------
# Family membership
# ---------------------------------------------------------------------------

async def add_family_member(family_id: str, player_id: str, role: str = "member") -> asyncpg.Record:
    p = get_pool()
    return await p.fetchrow(
        "INSERT INTO family_members (family_id, player_id, role) VALUES ($1, $2, $3) "
        "ON CONFLICT (family_id, player_id) DO UPDATE SET role = EXCLUDED.role "
        "RETURNING family_id, player_id, role, created_at",
        family_id, player_id, role,
    )


async def remove_family_member(family_id: str, player_id: str) -> bool:
    p = get_pool()
    result = await p.execute(
        "DELETE FROM family_members WHERE family_id = $1 AND player_id = $2",
        family_id, player_id,
    )
    return result == "DELETE 1"


async def is_family_member(family_id: str, player_id: str) -> bool:
    p = get_pool()
    row = await p.fetchrow(
        "SELECT 1 FROM family_members WHERE family_id = $1 AND player_id = $2",
        family_id, player_id,
    )
    return row is not None


async def get_family_role(family_id: str, player_id: str) -> str | None:
    p = get_pool()
    row = await p.fetchrow(
        "SELECT role FROM family_members WHERE family_id = $1 AND player_id = $2",
        family_id, player_id,
    )
    return row["role"] if row else None


async def list_family_members(family_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT family_id, player_id, role, created_at FROM family_members "
        "WHERE family_id = $1 ORDER BY created_at",
        family_id,
    )


async def list_player_families(player_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT f.id, f.name, f.created_at, f.updated_at "
        "FROM families f "
        "JOIN family_members m ON m.family_id = f.id "
        "WHERE m.player_id = $1 "
        "ORDER BY f.created_at DESC",
        player_id,
    )


# ---------------------------------------------------------------------------
# Family invites
# ---------------------------------------------------------------------------

async def create_family_invite(family_id: str, player_id: str) -> asyncpg.Record:
    p = get_pool()
    invite_id = uuid.uuid4()
    for _ in range(10):
        code = _generate_invite_code()
        try:
            return await p.fetchrow(
                "INSERT INTO family_invites (id, family_id, invite_code, created_by) "
                "VALUES ($1, $2, $3, $4) "
                "RETURNING id, family_id, invite_code, created_by, created_at",
                invite_id, family_id, code, player_id,
            )
        except UniqueViolationError:
            invite_id = uuid.uuid4()
            continue
    raise RuntimeError("Failed to generate unique invite code after 10 attempts")


async def get_invite(invite_code: str) -> asyncpg.Record | None:
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, family_id, invite_code, created_by, created_at "
        "FROM family_invites WHERE invite_code = $1",
        invite_code,
    )


async def redeem_invite(invite_code: str, player_id: str) -> asyncpg.Record:
    """Add player as member of the invited family. Returns the family record."""
    invite = await get_invite(invite_code)
    if invite is None:
        raise ValueError("Invalid invite code")
    family_id = str(invite["family_id"])
    await add_family_member(family_id, player_id, role="member")
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, name, created_at, updated_at FROM families WHERE id = $1",
        family_id,
    )


async def list_family_invites(family_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, family_id, invite_code, created_by, created_at "
        "FROM family_invites WHERE family_id = $1 ORDER BY created_at DESC",
        family_id,
    )


async def delete_family_invite(invite_id: str) -> bool:
    p = get_pool()
    result = await p.execute("DELETE FROM family_invites WHERE id = $1", invite_id)
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Deck card browsing + exclusions
# ---------------------------------------------------------------------------

async def get_deck_cards(deck_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, deck_id, position, question, properties, difficulty, created_at "
        "FROM cards WHERE deck_id = $1 ORDER BY position",
        deck_id,
    )


async def add_exclusion(family_id: str, question: str) -> asyncpg.Record:
    p = get_pool()
    eid = uuid.uuid4()
    return await p.fetchrow(
        "INSERT INTO family_card_exclusions (id, family_id, question) VALUES ($1, $2, $3) "
        "ON CONFLICT (family_id, question) DO UPDATE SET excluded_at = now() "
        "RETURNING id, family_id, question, excluded_at",
        eid, family_id, question,
    )


async def remove_exclusion(exclusion_id: str) -> bool:
    p = get_pool()
    result = await p.execute("DELETE FROM family_card_exclusions WHERE id = $1", exclusion_id)
    return result == "DELETE 1"


async def list_exclusions(family_id: str) -> list[asyncpg.Record]:
    p = get_pool()
    return await p.fetch(
        "SELECT id, family_id, question, excluded_at FROM family_card_exclusions "
        "WHERE family_id = $1 ORDER BY excluded_at DESC",
        family_id,
    )


async def get_excluded_questions(family_id: str) -> set[str]:
    rows = await list_exclusions(family_id)
    return {r["question"] for r in rows}
