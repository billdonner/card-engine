"""Card generator — turns engine output into flashcard + trivia decks.

Generated decks are inserted into the existing decks + cards tables so
obo-ios and alities-mobile consume them through existing adapters.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone

import asyncpg

from server.family.engine import FamilyGraph, NamedRelation, Person, Relationship

logger = logging.getLogger("card_engine.family.generator")


# ---------------------------------------------------------------------------
# Template definitions
# ---------------------------------------------------------------------------

def _flashcard_templates(rel: NamedRelation) -> list[tuple[str, str, int]]:
    """Return (question, answer, difficulty) tuples for a relation."""
    p = rel.person
    cards: list[tuple[str, str, int]] = []

    # Core question
    cards.append((
        f"Who is your {rel.label}?",
        p.name,
        rel.difficulty,
    ))

    # Nickname question
    if p.nickname:
        cards.append((
            f"What is {p.nickname}'s real name?",
            p.name,
            rel.difficulty,
        ))
        cards.append((
            f"What do you call {p.name}?",
            p.nickname,
            rel.difficulty,
        ))

    # Maiden name question
    if p.maiden_name:
        cards.append((
            f"What was {p.name}'s maiden name?",
            p.maiden_name,
            min(rel.difficulty + 1, 3),
        ))

    # Birth year question
    if p.born:
        cards.append((
            f"When was {p.name} born?",
            str(p.born),
            min(rel.difficulty + 1, 3),
        ))

    return cards


def _trivia_templates(
    rel: NamedRelation,
    all_relations: list[NamedRelation],
    player_name: str,
) -> list[dict]:
    """Return trivia card dicts with multiple choice distractors."""
    p = rel.person
    cards: list[dict] = []

    # Collect same-generation names for distractors
    same_gen = [
        r.person.name for r in all_relations
        if r.person.id != p.id and abs(r.generation - rel.generation) <= 1
    ]

    def _distractors(correct: str, pool: list[str]) -> list[str]:
        options = [n for n in pool if n != correct]
        random.shuffle(options)
        result = options[:3]
        while len(result) < 3:
            result.append(f"Not {correct}")
        return result

    def _make_trivia(question: str, answer: str, diff: int) -> dict:
        wrong = _distractors(answer, same_gen)
        correct_idx = random.randint(0, 3)
        all_answers = list(wrong)
        all_answers.insert(correct_idx, answer)
        choices = [
            {"text": text, "isCorrect": i == correct_idx}
            for i, text in enumerate(all_answers)
        ]
        difficulty_map = {1: "easy", 2: "medium", 3: "hard", 4: "hard"}
        return {
            "question": question,
            "choices": choices,
            "correct_index": correct_idx,
            "explanation": f"{answer} is {player_name}'s {rel.label}.",
            "hint": f"Think about your {rel.label}.",
            "difficulty": difficulty_map.get(diff, "medium"),
        }

    cards.append(_make_trivia(
        f"Who is {player_name}'s {rel.label}?",
        p.name,
        rel.difficulty,
    ))

    if p.nickname:
        cards.append(_make_trivia(
            f"What is {p.nickname}'s real name?",
            p.name,
            rel.difficulty,
        ))

    if p.maiden_name:
        maiden_pool = [
            r.person.maiden_name for r in all_relations
            if r.person.maiden_name and r.person.id != p.id
        ]
        wrong = maiden_pool[:3] if maiden_pool else _distractors(p.maiden_name, same_gen)
        correct_idx = random.randint(0, min(3, len(wrong)))
        all_answers = list(wrong[:3])
        while len(all_answers) < 3:
            all_answers.append(f"Not {p.maiden_name}")
        all_answers.insert(correct_idx, p.maiden_name)
        all_answers = all_answers[:4]
        choices = [
            {"text": text, "isCorrect": i == correct_idx}
            for i, text in enumerate(all_answers)
        ]
        cards.append({
            "question": f"What was {p.name}'s maiden name?",
            "choices": choices,
            "correct_index": correct_idx,
            "explanation": f"{p.name}'s maiden name was {p.maiden_name}.",
            "hint": "Think about family names.",
            "difficulty": "medium" if rel.difficulty <= 2 else "hard",
        })

    return cards


# ---------------------------------------------------------------------------
# Deck generation
# ---------------------------------------------------------------------------

async def generate_decks(
    pool: asyncpg.Pool,
    family_id: str,
    player_id: str,
    people: list[dict],
    relationships: list[dict],
    kinds: list[str] | None = None,
) -> tuple[list[uuid.UUID], int]:
    """Generate flashcard and/or trivia decks for a player.

    Returns (deck_ids, total_cards_created).
    """
    kinds = kinds or ["flashcard", "trivia"]

    # Build engine objects
    engine_people = [
        Person(
            id=str(p["id"]),
            name=p["name"],
            nickname=p.get("nickname"),
            maiden_name=p.get("maiden_name"),
            born=p.get("born"),
            status=p.get("status", "living"),
            player=p.get("player", False),
            placeholder=p.get("placeholder", False),
        )
        for p in people
    ]
    engine_rels = [
        Relationship(
            id=str(r["id"]),
            type=r["type"],
            from_id=str(r["from_id"]),
            to_id=str(r["to_id"]),
        )
        for r in relationships
    ]

    graph = FamilyGraph(engine_people, engine_rels)
    relations = graph.compute_relations(player_id)

    if not relations:
        return [], 0

    player_person = next((p for p in engine_people if p.id == player_id), None)
    player_name = player_person.name if player_person else "you"

    deck_ids: list[uuid.UUID] = []
    total_cards = 0

    for kind in kinds:
        if kind not in ("flashcard", "trivia"):
            continue

        deck_id = uuid.uuid4()
        deck_title = f"{player_name}'s Family {kind.title()}s"
        deck_props = {
            "family_id": family_id,
            "player_id": player_id,
            "status": "published",
            "generated": True,
        }

        await pool.execute(
            "INSERT INTO decks (id, title, kind, properties) "
            "VALUES ($1, $2, $3::deck_kind, $4)",
            deck_id, deck_title, kind, deck_props,
        )
        deck_ids.append(deck_id)

        position = 0

        if kind == "flashcard":
            for rel in relations:
                for question, answer, diff in _flashcard_templates(rel):
                    card_id = uuid.uuid4()
                    difficulty_map = {1: "easy", 2: "medium", 3: "hard", 4: "hard"}
                    await pool.execute(
                        "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
                        "VALUES ($1, $2, $3, $4, $5, $6::difficulty)",
                        card_id, deck_id, position, question,
                        {"answer": answer},
                        difficulty_map.get(diff, "medium"),
                    )
                    position += 1
                    total_cards += 1

        elif kind == "trivia":
            for rel in relations:
                for card_data in _trivia_templates(rel, relations, player_name):
                    card_id = uuid.uuid4()
                    await pool.execute(
                        "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
                        "VALUES ($1, $2, $3, $4, $5, $6::difficulty)",
                        card_id, deck_id, position, card_data["question"],
                        {
                            "choices": card_data["choices"],
                            "correct_index": card_data["correct_index"],
                            "explanation": card_data.get("explanation", ""),
                            "hint": card_data.get("hint", ""),
                            "aisource": "family-tree",
                        },
                        card_data.get("difficulty", "medium"),
                    )
                    position += 1
                    total_cards += 1

        logger.info(
            "Generated %s deck %s for player %s: %d cards",
            kind, deck_id, player_id, position,
        )

    return deck_ids, total_cards
