#!/usr/bin/env python3
"""Bulk generate trivia questions for a specific category with fuzzy dedup.

Usage:
    # Generate 1000 art & literature questions
    python scripts/bulk_generate.py --category "Arts & Literature" --count 1000

    # Dry run (no DB writes, just show what would be inserted)
    python scripts/bulk_generate.py --category "Arts & Literature" --count 50 --dry-run

    # Dedup-only pass (no generation, just find and report duplicates)
    python scripts/bulk_generate.py --dedup-only

    # Dedup + delete duplicates
    python scripts/bulk_generate.py --dedup-only --delete-dupes

Requires: CE_OPENAI_API_KEY and DATABASE_URL environment variables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bulk_generate")

# ---------------------------------------------------------------------------
# Fuzzy dedup utilities
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_NON_ALNUM_RE.sub("", text.lower().strip()).split())


def trigrams(text: str) -> set[str]:
    """Character-level trigrams (3-grams) of normalized text."""
    t = normalize(text)
    if len(t) < 3:
        return {t}
    return {t[i : i + 3] for i in range(len(t) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Trigram Jaccard similarity — robust to typos and spelling errors.

    Unlike word-level Jaccard, trigrams overlap on character sequences,
    so 'Romeo' vs 'Romoe' still shares most trigrams (Rom, ome, meo vs Rom, omo, moe, oeo).
    """
    ta = trigrams(a)
    tb = trigrams(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def word_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity."""
    wa = set(normalize(a).split())
    wb = set(normalize(b).split())
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_fuzzy_duplicate(
    new_q: str,
    new_answer: str,
    existing: list[dict],
    trigram_threshold: float = 0.65,
    word_threshold: float = 0.85,
) -> dict | None:
    """Check if new_q is a duplicate of any existing question.

    Uses two strategies:
    1. Word Jaccard >= 0.85 (catches identical questions with minor rewording)
    2. Trigram similarity >= 0.65 (catches spelling errors, typos, reordering)

    Also checks if the answer is the same (normalized) — same answer + similar
    question is a strong duplicate signal.

    Returns the matching existing question dict, or None.
    """
    norm_new = normalize(new_q)
    norm_answer = normalize(new_answer)

    for ex in existing:
        norm_ex = normalize(ex["question"])

        # Exact normalized match
        if norm_new == norm_ex:
            return ex

        # Strategy 1: word Jaccard
        wj = word_jaccard(new_q, ex["question"])
        if wj >= word_threshold:
            return ex

        # Strategy 2: trigram similarity (catches typos)
        ts = trigram_similarity(new_q, ex["question"])
        if ts >= trigram_threshold:
            # Additional check: if answers also match, definitely a dupe
            ex_answer = normalize(ex.get("correct_answer", ""))
            if ex_answer and norm_answer:
                answer_sim = trigram_similarity(new_answer, ex.get("correct_answer", ""))
                if answer_sim >= 0.6:
                    return ex
            # Even without answer match, very high trigram = dupe
            if ts >= 0.80:
                return ex

    return None


# ---------------------------------------------------------------------------
# OpenAI generation
# ---------------------------------------------------------------------------

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_MODEL = "gpt-4o-mini"
_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)

# Subcategories for diverse art & literature coverage
ART_LIT_SUBCATEGORIES = [
    "classical literature and ancient texts",
    "Shakespeare's plays and sonnets",
    "19th century novels and novelists",
    "20th century modern literature",
    "contemporary fiction and bestsellers",
    "poetry and famous poets",
    "world literature and non-English authors",
    "art history and famous paintings",
    "Renaissance art and artists",
    "Impressionism and post-Impressionism",
    "modern and contemporary art movements",
    "sculpture and architecture in art",
    "mythology in literature and art",
    "children's literature and fairy tales",
    "science fiction and fantasy literature",
    "literary awards and prizes (Nobel, Pulitzer, Booker)",
    "famous literary characters and their creators",
    "playwrights and theater history",
    "art techniques and mediums",
    "photography as art",
    "graphic novels and illustrated books",
    "literary devices and writing techniques",
    "banned and controversial books",
    "literary movements (Romanticism, Realism, Modernism)",
    "art museums and galleries of the world",
    "Japanese literature and manga",
    "Latin American literature (magical realism)",
    "African and Middle Eastern literature",
    "autobiography and memoir",
    "the Beat Generation and counterculture literature",
]


async def generate_batch(
    api_key: str,
    subcategory: str,
    difficulty: str,
    count: int = 10,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Generate a batch of art & literature questions for a specific subcategory."""
    difficulty_guidance = {
        "easy": "Questions should be common knowledge that most people would know",
        "medium": "Questions should require some specific knowledge but not be obscure",
        "hard": "Questions should be challenging and require specialized knowledge",
    }
    guidance = difficulty_guidance.get(difficulty, difficulty_guidance["medium"])

    prompt = (
        f"Generate {count} unique trivia questions about {subcategory} "
        f"(within Arts & Literature) at {difficulty} difficulty level.\n\n"
        "Return a JSON array with this exact structure:\n"
        "[\n"
        "  {\n"
        '    "question": "The question text?",\n'
        '    "correct_answer": "The correct answer",\n'
        '    "incorrect_answers": ["Wrong 1", "Wrong 2", "Wrong 3"],\n'
        '    "explanation": "Brief explanation of why the answer is correct",\n'
        '    "hint": "A subtle clue that helps without giving away the answer"\n'
        "  }\n"
        "]\n\n"
        "Requirements:\n"
        "- Questions must be factually accurate\n"
        "- Each question must have exactly 3 incorrect answers\n"
        "- Incorrect answers should be plausible but clearly wrong\n"
        "- Questions should NOT be about pop culture, film, TV, or music — only books, visual art, theater, and literary topics\n"
        f"- For {difficulty} difficulty: {guidance}\n"
        "- Do NOT repeat common questions like 'Who wrote Romeo and Juliet'\n"
        "- Return ONLY the JSON array, no other text"
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a trivia question generator specializing in arts and literature. "
                    "Generate unique, factually accurate questions. Always respond with valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.9,  # slightly higher for diversity
        "max_tokens": 3000,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)

    try:
        resp = await client.post(_OPENAI_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_response(content, subcategory, difficulty)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.warning("Rate limited, waiting 30s...")
            await asyncio.sleep(30)
            return []
        logger.error("OpenAI HTTP %d: %s", exc.response.status_code, exc.response.text[:200])
        return []
    except Exception as exc:
        logger.error("OpenAI request failed: %s", exc)
        return []
    finally:
        if own_client:
            await client.aclose()


def _parse_response(content: str, subcategory: str, difficulty: str) -> list[dict]:
    """Parse OpenAI response into card dicts."""
    import random

    cleaned = _FENCE_RE.sub("", content).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or start >= end:
        logger.warning("Could not find JSON array in response")
        return []

    try:
        questions = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse OpenAI JSON: %s", exc)
        return []

    results = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question", "")
        correct_answer = q.get("correct_answer", "")
        incorrect = q.get("incorrect_answers", [])
        if not question_text or not correct_answer or len(incorrect) < 3:
            continue

        incorrect = incorrect[:3]
        correct_index = random.randint(0, 3)
        all_answers = list(incorrect)
        all_answers.insert(correct_index, correct_answer)

        choices = [
            {"text": text, "isCorrect": i == correct_index}
            for i, text in enumerate(all_answers)
        ]

        results.append({
            "question": question_text,
            "correct_answer": correct_answer,
            "category": "Arts & Literature",
            "subcategory": subcategory,
            "difficulty": difficulty,
            "choices": choices,
            "correct_index": correct_index,
            "explanation": q.get("explanation", ""),
            "hint": q.get("hint", ""),
        })

    return results


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

async def load_existing_questions(pool: asyncpg.Pool, categories: list[str]) -> list[dict]:
    """Load all existing questions from the given trivia categories."""
    placeholders = ", ".join(f"${i+1}" for i in range(len(categories)))
    rows = await pool.fetch(
        f"SELECT c.id::text, c.question, c.properties, d.title as category "
        f"FROM cards c JOIN decks d ON c.deck_id = d.id "
        f"WHERE d.kind = 'trivia' AND d.title IN ({placeholders}) "
        f"ORDER BY c.created_at",
        *categories,
    )
    results = []
    for row in rows:
        props = row["properties"] or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (json.JSONDecodeError, TypeError):
                props = {}
        if not isinstance(props, dict):
            props = {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)
        correct_answer = ""
        if choices and correct_idx < len(choices):
            c = choices[correct_idx]
            correct_answer = c["text"] if isinstance(c, dict) else str(c)
        results.append({
            "id": row["id"],
            "question": row["question"],
            "correct_answer": correct_answer,
            "category": row["category"],
        })
    return results


async def insert_card(pool: asyncpg.Pool, q: dict, deck_id: uuid.UUID, source_id: uuid.UUID | None) -> uuid.UUID:
    """Insert a single card."""
    max_pos = await pool.fetchval(
        "SELECT COALESCE(MAX(position), -1) FROM cards WHERE deck_id = $1", deck_id
    )
    position = (max_pos or 0) + 1
    card_id = uuid.uuid4()
    properties = {
        "choices": q["choices"],
        "correct_index": q["correct_index"],
        "explanation": q.get("explanation", ""),
        "hint": q.get("hint", ""),
        "aisource": "openai",
        "subcategory": q.get("subcategory", ""),
    }
    await pool.execute(
        "INSERT INTO cards (id, deck_id, position, question, properties, difficulty, source_id, source_date) "
        "VALUES ($1, $2, $3, $4, $5, $6::difficulty, $7, $8)",
        card_id, deck_id, position, q["question"], properties,
        q.get("difficulty", "medium"), source_id, datetime.now(timezone.utc),
    )
    return card_id


# ---------------------------------------------------------------------------
# Dedup scan
# ---------------------------------------------------------------------------

async def find_all_duplicates(pool: asyncpg.Pool) -> list[tuple[dict, dict]]:
    """Scan ALL trivia questions for duplicates using trigram + word Jaccard.

    Returns list of (original, duplicate) pairs.
    """
    rows = await pool.fetch(
        "SELECT c.id::text, c.question, c.properties, c.created_at, d.title as category "
        "FROM cards c JOIN decks d ON c.deck_id = d.id "
        "WHERE d.kind = 'trivia' "
        "ORDER BY c.created_at"
    )

    questions = []
    for row in rows:
        props = row["properties"] or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (json.JSONDecodeError, TypeError):
                props = {}
        if not isinstance(props, dict):
            props = {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)
        correct_answer = ""
        if choices and correct_idx < len(choices):
            c = choices[correct_idx]
            correct_answer = c["text"] if isinstance(c, dict) else str(c)
        questions.append({
            "id": row["id"],
            "question": row["question"],
            "correct_answer": correct_answer,
            "category": row["category"],
            "created_at": row["created_at"],
        })

    logger.info("Scanning %d questions for duplicates...", len(questions))

    duplicates = []
    seen_ids = set()

    for i, q in enumerate(questions):
        if q["id"] in seen_ids:
            continue
        if i % 500 == 0 and i > 0:
            logger.info("  scanned %d/%d, found %d duplicates so far", i, len(questions), len(duplicates))

        # Check against all earlier questions
        for j in range(i):
            if questions[j]["id"] in seen_ids:
                continue

            # Quick word Jaccard check first
            wj = word_jaccard(q["question"], questions[j]["question"])
            if wj >= 0.85:
                duplicates.append((questions[j], q))  # j is original (older), i is dupe
                seen_ids.add(q["id"])
                break

            # Trigram check for typos
            ts = trigram_similarity(q["question"], questions[j]["question"])
            if ts >= 0.70:
                # Also check answer similarity
                answer_sim = trigram_similarity(
                    q["correct_answer"], questions[j]["correct_answer"]
                )
                if answer_sim >= 0.60:
                    duplicates.append((questions[j], q))
                    seen_ids.add(q["id"])
                    break

    return duplicates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Bulk generate trivia with fuzzy dedup")
    parser.add_argument("--category", default="Arts & Literature", help="Target category")
    parser.add_argument("--count", type=int, default=1000, help="Number of questions to generate")
    parser.add_argument("--batch-size", type=int, default=15, help="Questions per OpenAI call")
    parser.add_argument("--concurrent", type=int, default=3, help="Concurrent OpenAI calls")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--dedup-only", action="store_true", help="Only scan for duplicates, no generation")
    parser.add_argument("--delete-dupes", action="store_true", help="Delete found duplicates (with --dedup-only)")
    args = parser.parse_args()

    # Use same env var pattern as server/db.py
    db_url = os.environ.get("CE_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not db_url:
        db_url = "postgresql://billdonner@localhost:5432/card_engine"
        logger.info("No DATABASE_URL set, using default: %s", db_url)

    async def _init_conn(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    pool = await asyncpg.create_pool(db_url, init=_init_conn)

    if args.dedup_only:
        await run_dedup_scan(pool, args.delete_dupes)
        await pool.close()
        return

    api_key = os.environ.get("CE_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        print("ERROR: CE_OPENAI_API_KEY or OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    await run_generation(pool, api_key, args)
    await pool.close()


async def run_dedup_scan(pool: asyncpg.Pool, delete: bool):
    """Scan all trivia for duplicates and optionally delete them."""
    duplicates = await find_all_duplicates(pool)

    if not duplicates:
        print("\nNo duplicates found!")
        return

    print(f"\n{'='*80}")
    print(f"Found {len(duplicates)} duplicate pairs:")
    print(f"{'='*80}")

    cat_counts: Counter = Counter()
    for orig, dupe in duplicates:
        cat_counts[dupe["category"]] += 1
        wj = word_jaccard(orig["question"], dupe["question"])
        ts = trigram_similarity(orig["question"], dupe["question"])
        print(f"\n  [{dupe['category']}] word={wj:.2f} trigram={ts:.2f}")
        print(f"  KEEP: {orig['question'][:100]}")
        print(f"       Answer: {orig['correct_answer']}")
        print(f"  DUPE: {dupe['question'][:100]}")
        print(f"       Answer: {dupe['correct_answer']}")

    print(f"\n{'='*80}")
    print("Duplicates by category:")
    for cat, count in cat_counts.most_common():
        print(f"  {cat}: {count}")
    print(f"  TOTAL: {len(duplicates)}")

    if delete:
        dupe_ids = [uuid.UUID(d["id"]) for _, d in duplicates]
        deleted = await pool.execute(
            "DELETE FROM cards WHERE id = ANY($1::uuid[])", dupe_ids
        )
        print(f"\nDeleted {len(dupe_ids)} duplicate cards.")
    else:
        print("\nRun with --delete-dupes to remove these duplicates.")


async def run_generation(pool: asyncpg.Pool, api_key: str, args):
    """Generate questions with fuzzy dedup."""
    import random

    target_category = args.category
    target_count = args.count

    # Also check the "Literature" category for cross-category dedup
    search_categories = [target_category]
    if target_category == "Arts & Literature":
        search_categories.append("Literature")
    elif target_category == "Literature":
        search_categories.append("Arts & Literature")

    # Load existing questions for dedup
    existing = await load_existing_questions(pool, search_categories)
    logger.info("Loaded %d existing questions from %s for dedup", len(existing), search_categories)

    # Get or create deck
    deck_row = await pool.fetchrow(
        "SELECT id FROM decks WHERE kind = 'trivia' AND title = $1", target_category
    )
    if deck_row:
        deck_id = deck_row["id"]
    else:
        deck_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO decks (id, title, kind, properties, tier) "
            "VALUES ($1, $2, 'trivia'::deck_kind, $3, 'free'::deck_tier)",
            deck_id, target_category, {"pic": "paintbrush"},
        )

    # Get source provider
    source_row = await pool.fetchrow("SELECT id FROM source_providers WHERE name = 'openai'")
    source_id = source_row["id"] if source_row else None

    # Generate in batches
    total_generated = 0
    total_inserted = 0
    total_dupes = 0
    batch_num = 0
    difficulties = ["easy", "medium", "hard"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        while total_inserted < target_count:
            remaining = target_count - total_inserted
            batch_num += 1

            # Pick random subcategories for diversity
            subcats = random.sample(
                ART_LIT_SUBCATEGORIES,
                min(args.concurrent, len(ART_LIT_SUBCATEGORIES)),
            )

            logger.info(
                "Batch %d: generating %d questions across %d subcategories (need %d more)",
                batch_num, args.batch_size * len(subcats), len(subcats), remaining,
            )

            tasks = []
            for subcat in subcats:
                diff = random.choice(difficulties)
                tasks.append(
                    generate_batch(api_key, subcat, diff, args.batch_size, client)
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_questions = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Batch failed: %s", result)
                    continue
                batch_questions.extend(result)

            total_generated += len(batch_questions)

            # Dedup and insert
            for q in batch_questions:
                if total_inserted >= target_count:
                    break

                match = is_fuzzy_duplicate(
                    q["question"],
                    q["correct_answer"],
                    existing,
                )
                if match:
                    total_dupes += 1
                    continue

                if not args.dry_run:
                    card_id = await insert_card(pool, q, deck_id, source_id)
                    logger.debug("Inserted card %s", card_id)

                # Add to existing corpus for future dedup
                existing.append({
                    "id": str(uuid.uuid4()),
                    "question": q["question"],
                    "correct_answer": q["correct_answer"],
                    "category": target_category,
                })
                total_inserted += 1

            logger.info(
                "Progress: %d/%d inserted (%d generated, %d dupes skipped)",
                total_inserted, target_count, total_generated, total_dupes,
            )

            # Small delay between batches to avoid rate limiting
            if total_inserted < target_count:
                await asyncio.sleep(2)

    print(f"\n{'='*80}")
    print(f"Generation complete!")
    print(f"  Target:     {target_count}")
    print(f"  Generated:  {total_generated}")
    print(f"  Inserted:   {total_inserted}")
    print(f"  Duplicates: {total_dupes}")
    print(f"  Batches:    {batch_num}")
    if args.dry_run:
        print(f"  (DRY RUN — nothing written to DB)")
    print(f"{'='*80}")


if __name__ == "__main__":
    asyncio.run(main())
