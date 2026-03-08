"""Corpus-wide duplicate and near-duplicate detection using TF-IDF + cosine similarity.

Two-stage approach:
  Stage 1: Exact signature match (normalized question + correct answer) — O(1)
  Stage 2: TF-IDF vectorization + cosine similarity for semantic near-duplicates
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import asyncpg
from sklearn.feature_extraction.text import TfidfVectorizer
import numpy as np

logger = logging.getLogger("card_engine.quality.dedup")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower().strip())


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
    """Stage 2: TF-IDF + cosine similarity for near-duplicates.

    Skips pairs already caught by exact matching.
    Uses sparse matrix operations — handles tens of thousands efficiently.
    """
    if len(cards) < 2:
        return []

    # Build normalized texts for TF-IDF
    texts = [_normalize(c["question"]) for c in cards]

    vectorizer = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 1),  # unigrams only to limit memory
        max_features=10_000,
    )
    tfidf_matrix = vectorizer.fit_transform(texts)

    # Process in small row batches to stay under 256MB memory.
    # Each batch: compute dot product of a few rows against the full matrix,
    # extract above-threshold pairs, then discard the batch result.

    if exact_sigs is None:
        exact_sigs = set()

    n = tfidf_matrix.shape[0]
    batch_size = 100  # small enough to avoid OOM on 256MB VM
    adj: dict[int, list[tuple[int, float]]] = {}

    tfidf_t = tfidf_matrix.T.tocsc()  # transpose once for efficient column slicing

    for start in range(0, n, batch_size):
        end = min(start + batch_size, n)
        batch = tfidf_matrix[start:end]

        # Sparse dot product: (end-start) x n_features @ n_features x n → (end-start) x n
        sim_batch = batch.dot(tfidf_t)

        # Convert to COO to iterate only non-zero entries
        coo = sim_batch.tocoo()
        for local_i, j, v in zip(coo.row, coo.col, coo.data):
            global_i = start + local_i
            if global_i == j:
                continue  # skip self
            if v < threshold:
                continue
            if global_i < j:  # only store each pair once
                adj.setdefault(global_i, []).append((j, float(v)))

        del sim_batch, coo  # free memory immediately

    # Build clusters from adjacency graph
    clusters_map: dict[int, list[int]] = {}
    assigned: set[int] = set()

    for i in sorted(adj.keys()):
        if i in assigned:
            continue
        neighbors = [(j, v) for j, v in adj[i] if j not in assigned]
        if not neighbors:
            continue

        member_indices = [i] + [j for j, _ in neighbors]

        # Check if this cluster is already fully covered by exact matching
        sigs = {f"{_normalize(cards[m]['question'])}|{_normalize(cards[m]['correct_answer'])}" for m in member_indices}
        if len(sigs) == 1 and next(iter(sigs)) in exact_sigs:
            continue

        avg_sim = float(np.mean([v for _, v in neighbors]))

        for m in member_indices:
            assigned.add(m)
        clusters_map[i] = (member_indices, avg_sim)

    clusters = []
    for rep_idx, (member_indices, avg_sim) in clusters_map.items():

        clusters.append(DupCluster(
            card_ids=[cards[i]["id"] for i in member_indices],
            questions=[cards[i]["question"] for i in member_indices],
            correct_answers=[cards[i]["correct_answer"] for i in member_indices],
            similarity=round(float(avg_sim), 3),
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
