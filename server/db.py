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


async def get_all_decks_with_cards(kind: str) -> list[asyncpg.Record]:
    """Bulk-fetch all decks of a given kind with their cards via LEFT JOIN.

    Returns rows with both deck and card columns; caller groups by deck.
    """
    p = get_pool()
    return await p.fetch(
        "SELECT d.id AS deck_id, d.title, d.kind, d.properties AS deck_props, "
        "       d.card_count, d.created_at AS deck_created, "
        "       c.id AS card_id, c.position, c.question, "
        "       c.properties AS card_props, c.difficulty, "
        "       c.source_url, c.source_date "
        "FROM decks d "
        "LEFT JOIN cards c ON c.deck_id = d.id "
        "WHERE d.kind = $1::deck_kind "
        "ORDER BY d.created_at DESC, c.position",
        kind,
    )


async def get_categories_with_counts() -> list[asyncpg.Record]:
    """Get trivia categories (deck titles) with card counts and pic."""
    p = get_pool()
    return await p.fetch(
        "SELECT title, properties->>'pic' AS pic, card_count "
        "FROM decks WHERE kind = 'trivia' ORDER BY title"
    )


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
