"""Card generator — turns engine output into flashcard + trivia decks.

Generated decks are inserted into the existing decks + cards tables so
obo-ios and alities-mobile consume them through existing adapters.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections import defaultdict

import asyncpg

from server.family.engine import FamilyGraph, NamedRelation, Person, Relationship

logger = logging.getLogger("card_engine.family.generator")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _display_name(p: Person) -> str:
    """Prefer nickname — more natural and fun for kids."""
    return p.nickname if p.nickname else p.name


def _base_label(label: str) -> str:
    """Strip side + gender qualifiers for grouping.

    'paternal grandmother' → 'grandparent', 'uncle' → 'aunt/uncle', etc.
    """
    # Strip side prefix
    stripped = label
    for prefix in ("paternal ", "maternal "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    # Map gendered labels to neutral groups
    _GROUP = {
        "father": "parent", "mother": "parent",
        "brother": "sibling", "sister": "sibling",
        "grandfather": "grandparent", "grandmother": "grandparent",
        "great-grandfather": "great-grandparent", "great-grandmother": "great-grandparent",
        "uncle": "aunt/uncle", "aunt": "aunt/uncle",
        "uncle (by marriage)": "aunt/uncle", "aunt (by marriage)": "aunt/uncle",
        "great-uncle": "great-aunt/uncle", "great-aunt": "great-aunt/uncle",
        "husband": "spouse", "wife": "spouse",
    }
    return _GROUP.get(stripped, stripped)


_DIFFICULTY_MAP = {1: "easy", 2: "medium", 3: "hard", 4: "hard"}


# ---------------------------------------------------------------------------
# Flashcard templates
# ---------------------------------------------------------------------------

def _flashcard_templates(
    rel: NamedRelation,
    all_relations: list[NamedRelation],
    player_name: str,
) -> list[tuple[str, str, int]]:
    """Return (question, answer, difficulty) tuples for one relation.

    Uses the person's name/nickname IN the question to avoid ambiguity
    when multiple people share the same label (e.g. two parents).
    """
    p = rel.person
    dn = _display_name(p)
    cards: list[tuple[str, str, int]] = []

    # --- Core: identify the relationship using the person's name ---
    cards.append((
        f"How is {dn} related to you?",
        f"Your {rel.label}",
        rel.difficulty,
    ))

    # Only ask "Who is your X?" when this person is the ONLY one with that label
    same_label = [r for r in all_relations if r.label == rel.label]
    if len(same_label) == 1:
        cards.append((
            f"Who is your {rel.label}?",
            dn,
            rel.difficulty,
        ))

    # --- Nickname questions (fun for kids) ---
    if p.nickname:
        cards.append((
            f"What is {p.nickname}'s real name?",
            p.name,
            max(1, rel.difficulty - 1),
        ))
        cards.append((
            f"What do we call {p.name} in our family?",
            p.nickname,
            max(1, rel.difficulty - 1),
        ))

    # --- Maiden name ---
    if p.maiden_name:
        cards.append((
            f"What was {dn}'s last name before getting married?",
            p.maiden_name,
            min(rel.difficulty + 1, 4),
        ))

    # --- Birth year ---
    if p.born:
        cards.append((
            f"What year was {dn} born?",
            str(p.born),
            min(rel.difficulty + 1, 4),
        ))

    return cards


def _bonus_flashcards(
    all_relations: list[NamedRelation],
    player_name: str,
) -> list[tuple[str, str, int]]:
    """Group, counting, and connection questions across the whole tree."""
    cards: list[tuple[str, str, int]] = []

    # Group by base label for counting
    groups: dict[str, list[NamedRelation]] = defaultdict(list)
    for r in all_relations:
        groups[_base_label(r.label)].append(r)

    # --- Counting & naming questions ---
    for base, group in groups.items():
        if len(group) < 2:
            continue
        plural = base + "s"
        if "aunt/uncle" in base:
            plural = "aunts and uncles"

        cards.append((
            f"How many {plural} do you have?",
            str(len(group)),
            2,
        ))
        if len(group) <= 5:
            names = sorted(_display_name(r.person) for r in group)
            cards.append((
                f"Can you name all your {plural}?",
                ", ".join(names),
                3,
            ))

    # --- Twins detection (siblings born same year) ---
    siblings = groups.get("sibling", [])
    if len(siblings) >= 2:
        by_year: dict[int, list[NamedRelation]] = defaultdict(list)
        for r in siblings:
            if r.person.born:
                by_year[r.person.born].append(r)
        for _year, twins in by_year.items():
            if len(twins) >= 2:
                twin_names = sorted(_display_name(r.person) for r in twins)
                cards.append((
                    "Who are the twins in your family?",
                    " and ".join(twin_names),
                    1,
                ))

    # --- Oldest / youngest sibling ---
    if len(siblings) >= 2:
        born_sibs = [(r, r.person.born) for r in siblings if r.person.born]
        if len(born_sibs) >= 2:
            oldest = min(born_sibs, key=lambda x: x[1])
            youngest = max(born_sibs, key=lambda x: x[1])
            if oldest[0].person.id != youngest[0].person.id:
                cards.append((
                    "Who is your oldest sibling?",
                    _display_name(oldest[0].person),
                    2,
                ))

    # --- Oldest / youngest cousin ---
    cousins = groups.get("cousin", [])
    if len(cousins) >= 2:
        born_cousins = [(r, r.person.born) for r in cousins if r.person.born]
        if len(born_cousins) >= 2:
            oldest = min(born_cousins, key=lambda x: x[1])
            youngest = max(born_cousins, key=lambda x: x[1])
            if oldest[0].person.id != youngest[0].person.id:
                cards.append((
                    "Who is the oldest cousin?",
                    _display_name(oldest[0].person),
                    2,
                ))
                cards.append((
                    "Who is the youngest cousin?",
                    _display_name(youngest[0].person),
                    2,
                ))

    # --- Nickname count ---
    nicknamed = [r for r in all_relations if r.person.nickname]
    if len(nicknamed) >= 2:
        cards.append((
            "How many family members have special nicknames?",
            str(len(nicknamed)),
            2,
        ))

    # --- Total relatives ---
    cards.append((
        "How many relatives are in your family tree?",
        str(len(all_relations)),
        3,
    ))

    return cards


# ---------------------------------------------------------------------------
# Trivia templates
# ---------------------------------------------------------------------------

def _trivia_templates(
    rel: NamedRelation,
    all_relations: list[NamedRelation],
    player_name: str,
) -> list[dict]:
    """Return trivia card dicts with multiple-choice answers."""
    p = rel.person
    dn = _display_name(p)
    cards: list[dict] = []

    # Name pool for distractors
    name_pool = [
        _display_name(r.person) for r in all_relations
        if r.person.id != p.id
    ]

    # Label pool for relationship distractors
    all_labels = list({r.label for r in all_relations})

    def _name_distractors(correct: str) -> list[str]:
        options = [n for n in name_pool if n != correct]
        random.shuffle(options)
        result = options[:3]
        while len(result) < 3:
            result.append(f"Not {correct}")
        return result

    def _label_distractors(correct: str) -> list[str]:
        options = [lbl for lbl in all_labels if lbl != correct]
        random.shuffle(options)
        result = options[:3]
        while len(result) < 3:
            result.append("no relation")
        return result

    def _make_trivia(question: str, answer: str, diff: int,
                     distractors: list[str], explanation: str, hint: str) -> dict:
        correct_idx = random.randint(0, 3)
        all_answers = list(distractors[:3])
        all_answers.insert(correct_idx, answer)
        all_answers = all_answers[:4]
        choices = [
            {"text": text, "isCorrect": i == correct_idx}
            for i, text in enumerate(all_answers)
        ]
        return {
            "question": question,
            "choices": choices,
            "correct_index": correct_idx,
            "explanation": explanation,
            "hint": hint,
            "difficulty": _DIFFICULTY_MAP.get(diff, "medium"),
        }

    # --- "How is X related to player?" — always unambiguous ---
    cards.append(_make_trivia(
        f"How is {dn} related to {player_name}?",
        rel.label,
        rel.difficulty,
        _label_distractors(rel.label),
        f"{dn} is {player_name}'s {rel.label}.",
        f"Think about how {dn} fits in the family.",
    ))

    # --- "Who is player's X?" only when unique ---
    same_label = [r for r in all_relations if r.label == rel.label]
    if len(same_label) == 1:
        cards.append(_make_trivia(
            f"Who is {player_name}'s {rel.label}?",
            dn,
            rel.difficulty,
            _name_distractors(dn),
            f"{dn} is {player_name}'s {rel.label}.",
            f"Think about your {rel.label}.",
        ))

    # --- Nickname trivia ---
    if p.nickname:
        cards.append(_make_trivia(
            f"What is {p.nickname}'s real name?",
            p.name,
            max(1, rel.difficulty - 1),
            _name_distractors(p.name),
            f"{p.nickname}'s real name is {p.name}.",
            "Think about family nicknames!",
        ))

    # --- Maiden name trivia ---
    if p.maiden_name:
        maiden_pool = [
            r.person.maiden_name for r in all_relations
            if r.person.maiden_name and r.person.id != p.id
        ] or _name_distractors(p.maiden_name)
        cards.append(_make_trivia(
            f"What was {dn}'s last name before getting married?",
            p.maiden_name,
            min(rel.difficulty + 1, 4),
            maiden_pool,
            f"{dn}'s maiden name was {p.maiden_name}.",
            "Think about family names before marriage.",
        ))

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
            gender=p.get("gender"),
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

    # Skip deceased people — don't generate questions about them
    relations = [r for r in relations if r.person.status != "deceased"]

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
                for question, answer, diff in _flashcard_templates(rel, relations, player_name):
                    card_id = uuid.uuid4()
                    await pool.execute(
                        "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
                        "VALUES ($1, $2, $3, $4, $5, $6::difficulty)",
                        card_id, deck_id, position, question,
                        {"answer": answer},
                        _DIFFICULTY_MAP.get(diff, "medium"),
                    )
                    position += 1
                    total_cards += 1

            # Bonus group/counting/connection cards
            for question, answer, diff in _bonus_flashcards(relations, player_name):
                card_id = uuid.uuid4()
                await pool.execute(
                    "INSERT INTO cards (id, deck_id, position, question, properties, difficulty) "
                    "VALUES ($1, $2, $3, $4, $5, $6::difficulty)",
                    card_id, deck_id, position, question,
                    {"answer": answer},
                    _DIFFICULTY_MAP.get(diff, "medium"),
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
