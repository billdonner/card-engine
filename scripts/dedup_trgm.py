"""Fast trigram dedup using pg_trgm GIN index.

Strategy: worker queue — N workers each loop through the question list,
running individual GIN similarity lookups. Simple, robust, no closure issues.

Usage:
    python scripts/dedup_trgm.py              # dry run
    python scripts/dedup_trgm.py --delete     # delete duplicates
    python scripts/dedup_trgm.py --verbose    # print each pair
    python scripts/dedup_trgm.py --category "Science & Nature"
    python scripts/dedup_trgm.py --threshold 0.70

Environment: CE_DATABASE_HOST, CE_DATABASE_PORT, CE_DATABASE_USER,
             CE_DATABASE_PASSWORD, CE_DATABASE_NAME
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _db_kwargs() -> dict:
    host     = os.environ.get("CE_DATABASE_HOST", "localhost")
    port_str = os.environ.get("CE_DATABASE_PORT", "5432")
    port     = int(port_str.split(":")[-1]) if ":" in port_str else int(port_str)
    user     = os.environ.get("CE_DATABASE_USER", "card_engine_user")
    password = os.environ.get("CE_DATABASE_PASSWORD", "")
    dbname   = os.environ.get("CE_DATABASE_NAME", "card_engine")
    return dict(host=host, port=port, user=user, password=password, database=dbname)


# ---------------------------------------------------------------------------
# Single-question similarity lookup — module-level to avoid closure issues
# ---------------------------------------------------------------------------

async def find_similar_older(conn, question: str, card_id: str, created_at, threshold: float) -> list[dict]:
    """Find older cards similar to question using GIN index (% operator)."""
    rows = await conn.fetch(
        """
        SELECT id::text, question, similarity($1, question) AS sim
        FROM cards
        WHERE id::text <> $2
          AND created_at <= $3
          AND $1 % question
        ORDER BY sim DESC
        LIMIT 3
        """,
        question, card_id, created_at,
        timeout=60,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Worker-based scanner
# ---------------------------------------------------------------------------

async def scan_worker(
    worker_id: int,
    queue: asyncio.Queue,
    pool: asyncpg.Pool,
    threshold: float,
    results: list,
    seen_ids: set,
    counters: dict,
):
    """Pull questions from queue, run GIN lookup, add dupes to results."""
    while True:
        try:
            q = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        if q["id"] in seen_ids:
            queue.task_done()
            counters["checked"] += 1
            continue

        try:
            async with pool.acquire() as conn:
                matches = await find_similar_older(
                    conn, q["question"], q["id"], q["created_at"], threshold
                )
        except Exception as e:
            logger.warning("Worker %d error for card %s: %s", worker_id, q["id"][:8], e)
            queue.task_done()
            counters["checked"] += 1
            continue

        if matches and q["id"] not in seen_ids:
            seen_ids.add(q["id"])
            best = matches[0]
            results.append({
                "newer_id": q["id"],
                "newer_q":  q["question"],
                "older_id": best["id"],
                "older_q":  best["question"],
                "sim":      best["sim"],
                "category": q["category"],
            })
            counters["found"] += 1

        counters["checked"] += 1
        queue.task_done()


async def find_all_duplicates(
    pool: asyncpg.Pool,
    questions: list[dict],
    threshold: float,
    concurrency: int = 12,
) -> list[dict]:
    """Scan all questions for duplicates using N concurrent workers."""
    queue: asyncio.Queue = asyncio.Queue()
    for q in questions:
        await queue.put(q)

    results: list[dict] = []
    seen_ids: set[str] = set()
    counters = {"checked": 0, "found": 0}
    total = len(questions)
    t_start = time.time()

    # Progress reporter task
    async def reporter():
        prev = 0
        while True:
            await asyncio.sleep(30)
            checked = counters["checked"]
            found   = counters["found"]
            elapsed = time.time() - t_start
            rate    = checked / elapsed if elapsed > 0 else 0
            eta     = (total - checked) / rate if rate > 0 else 0
            logger.info(
                "  %d/%d checked (+%d), %d dupes | %.0f q/s | ETA %.0f s",
                checked, total, checked - prev, found, rate, eta,
            )
            prev = checked
            if checked >= total:
                break

    reporter_task = asyncio.create_task(reporter())

    # Launch workers
    workers = [
        asyncio.create_task(
            scan_worker(i, queue, pool, threshold, results, seen_ids, counters)
        )
        for i in range(concurrency)
    ]

    await asyncio.gather(*workers)
    reporter_task.cancel()

    elapsed = time.time() - t_start
    logger.info(
        "Scan complete: %d/%d checked, %d dupes found in %.1f s (%.0f q/s)",
        counters["checked"], total, counters["found"], elapsed, total / elapsed,
    )
    return results


# ---------------------------------------------------------------------------
# Delete duplicates
# ---------------------------------------------------------------------------

async def deduplicate(pool: asyncpg.Pool, pairs: list[dict], delete: bool, verbose: bool) -> int:
    to_delete = list({p["newer_id"] for p in pairs})
    cat_counts: dict[str, int] = {}
    for p in pairs:
        cat_counts[p["category"]] = cat_counts.get(p["category"], 0) + 1
        if verbose:
            logger.info(
                "[sim=%.2f] [%s]\n  KEEP: %s\n  DROP: %s",
                p["sim"], p["category"], p["older_q"][:100], p["newer_q"][:100],
            )

    logger.info("\nDuplicate summary by category:")
    for cat, n in sorted(cat_counts.items()):
        logger.info("  %-32s  %d", cat, n)
    logger.info("  %-32s  %d total", "", len(to_delete))

    if not to_delete:
        logger.info("No duplicates found.")
        return 0

    if not delete:
        logger.info("Dry run — pass --delete to remove %d duplicates.", len(to_delete))
        return 0

    deleted = 0
    for i in range(0, len(to_delete), 200):
        batch = to_delete[i : i + 200]
        result = await pool.execute("DELETE FROM cards WHERE id::text = ANY($1::text[])", batch)
        deleted += int(result.split()[-1])
        logger.info("Deleted %d/%d...", deleted, len(to_delete))

    logger.info("Done — deleted %d duplicate cards.", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Fast pg_trgm dedup for trivia cards")
    parser.add_argument("--delete",      action="store_true", help="Delete duplicates (default: dry run)")
    parser.add_argument("--verbose",     action="store_true", help="Print each pair")
    parser.add_argument("--category",    help="Limit to one category")
    parser.add_argument("--threshold",   type=float, default=0.65, help="Similarity threshold (default: 0.65)")
    parser.add_argument("--concurrency", type=int, default=12, help="Worker count (default: 12)")
    args = parser.parse_args()

    threshold = args.threshold

    async def init_conn(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("json",  encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.execute(f"SET pg_trgm.similarity_threshold = {threshold}")

    kw = _db_kwargs()
    pool = await asyncpg.create_pool(
        **kw, init=init_conn, min_size=1, max_size=args.concurrency + 2,
    )

    logger.info("Connected to %s:%d/%s", kw["host"], kw["port"], kw["database"])
    logger.info("threshold=%.2f  concurrency=%d  mode=%s",
                threshold, args.concurrency, "DELETE" if args.delete else "DRY RUN")
    if args.category:
        logger.info("category filter: %s", args.category)

    # Load questions
    if args.category:
        rows = await pool.fetch(
            "SELECT c.id::text, c.question, c.created_at, d.title AS category "
            "FROM cards c JOIN decks d ON d.id = c.deck_id "
            "WHERE d.kind = 'trivia' AND d.title = $1 ORDER BY c.created_at",
            args.category,
        )
    else:
        rows = await pool.fetch(
            "SELECT c.id::text, c.question, c.created_at, d.title AS category "
            "FROM cards c JOIN decks d ON d.id = c.deck_id "
            "WHERE d.kind = 'trivia' ORDER BY c.created_at",
        )
    questions = [dict(r) for r in rows]
    logger.info("Loaded %d trivia questions", len(questions))

    pairs = await find_all_duplicates(pool, questions, threshold, args.concurrency)
    await deduplicate(pool, pairs, args.delete, args.verbose)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
