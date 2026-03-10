"""Fast local trigram dedup — runs on your machine, connects via flyctl proxy.

Implements pg_trgm similarity in Python (same algorithm as PostgreSQL).
Processes all 57k questions in one pass with an inverted trigram index.
Expected time: 2-5 minutes for 57k questions.

Usage:
    # Start proxy first:
    flyctl proxy 15433:5432 -a bd-postgres &

    # Dry run (find dupes, print summary):
    python scripts/dedup_local.py

    # Delete duplicates:
    python scripts/dedup_local.py --delete

    # Verbose (print every pair):
    python scripts/dedup_local.py --verbose

    # Different threshold:
    python scripts/dedup_local.py --threshold 0.70

Environment (defaults for local proxy):
    CE_DATABASE_HOST=localhost  CE_DATABASE_PORT=15433
    CE_DATABASE_USER=postgres   CE_DATABASE_NAME=card_engine
"""

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone


def _trigrams(s: str) -> set[str]:
    """Compute pg_trgm-compatible trigrams for a string."""
    # pg_trgm pads with spaces: 2 at start, 1 at end; lowercases
    s = s.lower().strip()
    s = "  " + s + " "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def _similarity(s1: str, s2: str) -> float:
    """Compute pg_trgm similarity (Jaccard on trigram bags)."""
    t1, t2 = _trigrams(s1), _trigrams(s2)
    if not t1 and not t2:
        return 1.0
    if not t1 or not t2:
        return 0.0
    common = len(t1 & t2)
    return 2.0 * common / (len(t1) + len(t2))


def find_duplicates(cards: list[dict], threshold: float, verbose: bool) -> list[dict]:
    """Find all duplicate pairs using an inverted trigram index. O(n log n) expected."""
    print(f"Building trigram index for {len(cards):,} cards...", file=sys.stderr)
    t0 = time.monotonic()

    # inverted index: trigram -> list of (card_index, trigram_count_for_this_card)
    trgm_index: dict[str, list[int]] = defaultdict(list)
    card_trgms: list[set[str]] = []

    for i, card in enumerate(cards):
        tgm = _trigrams(card["question"])
        card_trgms.append(tgm)
        for tg in tgm:
            trgm_index[tg].append(i)

    print(f"  Index built in {time.monotonic()-t0:.1f}s ({len(trgm_index):,} unique trigrams)", file=sys.stderr)

    # For each card, find candidates via shared trigrams, then verify similarity
    pairs: list[dict] = []
    seen_newer: set[str] = set()
    t0 = time.monotonic()

    for i, card in enumerate(cards):
        if i % 5000 == 0:
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(cards) - i) / rate if rate > 0 else 0
            print(
                f"  {i:>6}/{len(cards):>6} checked | {len(pairs)} dupes | "
                f"{rate:.0f} q/s | ETA {eta:.0f}s",
                file=sys.stderr,
            )

        if card["id"] in seen_newer:
            continue  # already marked as a duplicate

        t_card = card_trgms[i]
        if not t_card:
            continue

        # Count shared trigrams with each candidate
        candidate_hits: dict[int, int] = defaultdict(int)
        for tg in t_card:
            for j in trgm_index.get(tg, []):
                if j != i:
                    candidate_hits[j] += 1

        # Filter candidates by minimum trigram overlap (quick pre-filter)
        min_common = threshold * (len(t_card) + 1)  # rough lower bound
        promising = [j for j, hits in candidate_hits.items() if hits >= min_common * 0.7]

        best_sim = 0.0
        best_j = -1
        for j in promising:
            sim = _similarity(card["question"], cards[j]["question"])
            if sim >= threshold and sim > best_sim:
                # Pick the one with smaller id as "anchor" to avoid dup detection
                newer_ts = card["created_at"]
                other_ts = cards[j]["created_at"]
                if newer_ts < other_ts:
                    # card is OLDER → card[j] is the duplicate
                    if cards[j]["id"] not in seen_newer:
                        best_sim = sim
                        best_j = j
                elif newer_ts > other_ts:
                    # card[j] is OLDER → card is the duplicate → skip (will be found from j's perspective)
                    pass  # or handle both directions
                else:
                    # Same timestamp - pick by ID
                    if card["id"] > cards[j]["id"] and cards[j]["id"] not in seen_newer:
                        best_sim = sim
                        best_j = j

        if best_j >= 0:
            other = cards[best_j]
            newer = card if card["created_at"] >= other["created_at"] else other
            older = other if newer is card else card
            seen_newer.add(newer["id"])
            pair = {
                "newer_id": newer["id"],
                "newer_q":  newer["question"],
                "older_id": older["id"],
                "older_q":  older["question"],
                "sim":      round(best_sim, 4),
                "category": card["category"],
            }
            pairs.append(pair)
            if verbose:
                print(f"  [{best_sim:.3f}] {newer['question'][:70]!r}")
                print(f"       ≈ {older['question'][:70]!r}")

    return pairs


async def main():
    parser = argparse.ArgumentParser(description="Local trigram dedup for cardzerver")
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--delete", action="store_true", help="Actually delete duplicates")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    import asyncpg  # noqa: PLC0415

    host     = os.environ.get("CE_DATABASE_HOST", "localhost")
    port_str = os.environ.get("CE_DATABASE_PORT", "15433")
    port     = int(port_str.split(":")[-1]) if ":" in port_str else int(port_str)
    user     = os.environ.get("CE_DATABASE_USER", "postgres")
    password = os.environ.get("CE_DATABASE_PASSWORD", "")
    dbname   = os.environ.get("CE_DATABASE_NAME", "card_engine")

    print(f"Connecting to {host}:{port}/{dbname} as {user}", file=sys.stderr)
    conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=dbname)

    try:
        # Load all trivia cards
        cat_where = "AND d.title = $1" if args.category else ""
        params = [args.category] if args.category else []
        print("Loading cards...", file=sys.stderr)
        t0 = time.monotonic()
        rows = await conn.fetch(
            f"SELECT c.id::text, c.question, c.created_at, d.title AS category "
            f"FROM cards c JOIN decks d ON d.id = c.deck_id AND d.kind = 'trivia' {cat_where} "
            f"ORDER BY c.created_at",
            *params,
            timeout=120,
        )
        cards = [dict(r) for r in rows]
        print(f"  Loaded {len(cards):,} cards in {time.monotonic()-t0:.1f}s", file=sys.stderr)

        # Find duplicates in Python
        pairs = find_duplicates(cards, args.threshold, args.verbose)

        print(f"\nFound {len(pairs)} duplicates (threshold={args.threshold})", file=sys.stderr)

        # Summary by category
        cat_summary: dict[str, int] = {}
        for p in pairs:
            cat_summary[p["category"]] = cat_summary.get(p["category"], 0) + 1
        for cat, cnt in sorted(cat_summary.items(), key=lambda x: -x[1]):
            print(f"  {cnt:>5}  {cat}", file=sys.stderr)

        # Delete if requested — open a fresh connection since the scan takes
        # many minutes and the original connection may have been dropped by the proxy
        deleted = 0
        if args.delete and pairs:
            to_delete = [p["newer_id"] for p in pairs]
            print(f"\nDeleting {len(to_delete)} duplicates...", file=sys.stderr)
            del_conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database=dbname)
            try:
                for i in range(0, len(to_delete), 200):
                    batch = to_delete[i : i + 200]
                    result = await del_conn.execute(
                        "DELETE FROM cards WHERE id::text = ANY($1::text[])", batch
                    )
                    deleted += int(result.split()[-1])
                    print(f"  Deleted batch {i//200 + 1}: {result}", file=sys.stderr)
            finally:
                await del_conn.close()
            print(f"  Total deleted: {deleted}", file=sys.stderr)
        elif pairs:
            print(f"\nDRY RUN — add --delete to remove {len(pairs)} duplicates", file=sys.stderr)

        # Print JSON summary to stdout
        import json
        print(json.dumps({
            "total_cards": len(cards),
            "duplicates_found": len(pairs),
            "deleted": deleted,
            "dry_run": not args.delete,
            "threshold": args.threshold,
            "by_category": cat_summary,
            "sample_pairs": pairs[:20],
        }, default=str, indent=2))

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
