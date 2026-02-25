"""Database query helpers for family tree tables."""

from __future__ import annotations

import uuid

import asyncpg

from server.db import get_pool


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
            "SELECT id, family_id, name, nickname, maiden_name, born, status, notes, "
            "player, placeholder, photo_url, created_at, updated_at "
            "FROM family_people WHERE id = $1",
            person_id,
        )

    params.append(person_id)
    sql = (
        f"UPDATE family_people SET {', '.join(sets)} "
        f"WHERE id = ${idx} "
        f"RETURNING id, family_id, name, nickname, maiden_name, born, status, notes, "
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
