"""Database pool management and query helpers for card-engine."""

from __future__ import annotations

import json
import os

import asyncpg

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DB_HOST = os.environ.get("CE_DB_HOST", "localhost")
_DB_PORT = os.environ.get("CE_DB_PORT", "5432")
_DB_USER = os.environ.get("CE_DB_USER", "postgres")
_DB_PASSWORD = os.environ.get("CE_DB_PASSWORD", "postgres")
_DB_NAME = os.environ.get("CE_DB_NAME", "card_engine")

DATABASE_URL = os.environ.get(
    "CE_DATABASE_URL",
    f"postgresql://{_DB_USER}:{_DB_PASSWORD}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}",
)

# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the global asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        init=_init_connection,
    )
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the pool, raising if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up JSON codec on each new connection."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def list_decks(
    kind: str | None = None,
    age: str | None = None,
    tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[asyncpg.Record], int]:
    """List decks with optional filters. Returns (rows, total_count)."""
    p = get_pool()
    conditions: list[str] = []
    params: list = []
    idx = 1

    if kind:
        conditions.append(f"kind = ${idx}::deck_kind")
        params.append(kind)
        idx += 1
    if age:
        conditions.append(f"properties->>'age_range' = ${idx}")
        params.append(age)
        idx += 1
    if tier:
        conditions.append(f"tier = ${idx}::deck_tier")
        params.append(tier)
        idx += 1

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    count_sql = f"SELECT COUNT(*) FROM decks{where}"
    total = await p.fetchval(count_sql, *params)

    params.extend([limit, offset])
    select_sql = (
        f"SELECT id, title, kind, properties, card_count, created_at "
        f"FROM decks{where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
    )
    rows = await p.fetch(select_sql, *params)
    return rows, total


async def get_deck(deck_id: str) -> tuple[asyncpg.Record | None, list[asyncpg.Record]]:
    """Get a single deck and its cards. Returns (deck_row, card_rows)."""
    p = get_pool()
    deck = await p.fetchrow(
        "SELECT id, title, kind, properties, card_count, created_at "
        "FROM decks WHERE id = $1",
        deck_id,
    )
    if deck is None:
        return None, []

    cards = await p.fetch(
        "SELECT id, position, question, properties, difficulty, source_url, source_date "
        "FROM cards WHERE deck_id = $1 ORDER BY position",
        deck_id,
    )
    return deck, cards


async def get_all_decks_with_cards(kind: str, tier: str | None = None) -> list[asyncpg.Record]:
    """Bulk-fetch all decks of a given kind with their cards via LEFT JOIN.

    Returns rows with both deck and card columns; caller groups by deck.
    """
    p = get_pool()
    sql = (
        "SELECT d.id AS deck_id, d.title, d.kind, d.properties AS deck_props, "
        "       d.card_count, d.created_at AS deck_created, "
        "       c.id AS card_id, c.position, c.question, "
        "       c.properties AS card_props, c.difficulty, "
        "       c.source_url, c.source_date "
        "FROM decks d "
        "LEFT JOIN cards c ON c.deck_id = d.id "
        "WHERE d.kind = $1::deck_kind "
        "  AND COALESCE(d.properties->>'status', 'published') = 'published' "
    )
    params: list = [kind]
    if tier:
        sql += "  AND d.tier = $2::deck_tier "
        params.append(tier)
    sql += "ORDER BY d.created_at DESC, c.position"
    return await p.fetch(sql, *params)


async def get_categories_with_counts(tier: str | None = None) -> list[asyncpg.Record]:
    """Get trivia categories (deck titles) with card counts and pic."""
    p = get_pool()
    sql = (
        "SELECT title, properties->>'pic' AS pic, card_count "
        "FROM decks WHERE kind = 'trivia' "
        "  AND COALESCE(properties->>'status', 'published') = 'published' "
    )
    params: list = []
    if tier:
        sql += "  AND tier = $1::deck_tier "
        params.append(tier)
    sql += "ORDER BY title"
    return await p.fetch(sql, *params)


async def get_stats() -> dict:
    """Aggregate stats for the metrics endpoint."""
    p = get_pool()
    total_decks = await p.fetchval("SELECT COUNT(*) FROM decks")
    total_cards = await p.fetchval("SELECT COUNT(*) FROM cards")
    total_sources = await p.fetchval("SELECT COUNT(*) FROM source_providers")

    kind_counts = await p.fetch(
        "SELECT kind::text, COUNT(*) AS cnt FROM decks GROUP BY kind ORDER BY kind"
    )

    return {
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_sources": total_sources,
        "decks_by_kind": {r["kind"]: r["cnt"] for r in kind_counts},
    }


# ---------------------------------------------------------------------------
# Studio CRUD helpers
# ---------------------------------------------------------------------------

import uuid


async def create_deck(title: str, kind: str, properties: dict) -> asyncpg.Record:
    """Create a new deck and return it. New decks default to draft status."""
    p = get_pool()
    deck_id = uuid.uuid4()
    props = {**properties, "status": properties.get("status", "draft")}
    return await p.fetchrow(
        "INSERT INTO decks (id, title, kind, properties) "
        "VALUES ($1, $2, $3::deck_kind, $4) "
        "RETURNING id, title, kind, properties, card_count, created_at",
        deck_id, title, kind, props,
    )


async def update_deck(deck_id: str, title: str | None, properties: dict | None) -> asyncpg.Record | None:
    """Update deck metadata. Returns updated row or None if not found."""
    p = get_pool()
    sets: list[str] = []
    params: list = []
    idx = 1

    if title is not None:
        sets.append(f"title = ${idx}")
        params.append(title)
        idx += 1
    if properties is not None:
        sets.append(f"properties = ${idx}")
        params.append(properties)
        idx += 1

    if not sets:
        return await p.fetchrow(
            "SELECT id, title, kind, properties, card_count, created_at FROM decks WHERE id = $1",
            deck_id,
        )

    params.append(deck_id)
    sql = (
        f"UPDATE decks SET {', '.join(sets)} "
        f"WHERE id = ${idx} "
        f"RETURNING id, title, kind, properties, card_count, created_at"
    )
    return await p.fetchrow(sql, *params)


async def delete_deck(deck_id: str) -> bool:
    """Delete a deck and its cards (cascade). Returns True if deleted."""
    p = get_pool()
    result = await p.execute("DELETE FROM decks WHERE id = $1", deck_id)
    return result == "DELETE 1"


async def create_card(deck_id: str, question: str, properties: dict, difficulty: str) -> asyncpg.Record:
    """Create a new card in the given deck."""
    p = get_pool()
    card_id = uuid.uuid4()
    # Get next position
    max_pos = await p.fetchval(
        "SELECT COALESCE(MAX(position), -1) FROM cards WHERE deck_id = $1", deck_id
    )
    position = max_pos + 1
    return await p.fetchrow(
        "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
        "VALUES ($1, $2, $3, $4, $5, $6::difficulty) "
        "RETURNING id, deck_id, position, question, properties, difficulty, source_url, source_date",
        card_id, deck_id, position, question, properties, difficulty,
    )


async def update_card(
    card_id: str, question: str | None, properties: dict | None, difficulty: str | None
) -> asyncpg.Record | None:
    """Update a card. Returns updated row or None if not found."""
    p = get_pool()
    sets: list[str] = []
    params: list = []
    idx = 1

    if question is not None:
        sets.append(f"question = ${idx}")
        params.append(question)
        idx += 1
    if properties is not None:
        sets.append(f"properties = ${idx}")
        params.append(properties)
        idx += 1
    if difficulty is not None:
        sets.append(f"difficulty = ${idx}::difficulty")
        params.append(difficulty)
        idx += 1

    if not sets:
        return await p.fetchrow(
            "SELECT id, deck_id, position, question, properties, difficulty, source_url, source_date "
            "FROM cards WHERE id = $1",
            card_id,
        )

    params.append(card_id)
    sql = (
        f"UPDATE cards SET {', '.join(sets)} "
        f"WHERE id = ${idx} "
        f"RETURNING id, deck_id, position, question, properties, difficulty, source_url, source_date"
    )
    return await p.fetchrow(sql, *params)


async def delete_card(card_id: str) -> bool:
    """Delete a card. Returns True if deleted."""
    p = get_pool()
    result = await p.execute("DELETE FROM cards WHERE id = $1", card_id)
    return result == "DELETE 1"


async def reorder_cards(deck_id: str, card_ids: list[str]) -> list[asyncpg.Record]:
    """Reorder cards in a deck by updating positions to match card_ids order."""
    p = get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            for pos, cid in enumerate(card_ids):
                await conn.execute(
                    "UPDATE cards SET position = $1 WHERE id = $2 AND deck_id = $3",
                    pos, cid, deck_id,
                )
    return await p.fetch(
        "SELECT id, deck_id, position, question, properties, difficulty, source_url, source_date "
        "FROM cards WHERE deck_id = $1 ORDER BY position",
        deck_id,
    )


async def search_cards(query: str, limit: int = 20) -> tuple[list[asyncpg.Record], int]:
    """Full-text search across card questions. Returns (rows, total)."""
    p = get_pool()
    tsquery = " & ".join(query.strip().split())

    count_sql = (
        "SELECT COUNT(*) FROM cards c JOIN decks d ON d.id = c.deck_id "
        "WHERE to_tsvector('english', c.question) @@ to_tsquery('english', $1)"
    )
    total = await p.fetchval(count_sql, tsquery)

    rows = await p.fetch(
        "SELECT c.id AS card_id, c.deck_id, d.title AS deck_title, d.kind::text AS deck_kind, "
        "       c.question, c.properties, "
        "       ts_rank(to_tsvector('english', c.question), to_tsquery('english', $1)) AS rank "
        "FROM cards c JOIN decks d ON d.id = c.deck_id "
        "WHERE to_tsvector('english', c.question) @@ to_tsquery('english', $1) "
        "ORDER BY rank DESC LIMIT $2",
        tsquery, limit,
    )
    return rows, total
