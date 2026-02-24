"""Category normalization and SF Symbol mapping.

Ported from alities-engine CategoryMap.swift.
"""

# 40-entry alias → canonical name map
ALIAS_TO_CANONICAL: dict[str, str] = {
    "science": "Science & Nature",
    "science & nature": "Science & Nature",
    "nature": "Science & Nature",
    "animals": "Science & Nature",
    "science - computers": "Technology",
    "science - gadgets": "Technology",
    "technology": "Technology",
    "mathematics": "Mathematics",
    "science - mathematics": "Mathematics",
    "history": "History",
    "geography": "Geography",
    "politics": "Politics",
    "sports": "Sports",
    "sport_and_leisure": "Sports",
    "music": "Music",
    "musicals & theatres": "Music",
    "literature": "Literature",
    "books": "Literature",
    "arts_and_literature": "Arts & Literature",
    "arts and literature": "Arts & Literature",
    "art": "Arts & Literature",
    "movies": "Film & TV",
    "film": "Film & TV",
    "film_and_tv": "Film & TV",
    "television": "Film & TV",
    "cartoon & animations": "Film & TV",
    "japanese anime & manga": "Film & TV",
    "video games": "Video Games",
    "board games": "Board Games",
    "comics": "Comics",
    "food & drink": "Food & Drink",
    "food_and_drink": "Food & Drink",
    "pop culture": "Pop Culture",
    "celebrities": "Pop Culture",
    "mythology": "Mythology",
    "society_and_culture": "Society & Culture",
    "society and culture": "Society & Culture",
    "general_knowledge": "General Knowledge",
    "general knowledge": "General Knowledge",
    "vehicles": "Vehicles",
}

# 20-entry canonical name → SF Symbol map
CANONICAL_TO_SYMBOL: dict[str, str] = {
    "Science & Nature": "atom",
    "Technology": "desktopcomputer",
    "Mathematics": "number",
    "History": "clock",
    "Geography": "globe.americas",
    "Politics": "building.columns",
    "Sports": "sportscourt",
    "Music": "music.note",
    "Literature": "book",
    "Arts & Literature": "paintbrush",
    "Film & TV": "film",
    "Video Games": "gamecontroller",
    "Board Games": "gamecontroller",
    "Comics": "text.bubble",
    "Food & Drink": "fork.knife",
    "Pop Culture": "star",
    "Mythology": "sparkles",
    "Society & Culture": "person.3",
    "General Knowledge": "questionmark.circle",
    "Vehicles": "car",
}

CANONICAL_CATEGORIES: list[str] = list(CANONICAL_TO_SYMBOL.keys())


def normalize(raw: str) -> str:
    """Map a raw category string to its canonical name."""
    return ALIAS_TO_CANONICAL.get(raw.lower().strip(), raw)


def symbol_for(category: str) -> str:
    """Return the SF Symbol name for a category (canonical or alias)."""
    canonical = normalize(category)
    return CANONICAL_TO_SYMBOL.get(canonical, "questionmark.circle")
