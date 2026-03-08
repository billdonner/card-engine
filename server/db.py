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


async def get_all_decks_with_cards(
    kind: str, tier: str | None = None, categories: list[str] | None = None
) -> list[asyncpg.Record]:
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
    idx = 2
    if tier:
        sql += f"  AND d.tier = ${idx}::deck_tier "
        params.append(tier)
        idx += 1
    if categories:
        sql += f"  AND d.title = ANY(${idx}::text[]) "
        params.append(categories)
        idx += 1
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


async def create_deck_with_cards(
    title: str, kind: str, properties: dict, cards: list[dict]
) -> tuple[asyncpg.Record, list[asyncpg.Record]]:
    """Create a deck with all its cards in a single transaction.

    cards: list of dicts with keys: question, properties, difficulty
    Returns (deck_row, card_rows).
    """
    p = get_pool()
    deck_id = uuid.uuid4()
    props = {**properties, "status": properties.get("status", "draft")}

    async with p.acquire() as conn:
        async with conn.transaction():
            deck_row = await conn.fetchrow(
                "INSERT INTO decks (id, title, kind, properties) "
                "VALUES ($1, $2, $3::deck_kind, $4) "
                "RETURNING id, title, kind, properties, card_count, created_at",
                deck_id, title, kind, props,
            )
            card_rows = []
            for pos, card in enumerate(cards):
                card_id = uuid.uuid4()
                row = await conn.fetchrow(
                    "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
                    "VALUES ($1, $2, $3, $4, $5, $6::difficulty) "
                    "RETURNING id, deck_id, position, question, properties, difficulty, source_url, source_date",
                    card_id, deck_id, pos, card["question"], card.get("properties", {}),
                    card.get("difficulty", "medium"),
                )
                card_rows.append(row)
    return deck_row, card_rows


async def deck_stats() -> dict:
    """Deck/card statistics by kind and age range."""
    p = get_pool()
    by_kind = await p.fetch(
        "SELECT kind::text, COUNT(*)::int AS deck_count, "
        "COALESCE(SUM(card_count), 0)::int AS card_count "
        "FROM decks GROUP BY kind ORDER BY kind"
    )
    by_age = await p.fetch(
        "SELECT COALESCE(properties->>'age_range', 'unset') AS age_range, COUNT(*)::int AS deck_count "
        "FROM decks GROUP BY 1 ORDER BY 1"
    )
    total_decks = sum(r["deck_count"] for r in by_kind)
    total_cards = sum(r["card_count"] for r in by_kind)
    return {
        "total_decks": total_decks,
        "total_cards": total_cards,
        "by_kind": [{"kind": r["kind"], "decks": r["deck_count"], "cards": r["card_count"]} for r in by_kind],
        "by_age_range": [{"age_range": r["age_range"], "decks": r["deck_count"]} for r in by_age],
    }


async def find_deck_by_title(title: str, kind: str) -> asyncpg.Record | None:
    """Find a deck by exact title (case-insensitive) and kind."""
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, title, kind, card_count, created_at FROM decks "
        "WHERE LOWER(title) = LOWER($1) AND kind = $2::deck_kind LIMIT 1",
        title, kind,
    )


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


async def insert_question_report(data: dict) -> asyncpg.Record:
    """Insert a question report and return the new row."""
    p = get_pool()
    return await p.fetchrow(
        "INSERT INTO question_reports (app_id, challenge_id, topic, question_text, reason, detail) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "RETURNING id, app_id, challenge_id, reported_at",
        data["app_id"],
        data["challenge_id"],
        data.get("topic"),
        data["question_text"],
        data.get("reason", "inaccurate"),
        data.get("detail"),
    )


async def list_question_reports(
    app_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[asyncpg.Record], int]:
    """List question reports with optional app_id filter."""
    p = get_pool()
    conditions: list[str] = []
    params: list = []
    idx = 1

    if app_id:
        conditions.append(f"app_id = ${idx}")
        params.append(app_id)
        idx += 1

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    total = await p.fetchval(f"SELECT COUNT(*) FROM question_reports{where}", *params)

    params.extend([limit, offset])
    rows = await p.fetch(
        f"SELECT id, app_id, challenge_id, reported_at "
        f"FROM question_reports{where} ORDER BY reported_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return rows, total


async def get_report_count() -> int:
    """Total number of question reports."""
    p = get_pool()
    return await p.fetchval("SELECT COUNT(*) FROM question_reports")


# ---------------------------------------------------------------------------
# Player & session helpers
# ---------------------------------------------------------------------------

import random
import string


async def upsert_player(device_id: str, display_name: str | None = None, properties: dict | None = None) -> tuple[asyncpg.Record, bool]:
    """Insert or update a player by device_id. Returns (row, is_new)."""
    p = get_pool()
    props = properties or {}
    # Try insert first
    existing = await p.fetchrow("SELECT id FROM players WHERE device_id = $1", device_id)
    if existing:
        row = await p.fetchrow(
            "UPDATE players SET last_seen_at = now(), "
            "display_name = COALESCE($2, display_name) "
            "WHERE device_id = $1 "
            "RETURNING id, device_id, display_name, created_at, last_seen_at",
            device_id, display_name,
        )
        return row, False
    else:
        player_id = uuid.uuid4()
        row = await p.fetchrow(
            "INSERT INTO players (id, device_id, display_name, properties) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, device_id, display_name, created_at, last_seen_at",
            player_id, device_id, display_name, props,
        )
        return row, True


async def get_player(player_id: uuid.UUID) -> asyncpg.Record | None:
    """Get a player by UUID."""
    p = get_pool()
    return await p.fetchrow(
        "SELECT id, device_id, display_name, created_at, last_seen_at "
        "FROM players WHERE id = $1",
        player_id,
    )


async def get_player_seen_card_ids(player_id: uuid.UUID, app_id: str = "qross") -> set[uuid.UUID]:
    """Return the set of card UUIDs this player has already been served."""
    p = get_pool()
    rows = await p.fetch(
        "SELECT card_id FROM player_card_history WHERE player_id = $1 AND app_id = $2",
        player_id, app_id,
    )
    return {r["card_id"] for r in rows}


async def record_seen_cards(player_id: uuid.UUID, card_ids: list[uuid.UUID], app_id: str = "qross") -> None:
    """Record that a player has been served these cards (ON CONFLICT ignore)."""
    if not card_ids:
        return
    p = get_pool()
    async with p.acquire() as conn:
        await conn.executemany(
            "INSERT INTO player_card_history (player_id, card_id, app_id) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            [(player_id, cid, app_id) for cid in card_ids],
        )


async def clear_player_history(player_id: uuid.UUID, app_id: str | None = None) -> int:
    """Clear a player's seen-card history. Returns count deleted."""
    p = get_pool()
    if app_id:
        result = await p.execute(
            "DELETE FROM player_card_history WHERE player_id = $1 AND app_id = $2",
            player_id, app_id,
        )
    else:
        result = await p.execute(
            "DELETE FROM player_card_history WHERE player_id = $1",
            player_id,
        )
    # result is like "DELETE 50"
    return int(result.split()[-1])


async def get_player_stats(player_id: uuid.UUID, app_id: str = "qross") -> dict:
    """Get seen-card stats for a player: total + per-category breakdown."""
    p = get_pool()
    total = await p.fetchval(
        "SELECT COUNT(*) FROM player_card_history WHERE player_id = $1 AND app_id = $2",
        player_id, app_id,
    )
    rows = await p.fetch(
        "SELECT d.title AS category, COUNT(*) AS cnt "
        "FROM player_card_history pch "
        "JOIN cards c ON c.id = pch.card_id "
        "JOIN decks d ON d.id = c.deck_id "
        "WHERE pch.player_id = $1 AND pch.app_id = $2 "
        "GROUP BY d.title ORDER BY d.title",
        player_id, app_id,
    )
    return {
        "total_seen": total,
        "by_category": {r["category"]: r["cnt"] for r in rows},
    }


def _generate_share_code(length: int = 6) -> str:
    """Generate a random uppercase alphanumeric share code."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


async def create_session(
    player_id: uuid.UUID,
    card_ids: list[uuid.UUID],
    app_id: str = "qross",
    properties: dict | None = None,
) -> tuple[uuid.UUID, str]:
    """Create a session with a unique share code and ordered card list.
    Returns (session_id, share_code).
    """
    p = get_pool()
    session_id = uuid.uuid4()
    props = properties or {}

    # Retry loop for share code collisions
    for _ in range(10):
        share_code = _generate_share_code()
        try:
            async with p.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO sessions (id, player_id, share_code, app_id, properties) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        session_id, player_id, share_code, app_id, props,
                    )
                    if card_ids:
                        await conn.executemany(
                            "INSERT INTO session_cards (session_id, card_id, position) "
                            "VALUES ($1, $2, $3)",
                            [(session_id, cid, pos) for pos, cid in enumerate(card_ids)],
                        )
            return session_id, share_code
        except asyncpg.UniqueViolationError:
            # Share code collision — retry with new code
            session_id = uuid.uuid4()
            continue
    raise RuntimeError("Failed to generate unique share code after 10 attempts")


async def get_session_by_share_code(share_code: str) -> tuple[asyncpg.Record | None, list[asyncpg.Record]]:
    """Look up a session by share code. Returns (session_row, card_rows).
    Card rows include full card + deck info for building challenges.
    """
    p = get_pool()
    session = await p.fetchrow(
        "SELECT id, player_id, share_code, app_id, properties, created_at FROM sessions WHERE share_code = $1",
        share_code.upper(),
    )
    if session is None:
        return None, []

    rows = await p.fetch(
        "SELECT sc.position, "
        "       d.id AS deck_id, d.title, d.kind, d.properties AS deck_props, "
        "       d.card_count, d.created_at AS deck_created, "
        "       c.id AS card_id, c.position AS card_position, c.question, "
        "       c.properties AS card_props, c.difficulty, "
        "       c.source_url, c.source_date "
        "FROM session_cards sc "
        "JOIN cards c ON c.id = sc.card_id "
        "JOIN decks d ON d.id = c.deck_id "
        "WHERE sc.session_id = $1 "
        "ORDER BY sc.position",
        session["id"],
    )
    return session, rows


async def update_session_properties(session_id: uuid.UUID, properties: dict) -> asyncpg.Record | None:
    """Merge properties into an existing session's JSONB column."""
    p = get_pool()
    return await p.fetchrow(
        "UPDATE sessions SET properties = properties || $2::jsonb "
        "WHERE id = $1 RETURNING id, properties",
        session_id, json.dumps(properties),
    )


async def get_player_count() -> int:
    """Total number of registered players."""
    p = get_pool()
    return await p.fetchval("SELECT COUNT(*) FROM players")


async def get_session_count() -> int:
    """Total number of sessions."""
    p = get_pool()
    return await p.fetchval("SELECT COUNT(*) FROM sessions")


async def get_card_view_count() -> int:
    """Total number of card views (player_card_history rows)."""
    p = get_pool()
    return await p.fetchval("SELECT COUNT(*) FROM player_card_history")


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
