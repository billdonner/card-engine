#!/usr/bin/env python3
"""Direct-DB quality checks for trivia cards.

Replaces HTTP-based trivia-check for operations that 502 on large scans.

Usage:
    # Show per-category stats
    python scripts/quality_check.py stats

    # Scan for exact duplicate questions
    python scripts/quality_check.py dedup [--delete]

    # Scan for answer-in-question leaks
    python scripts/quality_check.py aiq [--delete]

    # Run all checks (stats + dedup + aiq)
    python scripts/quality_check.py all

Requires CE_DATABASE_HOST/PORT/USER/PASSWORD/NAME or CE_DATABASE_URL env vars.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg


async def get_pool() -> asyncpg.Pool:
    """Connect using the same env-var pattern as bulk_generate.py."""

    async def _init_conn(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    db_host = os.environ.get("CE_DATABASE_HOST", "")
    if db_host:
        use_ssl = os.environ.get("CE_DATABASE_SSL", "false").lower() in ("true", "1", "yes")
        return await asyncpg.create_pool(
            host=db_host,
            port=int(os.environ.get("CE_DATABASE_PORT", "5432")),
            user=os.environ.get("CE_DATABASE_USER", ""),
            password=os.environ.get("CE_DATABASE_PASSWORD", ""),
            database=os.environ.get("CE_DATABASE_NAME", "card_engine"),
            ssl="require" if use_ssl else False,
            init=_init_conn,
        )
    db_url = os.environ.get("CE_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not db_url:
        db_url = "postgresql://billdonner@localhost:5432/card_engine"
    return await asyncpg.create_pool(db_url, init=_init_conn)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def cmd_stats(pool: asyncpg.Pool):
    """Print per-category question counts."""
    rows = await pool.fetch("""
        SELECT d.title AS category, d.card_count,
               COUNT(c.id) AS actual_count,
               COUNT(CASE WHEN c.difficulty = 'easy' THEN 1 END) AS easy,
               COUNT(CASE WHEN c.difficulty = 'medium' THEN 1 END) AS medium,
               COUNT(CASE WHEN c.difficulty = 'hard' THEN 1 END) AS hard
        FROM decks d
        LEFT JOIN cards c ON c.deck_id = d.id
        WHERE d.kind = 'trivia'
        GROUP BY d.id, d.title, d.card_count
        ORDER BY d.card_count DESC
    """)

    total_cards = 0
    print(f"\n{'Category':<30} {'Count':>6} {'Easy':>6} {'Med':>6} {'Hard':>6}")
    print("-" * 60)
    for r in rows:
        count = r["actual_count"]
        total_cards += count
        print(f"{r['category']:<30} {count:>6} {r['easy']:>6} {r['medium']:>6} {r['hard']:>6}")
    print("-" * 60)
    print(f"{'TOTAL':<30} {total_cards:>6}")
    print(f"\n{len(rows)} categories, {total_cards} total trivia cards")


# ---------------------------------------------------------------------------
# Dedup scan
# ---------------------------------------------------------------------------

async def cmd_dedup(pool: asyncpg.Pool, delete: bool):
    """Find exact duplicate questions (same question text, case-insensitive)."""
    dupes = await pool.fetch("""
        SELECT c1.id AS dupe_id, c1.question, d.title AS category, c1.created_at,
               c2.id AS original_id, c2.created_at AS original_created
        FROM cards c1
        JOIN cards c2 ON LOWER(TRIM(c1.question)) = LOWER(TRIM(c2.question))
                     AND c1.id != c2.id
                     AND c1.created_at > c2.created_at
        JOIN decks d ON c1.deck_id = d.id
        WHERE d.kind = 'trivia'
        ORDER BY d.title, c1.created_at
    """)

    if not dupes:
        print("\nNo exact duplicates found.")
        return

    print(f"\nFound {len(dupes)} exact duplicates:")
    print("-" * 70)
    for r in dupes:
        print(f"  [{r['category']}] {r['question'][:80]}")
        print(f"    dupe {r['dupe_id']} (keep original {r['original_id']})")

    if delete:
        dupe_ids = [r["dupe_id"] for r in dupes]
        result = await pool.execute(
            "DELETE FROM cards WHERE id = ANY($1::uuid[])", dupe_ids
        )
        print(f"\nDeleted {len(dupe_ids)} duplicate cards. ({result})")
    else:
        print(f"\nRun with --delete to remove these {len(dupes)} duplicates.")


# ---------------------------------------------------------------------------
# Answer-in-question scan
# ---------------------------------------------------------------------------

async def cmd_aiq(pool: asyncpg.Pool, delete: bool):
    """Find trivia cards where the correct answer appears in the question text."""
    rows = await pool.fetch("""
        SELECT c.id, c.question, c.properties, d.title AS category
        FROM cards c
        JOIN decks d ON c.deck_id = d.id
        WHERE d.kind = 'trivia'
    """)

    leaks = []
    for r in rows:
        props = r["properties"]
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index")
        if correct_idx is None or correct_idx >= len(choices):
            continue

        correct_choice = choices[correct_idx]
        # Handle both {"text": "..."} and plain string formats
        answer = correct_choice.get("text", correct_choice) if isinstance(correct_choice, dict) else str(correct_choice)

        if not answer or len(answer) < 3:
            continue

        # Case-insensitive check: does the answer appear verbatim in the question?
        if answer.lower() in r["question"].lower():
            leaks.append({
                "id": r["id"],
                "category": r["category"],
                "question": r["question"],
                "answer": answer,
            })

    if not leaks:
        print("\nNo answer-in-question leaks found.")
        return

    print(f"\nFound {len(leaks)} answer-in-question leaks:")
    print("-" * 70)
    for leak in leaks:
        print(f"  [{leak['category']}] Q: {leak['question'][:70]}")
        print(f"    A: {leak['answer']}")

    if delete:
        leak_ids = [leak["id"] for leak in leaks]
        result = await pool.execute(
            "DELETE FROM cards WHERE id = ANY($1::uuid[])", leak_ids
        )
        print(f"\nDeleted {len(leak_ids)} leaked cards. ({result})")
    else:
        print(f"\nRun with --delete to remove these {len(leaks)} leaked cards.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Direct-DB quality checks for trivia cards")
    parser.add_argument("command", choices=["stats", "dedup", "aiq", "all"], help="Check to run")
    parser.add_argument("--delete", action="store_true", help="Delete bad cards (dedup/aiq)")
    args = parser.parse_args()

    pool = await get_pool()

    try:
        if args.command in ("stats", "all"):
            await cmd_stats(pool)
        if args.command in ("dedup", "all"):
            await cmd_dedup(pool, args.delete)
        if args.command in ("aiq", "all"):
            await cmd_aiq(pool, args.delete)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
