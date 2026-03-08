"""Corpus-wide duplicate and near-duplicate detection.

Two-stage approach:
  Stage 1: Exact signature match (normalized question + correct answer) — O(1)
  Stage 2: MinHash + word-set Jaccard similarity for near-duplicates
           (pure Python, no sklearn/numpy — fits in 256MB VM)
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger("card_engine.quality.dedup")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower().strip())


def _jaccard(a: set[str], b: set[str]) -> float:
    """Word-set Jaccard similarity."""
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _word_set(text: str) -> set[str]:
    """Get the word set of a normalized text, filtering trivial words."""
    words = set(_normalize(text).split())
    # Remove very common words that add noise
    words -= {"what", "which", "who", "the", "a", "an", "is", "of", "in", "and", "to", "for"}
    return words


def _minhash(words: set[str], num_hashes: int = 64) -> list[int]:
    """Compute MinHash signature for a word set.

    Uses SHA-256 with different seeds for each hash function.
    This allows O(1) approximate Jaccard estimation.
    """
    if not words:
        return [0] * num_hashes
    sig = []
    for seed in range(num_hashes):
        min_hash = float("inf")
        for word in words:
            h = int(hashlib.md5(f"{seed}:{word}".encode()).hexdigest()[:8], 16)
            if h < min_hash:
                min_hash = h
        sig.append(min_hash)
    return sig


def _minhash_similarity(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimate Jaccard similarity from MinHash signatures."""
    if not sig_a or not sig_b:
        return 0.0
    matches = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return matches / len(sig_a)


@dataclass
class DupCluster:
    """A group of cards that are duplicates or near-duplicates of each other."""
    card_ids: list[str]
    questions: list[str]
    correct_answers: list[str]
    similarity: float
    match_type: str  # "exact" or "near"


@dataclass
class DedupResult:
    total_cards: int = 0
    exact_clusters: list[DupCluster] = field(default_factory=list)
    near_clusters: list[DupCluster] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def total_duplicates(self) -> int:
        """Total number of duplicate cards (all but the first in each cluster)."""
        count = 0
        for c in self.exact_clusters:
            count += len(c.card_ids) - 1
        for c in self.near_clusters:
            count += len(c.card_ids) - 1
        return count


async def load_trivia_cards(pool: asyncpg.Pool) -> list[dict]:
    """Load all non-quarantined trivia cards from DB."""
    rows = await pool.fetch(
        "SELECT c.id::text, c.question, c.properties, d.title AS topic "
        "FROM cards c "
        "JOIN decks d ON d.id = c.deck_id "
        "WHERE d.kind = 'trivia' "
        "  AND c.quarantined = FALSE "
        "ORDER BY c.created_at"
    )
    cards = []
    for r in rows:
        raw_props = r["properties"]
        props = raw_props if isinstance(raw_props, dict) else {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)
        correct_answer = ""
        if choices and correct_idx < len(choices):
            c = choices[correct_idx]
            correct_answer = c["text"] if isinstance(c, dict) else str(c)
        cards.append({
            "id": r["id"],
            "question": r["question"],
            "correct_answer": correct_answer,
            "topic": r["topic"],
        })
    return cards


def find_exact_duplicates(cards: list[dict]) -> list[DupCluster]:
    """Stage 1: Find exact signature duplicates."""
    sig_map: dict[str, list[int]] = {}
    for i, card in enumerate(cards):
        sig = f"{_normalize(card['question'])}|{_normalize(card['correct_answer'])}"
        sig_map.setdefault(sig, []).append(i)

    clusters = []
    for indices in sig_map.values():
        if len(indices) > 1:
            clusters.append(DupCluster(
                card_ids=[cards[i]["id"] for i in indices],
                questions=[cards[i]["question"] for i in indices],
                correct_answers=[cards[i]["correct_answer"] for i in indices],
                similarity=1.0,
                match_type="exact",
            ))
    return clusters


def find_near_duplicates(
    cards: list[dict],
    threshold: float = 0.85,
    exact_sigs: set[str] | None = None,
) -> list[DupCluster]:
    """Stage 2: MinHash-accelerated Jaccard similarity for near-duplicates.

    Uses MinHash for fast candidate generation (LSH-like), then verifies
    with exact Jaccard. Pure Python — no sklearn/numpy required.
    Fits comfortably in 256MB.
    """
    if len(cards) < 2:
        return []

    if exact_sigs is None:
        exact_sigs = set()

    # Precompute word sets and MinHash signatures
    word_sets = [_word_set(c["question"]) for c in cards]
    minhash_sigs = [_minhash(ws, num_hashes=32) for ws in word_sets]

    # Use LSH banding: split 32 hashes into 8 bands of 4.
    # Two items are candidates if they share at least one band.
    # Tuned for high threshold (0.85) — fewer bands = fewer false candidates.
    num_bands = 8
    band_size = 4
    candidates: set[tuple[int, int]] = set()

    for band_idx in range(num_bands):
        band_start = band_idx * band_size
        band_end = band_start + band_size
        bucket: dict[tuple, list[int]] = {}

        for i, sig in enumerate(minhash_sigs):
            band_key = tuple(sig[band_start:band_end])
            bucket.setdefault(band_key, []).append(i)

        for indices in bucket.values():
            if len(indices) > 1:
                for a_idx in range(len(indices)):
                    for b_idx in range(a_idx + 1, len(indices)):
                        candidates.add((indices[a_idx], indices[b_idx]))

    # Verify candidates with exact Jaccard
    clusters_map: dict[int, list[tuple[int, float]]] = {}
    assigned: set[int] = set()

    verified_pairs: list[tuple[int, int, float]] = []
    for i, j in candidates:
        sim = _jaccard(word_sets[i], word_sets[j])
        if sim >= threshold:
            verified_pairs.append((i, j, sim))

    # Sort by first index for stable clustering
    verified_pairs.sort()

    for i, j, sim in verified_pairs:
        if i in assigned or j in assigned:
            continue

        # Check if already an exact dup
        sig_i = f"{_normalize(cards[i]['question'])}|{_normalize(cards[i]['correct_answer'])}"
        sig_j = f"{_normalize(cards[j]['question'])}|{_normalize(cards[j]['correct_answer'])}"
        if sig_i == sig_j and sig_i in exact_sigs:
            continue

        # Start a cluster from this pair
        cluster = [i, j]
        assigned.add(i)
        assigned.add(j)

        # Check if any other candidates can join
        for k in range(j + 1, len(cards)):
            if k in assigned:
                continue
            if (i, k) in candidates or (j, k) in candidates:
                s = _jaccard(word_sets[i], word_sets[k])
                if s >= threshold:
                    cluster.append(k)
                    assigned.add(k)

        clusters_map[i] = [(m, sim) for m in cluster]

    clusters = []
    for rep_idx, members in clusters_map.items():
        member_indices = [m for m, _ in members]
        avg_sim = sum(s for _, s in members) / len(members) if members else threshold

        clusters.append(DupCluster(
            card_ids=[cards[i]["id"] for i in member_indices],
            questions=[cards[i]["question"] for i in member_indices],
            correct_answers=[cards[i]["correct_answer"] for i in member_indices],
            similarity=round(avg_sim, 3),
            match_type="near",
        ))

    return clusters


async def scan_duplicates(
    pool: asyncpg.Pool,
    threshold: float = 0.85,
) -> DedupResult:
    """Run full corpus dedup scan. Returns clusters of duplicates."""
    t0 = time.time()
    cards = await load_trivia_cards(pool)

    exact = find_exact_duplicates(cards)

    # Build set of exact sigs to skip in near-dup stage
    exact_sigs: set[str] = set()
    for cluster in exact:
        for q, a in zip(cluster.questions, cluster.correct_answers):
            exact_sigs.add(f"{_normalize(q)}|{_normalize(a)}")

    near = find_near_duplicates(cards, threshold=threshold, exact_sigs=exact_sigs)

    elapsed = time.time() - t0
    return DedupResult(
        total_cards=len(cards),
        exact_clusters=exact,
        near_clusters=near,
        elapsed_seconds=round(elapsed, 2),
    )


async def purge_duplicates(
    pool: asyncpg.Pool,
    result: DedupResult,
    dry_run: bool = False,
) -> dict:
    """Quarantine duplicate cards (keeping the first in each cluster).

    Returns summary of actions taken.
    """
    quarantine_ids: list[str] = []

    for cluster in result.exact_clusters:
        # Keep first, quarantine rest
        quarantine_ids.extend(cluster.card_ids[1:])

    for cluster in result.near_clusters:
        quarantine_ids.extend(cluster.card_ids[1:])

    if dry_run or not quarantine_ids:
        return {
            "would_quarantine": len(quarantine_ids),
            "dry_run": dry_run,
            "quarantined_ids": quarantine_ids,
        }

    async with pool.acquire() as conn:
        async with conn.transaction():
            for card_id in quarantine_ids:
                await conn.execute(
                    "UPDATE cards SET quarantined = TRUE, quarantine_reason = $2 "
                    "WHERE id = $1::uuid AND quarantined = FALSE",
                    card_id, "duplicate",
                )

    return {
        "quarantined": len(quarantine_ids),
        "dry_run": False,
        "quarantined_ids": quarantine_ids,
    }
