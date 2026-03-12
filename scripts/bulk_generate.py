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

# Subcategories per category — add new categories here to unlock them for bulk_generate
CATEGORY_SUBCATEGORIES: dict[str, list[str]] = {
    "Arts & Literature": [
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
    ],
    "General Knowledge": [
        "everyday science and how things work",
        "famous inventions and inventors",
        "world capitals and countries",
        "animals and the natural world",
        "food and cooking around the world",
        "human body and health basics",
        "famous quotes and who said them",
        "currencies, flags, and national symbols",
        "popular sports and their rules",
        "major world religions and beliefs",
        "oceans, mountains, and geography records",
        "famous firsts in history",
        "common phrases and their origins",
        "the solar system and space basics",
        "plants and the environment",
        "aviation, ships, and transportation history",
        "famous buildings and landmarks",
        "record holders and world records",
        "time zones, calendars, and measurement",
        "Nobel Prize winners and their achievements",
        "mathematics and number facts",
        "weather, climate, and natural disasters",
        "languages and linguistics",
        "colors, shapes, and visual perception",
        "everyday technology and how it works",
        "economics and money basics",
        "law and government fundamentals",
        "psychology and human behavior",
        "medicine and famous doctors",
        "popular culture and entertainment trivia",
    ],
    "History": [
        "ancient Egypt and the pharaohs",
        "ancient Greece and city-states",
        "the Roman Empire and its fall",
        "medieval Europe and the feudal system",
        "the Crusades and holy wars",
        "the Renaissance and Reformation",
        "the Age of Exploration and colonization",
        "the French Revolution",
        "the American Revolution and founding of the US",
        "the Industrial Revolution",
        "World War I causes and battles",
        "World War II in Europe",
        "World War II in the Pacific",
        "the Holocaust and genocide",
        "the Cold War and nuclear age",
        "the Space Race",
        "the Civil Rights Movement",
        "the Vietnam War",
        "decolonization and independence movements in Africa and Asia",
        "the Russian Revolution and Soviet Union",
        "ancient China and its dynasties",
        "the Mongol Empire",
        "the Ottoman Empire",
        "the British Empire",
        "the American Civil War",
        "the Great Depression",
        "ancient Mesopotamia and the first civilizations",
        "the history of democracy and voting rights",
        "famous leaders, emperors, and conquerors",
        "historical treaties and turning-point events",
    ],
    "Politics": [
        "US presidents and their administrations",
        "the US Congress and legislative process",
        "the US Supreme Court and landmark rulings",
        "the US Constitution and Bill of Rights",
        "elections and voting systems around the world",
        "political ideologies (liberalism, conservatism, socialism, etc.)",
        "the United Nations and international organizations",
        "NATO and military alliances",
        "famous political speeches and moments",
        "political parties and their histories",
        "heads of state and prime ministers worldwide",
        "the European Union and European politics",
        "revolutions and coups in the 20th century",
        "diplomacy and international treaties",
        "political scandals and controversies",
        "the history of democracy and voting rights",
        "communism and socialist governments",
        "dictators and authoritarian regimes",
        "political philosophy (Locke, Rousseau, Machiavelli, etc.)",
        "the Cold War and geopolitics",
        "the United Kingdom's Parliament and monarchy",
        "African and Asian political leaders",
        "political economics: tariffs, sanctions, trade policy",
        "espionage, intelligence agencies, and the CIA/KGB",
        "war, peace treaties, and the laws of armed conflict",
        "women in politics and feminist political movements",
        "environmental politics and the Green movement",
        "third-party and independent political movements",
        "constitutional monarchies and republics",
        "lobbying, campaign finance, and political influence",
    ],
    "Pop Culture": [
        "blockbuster movies and film franchises",
        "Academy Awards and film history",
        "iconic TV shows and their characters",
        "reality TV and game shows",
        "pop and rock music history",
        "famous musicians and bands",
        "Grammy Awards and music records",
        "video games and gaming culture",
        "comic books and superhero franchises (Marvel and DC)",
        "animated movies and cartoon series",
        "social media trends and internet culture",
        "fashion designers and iconic fashion moments",
        "celebrity gossip and famous relationships",
        "sports stars and their records",
        "the Olympics and sports culture",
        "Saturday Night Live and comedy culture",
        "famous advertising campaigns and slogans",
        "Broadway musicals and theater",
        "streaming services and binge-worthy shows",
        "toys, board games, and childhood nostalgia",
        "theme parks and pop culture experiences",
        "viral moments and internet memes",
        "anime and Japanese pop culture",
        "stand-up comedy and comedians",
        "true crime podcasts and documentaries",
        "dance crazes and choreography",
        "famous duos and groups (bands, comedy pairs, etc.)",
        "awards shows: Emmys, MTV VMAs, Golden Globes",
        "pop culture of the 1980s and 1990s",
        "celebrity catchphrases and memorable quotes",
    ],
    "Literature": [
        "classic novels and their authors",
        "Shakespeare's plays and sonnets",
        "19th century fiction and novelists",
        "20th century American literature",
        "British literature through the ages",
        "poetry and famous poets",
        "world literature and non-English authors",
        "children's literature and fairy tales",
        "science fiction and fantasy novels",
        "mystery and crime fiction",
        "literary awards (Nobel, Pulitzer, Booker, Hugo)",
        "famous literary characters and their creators",
        "literary movements (Romanticism, Realism, Modernism, Postmodernism)",
        "banned and challenged books",
        "mythology and its influence on literature",
        "short story writers and their collections",
        "autobiography and memoir",
        "Latin American literature and magical realism",
        "African literature and postcolonial writing",
        "Japanese and Asian literature",
        "the Beat Generation and counterculture writing",
        "Gothic and horror literature",
        "epistolary novels and experimental fiction",
        "literary devices and narrative techniques",
        "publishing history and famous editors",
        "ancient epic poetry (Homer, Virgil, Dante)",
        "Renaissance and Elizabethan literature",
        "Romantic poets (Keats, Shelley, Byron, Wordsworth)",
        "Victorian novelists and social commentary",
        "contemporary and debut fiction",
    ],
    "Geography": [
        "world capitals and major cities",
        "countries and their continents",
        "rivers, lakes, and bodies of water",
        "mountains, deserts, and physical geography",
        "flags, currencies, and national symbols",
        "US states, capitals, and geography",
        "European countries and their geography",
        "Asian countries and regions",
        "African geography and nations",
        "South American geography",
        "oceans, seas, and island nations",
        "climate zones and biomes",
        "famous national parks and natural wonders",
        "borders, disputed territories, and geopolitics",
        "population, density, and demographics",
        "largest and smallest countries by area",
        "landlocked countries and island chains",
        "rivers and their countries of origin",
        "latitude, longitude, and time zones",
        "ancient civilizations and their locations",
        "trade routes and economic geography",
        "volcanoes, earthquakes, and tectonic plates",
        "the Arctic, Antarctic, and polar geography",
        "canals, straits, and maritime geography",
        "UNESCO World Heritage Sites",
        "geographic extremes (highest, lowest, hottest, coldest)",
        "Australia and Oceania geography",
        "Middle East and North Africa geography",
        "Central Asia and the Caucasus",
        "map reading and cartography history",
    ],
    "Music": [
        "classical composers and their works",
        "opera and its history",
        "jazz musicians and the jazz era",
        "blues and its origins",
        "rock and roll history and pioneers",
        "pop music and chart-toppers",
        "hip hop and rap history",
        "country music artists and songs",
        "famous albums and their stories",
        "Grammy Award winners and records",
        "music theory and terminology",
        "musical instruments and orchestras",
        "music festivals and iconic performances",
        "the British Invasion (Beatles, Rolling Stones, etc.)",
        "Motown and soul music",
        "punk and new wave music",
        "electronic music and DJs",
        "music of the 1980s",
        "music of the 1990s",
        "legendary live performances and concerts",
        "music producers and behind-the-scenes legends",
        "one-hit wonders and forgotten hits",
        "music videos and MTV culture",
        "boy bands and girl groups",
        "singer-songwriters and folk music",
        "world music and international artists",
        "musical theater and Broadway soundtracks",
        "film scores and composers",
        "record labels and the music industry",
        "music technology (vinyl, cassette, CD, streaming)",
    ],
    "Mythology": [
        "Greek gods and their domains",
        "Greek heroes and their quests",
        "Roman mythology and its gods",
        "Norse gods and the nine realms",
        "Egyptian gods and the afterlife",
        "Mesopotamian mythology (Sumerian, Babylonian)",
        "Hindu mythology and epic stories",
        "Chinese mythology and legendary figures",
        "Japanese mythology and Shinto gods",
        "Celtic and Arthurian legend",
        "Native American mythology and spirits",
        "Aztec and Mayan mythology",
        "African mythology and trickster figures",
        "creation myths from around the world",
        "mythological creatures and monsters",
        "the Trojan War and Homer's epics",
        "the Odyssey and heroic journeys",
        "Olympian gods and their rivalries",
        "underworld myths across cultures",
        "flood myths and apocalyptic legends",
        "demigods and half-mortal heroes",
        "mythological weapons and artifacts",
        "love stories in mythology (Orpheus, Cupid and Psyche)",
        "transformation myths (Ovid's Metamorphoses)",
        "trickster gods across world mythologies",
        "mythological animals (Pegasus, Phoenix, Dragon, etc.)",
        "Norse Ragnarok and end-of-world myths",
        "Arthurian legend and the Knights of the Round Table",
        "mythology in modern culture and media",
        "comparative mythology and shared themes",
    ],
    "Mathematics": [
        "basic arithmetic and number theory",
        "famous mathematicians and their discoveries",
        "geometry and its theorems",
        "algebra and equations",
        "calculus and its inventors",
        "prime numbers and their properties",
        "famous mathematical constants (pi, e, phi)",
        "probability and statistics",
        "set theory and logic",
        "famous unsolved problems in mathematics",
        "the history of zero and number systems",
        "Fibonacci sequence and the golden ratio",
        "Pythagoras and his theorem",
        "Euclidean geometry and proofs",
        "topology and abstract spaces",
        "graph theory and networks",
        "mathematical paradoxes and puzzles",
        "cryptography and number theory",
        "fractals and chaos theory",
        "binary and computer mathematics",
        "Roman numerals and historical counting systems",
        "mathematical symbols and their origins",
        "game theory and strategy",
        "Fermat's Last Theorem and famous proofs",
        "mathematics in nature and the physical world",
        "trigonometry and its applications",
        "matrices and linear algebra",
        "Gödel's incompleteness theorems",
        "the history of measurement and units",
        "recreational mathematics and puzzles",
    ],
    "Science & Nature": [
        "physics and the laws of motion",
        "chemistry and the periodic table",
        "biology and cell theory",
        "human anatomy and physiology",
        "genetics and DNA",
        "evolution and natural selection",
        "ecology and ecosystems",
        "astronomy and the solar system",
        "space exploration and missions",
        "geology and plate tectonics",
        "meteorology and weather",
        "oceanography and marine biology",
        "botany and plant biology",
        "zoology and animal classification",
        "microbiology and viruses",
        "environmental science and climate change",
        "physics of electricity and magnetism",
        "thermodynamics and energy",
        "optics and light",
        "nuclear physics and radioactivity",
        "famous scientists and their discoveries",
        "scientific method and history of science",
        "medicine and the human immune system",
        "neuroscience and the brain",
        "chemistry of everyday life",
        "entomology and insects",
        "paleontology and dinosaurs",
        "quantum mechanics basics",
        "renewable energy and technology",
        "the deep sea and unexplored nature",
    ],
    "Sports": [
        "American football history and records",
        "basketball history and NBA records",
        "baseball history and MLB records",
        "soccer and FIFA World Cup history",
        "tennis grand slams and legends",
        "golf history and major championships",
        "the Olympic Games and records",
        "boxing legends and famous bouts",
        "ice hockey and the NHL",
        "motorsport and Formula 1",
        "athletics and track and field",
        "swimming and aquatic sports records",
        "cycling and the Tour de France",
        "rugby and international competitions",
        "cricket history and test records",
        "winter sports and the Winter Olympics",
        "extreme sports and their origins",
        "martial arts and combat sports",
        "famous sports stadiums and venues",
        "sports scandals and controversies",
        "women in sports and trailblazers",
        "college sports and the NCAA",
        "sports technology and innovation",
        "iconic sports moments of the 20th century",
        "iconic sports moments of the 21st century",
        "sports team nicknames and their origins",
        "sports records that may never be broken",
        "the history of the Super Bowl",
        "sports betting and fantasy sports",
        "esports and competitive gaming",
    ],
    "Baseball Oddities": [
        "bizarre rules and loopholes in official baseball rulebooks",
        "strange injuries that ended or interrupted careers",
        "unusual plays and freak occurrences mid-game",
        "weird statistical anomalies and impossible-seeming records",
        "odd superstitions and rituals of players and teams",
        "outfield and park peculiarities (Green Monster, Polo Grounds cliff, etc.)",
        "historic games with improbable comebacks or collapses",
        "players who played unusual positions or switched positions unexpectedly",
        "nicknames with strange or forgotten origins",
        "trades that seemed bizarre at the time",
        "famous brawls, ejections, and meltdowns",
        "mascots, promotions, and fan incidents gone wrong",
        "opening days and walk-off moments with unusual circumstances",
        "players who did something notable in their only at-bat or appearance",
        "oddballs: pitchers who hit home runs, position players who pitched",
    ],
    "Football Oddities": [
        "bizarre NFL rules and obscure penalties",
        "strange plays: fumblerooskis, lateral chains, wrong-way runs",
        "improbable comebacks and collapses (Miracle at the Meadowlands, etc.)",
        "weird injuries sustained in unusual ways outside of games",
        "odd superstitions and pre-game rituals of coaches and players",
        "unusual weather games: snow bowls, ice bowls, fog bowls",
        "freak statistical games and impossible-seeming performances",
        "famous blown calls and officiating controversies",
        "unusual stadium situations and field quirks",
        "players who played offense and defense simultaneously",
        "coaches' rants and meltdowns that became iconic",
        "failed fourth-down attempts that changed history",
        "kickers, punters, and long snappers in unusual game-deciding roles",
        "mascots and fan incidents that made headlines",
        "draft busts and sleepers with strange backstories",
    ],
    "Soccer Oddities": [
        "own goals that changed history or ended in record-setting fashion",
        "goalkeeper goals — outfield keepers who scored",
        "red cards in bizarre circumstances (before kickoff, etc.)",
        "games abandoned mid-match for strange reasons",
        "famous referee blunders and hand-of-god moments",
        "unusual penalty shootout moments",
        "players who appeared for multiple national teams",
        "mascots, fan behaviour, and pitch invasions",
        "freak injuries during warm-ups or celebrations",
        "lower-league giant-killings in major cups",
        "strange stadium incidents and pitch conditions",
        "players banned for odd reasons unrelated to on-field play",
        "longest matches, most goals, and improbable score-lines",
        "transfers and contract disputes with strange twists",
        "celebrations that resulted in yellow cards or chaos",
    ],
    "Vehicles": [
        "classic American muscle cars of the 1960s and 70s",
        "Formula 1 racing cars and iconic designs",
        "steam locomotives and the golden age of rail",
        "motorcycles and the history of Harley-Davidson",
        "military vehicles: tanks, Jeeps, and armored cars",
        "electric vehicles and the history of battery-powered transport",
        "luxury and supercar brands (Ferrari, Lamborghini, Bugatti)",
        "aviation: early biplanes and prop-era aircraft",
        "commercial jet airliners and airline history",
        "spacecraft and rockets that made history",
        "ocean liners and famous ships",
        "concept cars and vehicles that never made production",
        "automotive engineering firsts and innovations",
        "trucks, buses, and commercial vehicle history",
        "unusual and record-setting vehicles (land speed records, etc.)",
    ],
    "Video Games": [
        "Atari, Pong, and the origins of home gaming",
        "NES and SNES classic titles and their development stories",
        "Sega Genesis vs. Super Nintendo console wars",
        "PlayStation and Nintendo 64 3D revolution",
        "iconic game designers: Miyamoto, Kojima, Carmack",
        "massively multiplayer online games (WoW, EverQuest)",
        "fighting games: Street Fighter, Mortal Kombat lore",
        "role-playing games: Final Fantasy, Zelda, Pokémon",
        "first-person shooters: Doom, Halo, Half-Life",
        "indie games and the rise of Steam",
        "mobile gaming: Angry Birds, Candy Crush, PUBG Mobile",
        "esports tournaments and competitive gaming history",
        "game glitches and speedrunning records",
        "video game music and composers",
        "Easter eggs and secrets hidden in famous games",
    ],
    "Society & Culture": [
        "wedding and marriage customs around the world",
        "funeral traditions and death rituals globally",
        "coming-of-age ceremonies across cultures",
        "fashion trends and their social significance",
        "tattoo and body modification cultures",
        "social movements and activism in the 20th century",
        "youth subcultures: punk, hip-hop, goth, rave",
        "celebrity culture and the rise of tabloid media",
        "gender roles and how they've changed historically",
        "housing, architecture, and how humans have lived",
        "family structures and kinship across societies",
        "social class and stratification historically",
        "consumerism and advertising's role in society",
        "urban legends, moral panics, and social myths",
        "manners, etiquette, and changing social norms",
    ],
    "Board Games": [
        "chess history, famous games, and grandmasters",
        "the history and strategy of Go",
        "Monopoly's history and its variants",
        "Scrabble rules, records, and champions",
        "Dungeons & Dragons and the tabletop RPG revolution",
        "war games and hex-and-counter wargaming history",
        "abstract strategy games: Othello, Connect Four, Checkers",
        "classic parlor games: Clue, Risk, Life",
        "eurogames and the Catan revolution",
        "cooperative board games: Pandemic, Spirit Island",
        "deck-building games: Dominion and its descendants",
        "party games: Codenames, Taboo, Pictionary",
        "game designers and their award-winning designs",
        "famous board game tournaments and competitions",
        "dexterity games and physical challenge games",
    ],
    "Comics": [
        "Golden Age superheroes and their origins (1938–1956)",
        "Silver Age DC and Marvel characters",
        "Spider-Man's history and key storylines",
        "Batman: origins, villains, and iconic runs",
        "X-Men and mutant lore",
        "independent comics: Image, Dark Horse, Vertigo",
        "manga and the history of Japanese comics",
        "the history of the newspaper comic strip",
        "famous comic book artists and their styles",
        "comic writers: Alan Moore, Grant Morrison, Stan Lee",
        "crossover events and universe-altering storylines",
        "censorship and the Comics Code Authority",
        "superhero deaths and resurrections",
        "female superheroes and representation in comics",
        "comic-to-film adaptations and their histories",
    ],
    "Technology": [
        "the history of the personal computer (Apple, IBM, Microsoft)",
        "the internet: ARPANET, TCP/IP, the World Wide Web",
        "smartphones and the mobile revolution",
        "social media platforms: MySpace, Facebook, Twitter, TikTok",
        "search engines: Google, Yahoo, AltaVista",
        "semiconductors and Moore's Law",
        "artificial intelligence and machine learning history",
        "video streaming: Netflix, YouTube, Twitch",
        "cryptography and encryption history",
        "open-source software and Linux",
        "e-commerce: Amazon, eBay, and digital retail",
        "cybersecurity and famous hacks",
        "space technology: satellites, GPS, Hubble",
        "renewable energy technology: solar, wind, batteries",
        "robotics and automation history",
    ],
    "Food & Drink": [
        "origins of classic dishes and street food",
        "the history of wine and winemaking regions",
        "beer and brewing history: ales, lagers, craft beer",
        "spirits: whisky, rum, vodka, gin origins and production",
        "coffee culture and the history of the café",
        "tea ceremonies and global tea cultures",
        "famous chefs and culinary pioneers",
        "cookbooks and influential culinary writers",
        "spices and the spice trade that shaped history",
        "fast food chains: origins and global spread",
        "regional American cuisine and barbecue traditions",
        "Japanese cuisine: sushi, ramen, tempura history",
        "French haute cuisine and its influence",
        "food preservation: fermentation, canning, refrigeration",
        "world record foods and competitive eating",
    ],
    "Film & TV": [
        "silent film era and the birth of Hollywood",
        "golden age of Hollywood: studios, stars, and censorship",
        "iconic film directors: Hitchcock, Kubrick, Spielberg",
        "Academy Awards history and memorable moments",
        "science fiction films and franchises",
        "classic television sitcoms and their cultural impact",
        "animated films and the Disney legacy",
        "horror films: monsters, slashers, and psychological horror",
        "James Bond franchise history",
        "superhero film adaptations (MCU, DCEU)",
        "reality television and its rise",
        "late-night TV hosts and the talk show tradition",
        "film soundtracks and composers",
        "foreign language films and world cinema",
        "streaming era: Netflix originals and the content wars",
    ],
    "World Geography": [
        "mountain ranges and the world's highest peaks",
        "rivers, lakes, and watersheds of the world",
        "deserts: Sahara, Gobi, Atacama geography and facts",
        "island nations and archipelagos",
        "capitals, borders, and country name changes",
        "oceans, seas, and major straits",
        "national parks and UNESCO World Heritage Sites",
        "climate zones and biomes",
        "population density and the world's largest cities",
        "landlocked countries and geographic curiosities",
        "famous caves, canyons, and geological wonders",
        "volcanoes and geologically active regions",
        "polar regions: Arctic and Antarctic facts",
        "major fault lines and earthquake zones",
        "trade routes and geographic chokepoints throughout history",
    ],
    "Romance Novels": [
        "Regency romance and Jane Austen's legacy",
        "contemporary romance bestsellers and authors",
        "historical romance set in medieval and Tudor periods",
        "romance publishing: Harlequin, Mills & Boon, and the paperback boom",
        "paranormal romance: vampires, shifters, and fantasy settings",
        "romantic suspense and thriller romance",
        "enemies-to-lovers and fake dating tropes",
        "RITA Award winners and romance hall of fame",
        "Nora Roberts, Danielle Steel, and prolific romance authors",
        "the romance novel cover art tradition",
        "romance subgenres: cowboy, billionaire, sports, small-town",
        "queer romance and LGBTQ+ love stories",
        "YA romance and coming-of-age love stories",
        "BookTok and social media's impact on romance fiction",
        "classic love stories that defined the genre",
    ],
    "Silent Movies": [
        "Charlie Chaplin's films and career",
        "Buster Keaton and his death-defying stunts",
        "Harold Lloyd and silent comedy",
        "German Expressionism: Nosferatu, Metropolis, The Cabinet of Dr. Caligari",
        "early Hollywood studios and the star system",
        "the transition from silent to sound (talkies)",
        "D.W. Griffith and the birth of cinematic storytelling",
        "silent film actresses: Mary Pickford, Lillian Gish, Clara Bow",
        "Sergei Eisenstein and Soviet montage theory",
        "the Keystone Cops and slapstick comedy",
        "silent horror: Phantom of the Opera, The Golem",
        "title cards, intertitles, and visual storytelling techniques",
        "live musical accompaniment and theater organ tradition",
        "lost films and the preservation crisis",
        "silent film comedians beyond the big three",
    ],
    "Broadway Musicals": [
        "Golden Age musicals: Rodgers & Hammerstein, Lerner & Loewe",
        "Stephen Sondheim and the concept musical",
        "Andrew Lloyd Webber and British mega-musicals",
        "Lin-Manuel Miranda and Hamilton's cultural impact",
        "Tony Award history and memorable ceremonies",
        "Disney on Broadway: Lion King, Wicked, Frozen",
        "revival productions and reimagined classics",
        "Broadway choreographers: Bob Fosse, Jerome Robbins",
        "famous Broadway theaters and the Theater District",
        "Off-Broadway and experimental theater",
        "jukebox musicals: Mamma Mia, Jersey Boys, Moulin Rouge",
        "the Phantom of the Opera's record-breaking run",
        "legendary Broadway performers and triple threats",
        "musical librettists and lyricists who shaped the art form",
        "Broadway flops that became cult classics",
    ],
    "Cocktails & Spirits": [
        "classic cocktails: martini, manhattan, old fashioned origins",
        "Prohibition era and speakeasy culture",
        "tiki drinks and the Polynesian cocktail craze",
        "whiskey varieties: bourbon, scotch, rye, Irish",
        "gin history from medicine to London dry",
        "rum and its Caribbean heritage",
        "vodka: Russian and Polish origins and production",
        "tequila and mezcal: agave spirits of Mexico",
        "wine regions and grape varietals basics",
        "craft cocktail renaissance and modern mixology",
        "famous bartenders and cocktail inventors",
        "cocktail glassware, tools, and presentation",
        "aperitifs and digestifs traditions worldwide",
        "champagne and sparkling wine history",
        "non-alcoholic spirits and the mocktail movement",
    ],
    "Space Exploration": [
        "the Space Race: Sputnik to Apollo",
        "Apollo missions and the Moon landings",
        "the Space Shuttle program and its legacy",
        "the International Space Station: construction and life aboard",
        "Mars rovers: Spirit, Opportunity, Curiosity, Perseverance",
        "famous astronauts and cosmonauts",
        "NASA history and its landmark missions",
        "SpaceX, Blue Origin, and commercial spaceflight",
        "Hubble and James Webb Space Telescopes",
        "Voyager probes and the outer solar system",
        "space disasters: Challenger, Columbia, Apollo 13",
        "women and minorities in space exploration history",
        "the future of space: Moon bases, Mars colonization",
        "satellites and their impact on daily life",
        "space stations: Skylab, Mir, Tiangong",
    ],
    "True Crime": [
        "famous serial killers and the investigations that caught them",
        "unsolved mysteries and cold cases",
        "forensic science breakthroughs that changed criminal investigation",
        "notorious heists and robberies in history",
        "famous trials and courtroom drama",
        "FBI and law enforcement legendary cases",
        "wrongful convictions and exonerations",
        "organized crime: the Mafia, cartels, and crime families",
        "kidnappings and missing persons cases",
        "white-collar crime and famous fraudsters",
        "true crime podcasts, books, and documentaries",
        "crime scene investigation techniques and technology",
        "prison escapes and fugitives",
        "assassinations and political crimes",
        "cybercrime and digital forensics cases",
    ],
    "Fashion & Design": [
        "iconic fashion designers: Chanel, Dior, Versace, McQueen",
        "fashion decades: the styles that defined each era",
        "haute couture and the Paris fashion system",
        "fashion magazines: Vogue, Harper's Bazaar history",
        "supermodels who defined the industry",
        "streetwear culture and sneaker history",
        "fashion photography and iconic images",
        "textile history: silk, denim, cotton, synthetics",
        "fashion awards: CFDA, Met Gala, British Fashion Awards",
        "sustainable fashion and the slow fashion movement",
        "interior design movements: Art Deco, Bauhaus, Mid-Century Modern",
        "jewelry and watch design through the ages",
        "fashion capitals: Paris, Milan, New York, London",
        "costume design in film and theater",
        "fashion business: luxury brands and their empires",
    ],
    "Roller Coasters": [
        "the history of roller coasters from Russian ice slides to Coney Island",
        "record-breaking coasters: fastest, tallest, longest",
        "famous amusement parks: Cedar Point, Six Flags, Disney",
        "roller coaster engineering: loops, inversions, launches",
        "wooden coasters vs steel coasters: design and ride experience",
        "iconic individual coasters and their stories",
        "theme park design and the immersive ride experience",
        "coaster manufacturers: B&M, Intamin, RMC, Vekoma",
        "water rides and log flumes history",
        "dark rides and haunted attractions",
        "defunct and demolished coasters with legendary reputations",
        "the physics of roller coasters: G-forces, airtime, banking",
        "international theme parks: Alton Towers, Europa-Park, Fuji-Q",
        "flat rides, drop towers, and thrill attractions beyond coasters",
        "theme park history: from traveling carnivals to destination resorts",
    ],
    "Animated Films": [
        "Walt Disney and the early animated features",
        "Pixar's films and revolutionary CGI storytelling",
        "Studio Ghibli and Hayao Miyazaki's masterpieces",
        "DreamWorks Animation: Shrek, Kung Fu Panda, How to Train Your Dragon",
        "stop-motion animation: Aardman, Laika, Ray Harryhausen",
        "classic Disney Renaissance: Little Mermaid to Tarzan",
        "animated film music and songwriting teams",
        "voice acting legends in animated films",
        "Academy Award for Best Animated Feature history",
        "anime films beyond Ghibli: Akira, Your Name, Ghost in the Shell",
        "early animation pioneers: Fleischer Brothers, Tex Avery, Chuck Jones",
        "Looney Tunes, Tom and Jerry, and the golden age of cartoons",
        "adult animated films and pushing the medium's boundaries",
        "animation techniques: cel, CGI, rotoscoping, claymation",
        "animated sequels, prequels, and franchise universes",
    ],
    "Chess": [
        "World Chess Champions from Steinitz to Carlsen",
        "famous chess matches: Fischer vs Spassky, Kasparov vs Deep Blue",
        "chess openings: Sicilian, Queen's Gambit, Ruy Lopez",
        "chess endgame theory and famous endgame studies",
        "the history of chess from ancient India to modern tournaments",
        "chess prodigies and youngest grandmasters",
        "chess strategy: pawn structure, piece activity, king safety",
        "women in chess: Judit Polgar, Hou Yifan, and the Women's Championship",
        "chess notation, ratings (Elo), and the FIDE system",
        "famous chess puzzles and compositions",
        "chess in popular culture: movies, books, and The Queen's Gambit",
        "computer chess: from early programs to AlphaZero",
        "chess variants: blitz, bullet, Chess960, bughouse",
        "famous chess sacrifices and brilliancy prizes",
        "chess clubs, coffeehouses, and the social history of the game",
    ],
    "National Parks": [
        "US National Park Service history and founding",
        "Yellowstone: the world's first national park",
        "Grand Canyon geology and exploration history",
        "Yosemite and the legacy of John Muir",
        "national parks wildlife: bears, wolves, bison, condors",
        "African national parks and safari destinations",
        "national parks of South America: Patagonia, Galápagos, Iguazu",
        "Australian national parks: Great Barrier Reef, Uluru, Blue Mountains",
        "Asian national parks: Zhangjiajie, Komodo, Jiuzhaigou",
        "European national parks and protected landscapes",
        "the National Park ranger tradition and conservation heroes",
        "geological wonders within national parks worldwide",
        "endangered species protection within park boundaries",
        "hiking trails, scenic drives, and iconic park experiences",
        "marine national parks and underwater preserves",
    ],
    "Horror Movies": [
        "classic Universal monsters: Dracula, Frankenstein, the Mummy",
        "slasher films: Halloween, Friday the 13th, Nightmare on Elm Street",
        "psychological horror: The Shining, Psycho, Silence of the Lambs",
        "zombie films from Night of the Living Dead to modern",
        "found footage horror: Blair Witch, Paranormal Activity",
        "Asian horror cinema: The Ring, Ju-On, Audition",
        "horror directors: Hitchcock, Carpenter, Craven, Wan, Peele",
        "supernatural horror: The Exorcist, Poltergeist, Hereditary",
        "body horror and creature features",
        "horror film franchises and their mythology",
        "folk horror and cosmic horror on screen",
        "horror comedies: Shaun of the Dead, Scream, Tucker & Dale",
        "horror movie special effects and practical makeup",
        "horror film scores and the music of fear",
        "modern elevated horror: A24, Blumhouse, and the new wave",
    ],
    "The Beatles": [
        "early Beatles: Hamburg, the Cavern Club, and Beatlemania",
        "Beatles albums from Please Please Me to Let It Be",
        "Lennon-McCartney songwriting partnership",
        "George Harrison's contributions and solo career",
        "Ringo Starr's drumming style and solo work",
        "the British Invasion and the Beatles' American conquest",
        "Sgt. Pepper's Lonely Hearts Club Band and its cultural impact",
        "the Beatles' films: A Hard Day's Night, Help!, Yellow Submarine",
        "Apple Records and the Beatles' business ventures",
        "the breakup: reasons, lawsuits, and aftermath",
        "Beatles album covers and visual art",
        "the Beatles and Indian music, meditation, and the Maharishi",
        "Beatles conspiracy theories and urban legends",
        "Beatles producers and collaborators: George Martin, Billy Preston",
        "the Beatles' influence on rock, pop, and music production",
    ],
    "Inventions": [
        "the printing press and Gutenberg's revolution",
        "the steam engine and the Industrial Revolution",
        "electricity: Edison, Tesla, and the war of currents",
        "the telephone: Bell, Gray, and the race to patent",
        "the automobile: Benz, Ford, and mass production",
        "aviation: the Wright Brothers and early flight",
        "radio and television: Marconi, Farnsworth, Baird",
        "computers: Babbage, Turing, and the first electronic computers",
        "the internet and World Wide Web origins",
        "medical inventions: vaccines, X-rays, antibiotics, MRI",
        "photography: Daguerre, Eastman, and the camera revolution",
        "the cotton gin, sewing machine, and textile inventions",
        "weapons and military inventions that changed warfare",
        "everyday inventions: zipper, velcro, post-it notes, microwave",
        "patent wars and disputed inventions throughout history",
    ],
    "Reality TV": [
        "Survivor: seasons, winners, and iconic moments",
        "The Bachelor and Bachelorette franchise history",
        "American Idol and singing competition shows",
        "The Real World and the birth of modern reality TV",
        "cooking competitions: MasterChef, Top Chef, Chopped",
        "Keeping Up with the Kardashians and celebrity reality TV",
        "The Amazing Race and travel-based competition shows",
        "Big Brother formats around the world",
        "RuPaul's Drag Race and its cultural impact",
        "home renovation shows: Fixer Upper, Property Brothers",
        "dating shows beyond The Bachelor: Love Island, Too Hot to Handle",
        "talent shows: America's Got Talent, The X Factor",
        "reality TV controversies and behind-the-scenes drama",
        "Shark Tank, Dragon's Den, and business pitch shows",
        "competition reality: Project Runway, The Voice, Dancing with the Stars",
    ],
    "Volcanoes & Earthquakes": [
        "famous volcanic eruptions: Vesuvius, Krakatoa, Mt. St. Helens",
        "plate tectonics and how volcanoes form",
        "the Ring of Fire and Pacific volcanic zones",
        "earthquake magnitude scales: Richter, moment magnitude",
        "the San Andreas Fault and California earthquake history",
        "tsunamis: causes, famous events, and early warning systems",
        "volcanic types: shield, stratovolcano, cinder cone, caldera",
        "supervolcanoes: Yellowstone, Toba, and catastrophic eruptions",
        "famous earthquakes: San Francisco 1906, Haiti 2010, Japan 2011",
        "volcanology as a science: monitoring and prediction",
        "volcanic islands and hotspot geology: Hawaii, Iceland, Canary Islands",
        "the year without a summer: Tambora 1815",
        "geysers, hot springs, and hydrothermal features",
        "earthquake engineering and building codes",
        "volcanic hazards: lahars, pyroclastic flows, ash clouds",
    ],
    "Stand-Up Comedy": [
        "stand-up pioneers: Lenny Bruce, Richard Pryor, George Carlin",
        "comedy specials that changed the art form",
        "Saturday Night Live alumni and their stand-up roots",
        "women in stand-up: Joan Rivers, Ellen, Ali Wong, Hannah Gadsby",
        "comedy clubs and the comedy circuit: the Store, the Cellar, the Improv",
        "roast comedy and insult humor tradition",
        "observational comedy: Seinfeld, Gaffigan, Mulaney",
        "alternative comedy and anti-humor",
        "stand-up on Netflix and the streaming special boom",
        "British stand-up and panel show culture",
        "stand-up controversies and the limits of comedy",
        "comedy albums and the vinyl era of stand-up",
        "improvisational comedy: Second City, UCB, Whose Line",
        "political comedy and satirists: Stewart, Colbert, Hasan Minhaj",
        "international stand-up scenes and comedy festivals",
    ],
    "Pirates & Smugglers": [
        "the Golden Age of Piracy: 1650-1730",
        "Blackbeard: Edward Teach's reign of terror",
        "Captain Kidd and the line between privateer and pirate",
        "female pirates: Anne Bonny and Mary Read",
        "the Barbary corsairs and Mediterranean piracy",
        "pirate ships: galleons, sloops, and the Jolly Roger",
        "treasure and buried loot: fact vs fiction",
        "pirate codes, articles, and surprisingly democratic governance",
        "the East India Company and colonial-era smuggling",
        "Caribbean pirate havens: Port Royal, Nassau, Tortuga",
        "Chinese pirates: Zheng Yi Sao and the Red Flag Fleet",
        "Vikings as pirates and raiders",
        "rum running and Prohibition-era smuggling",
        "modern piracy: Somali pirates and shipping lane security",
        "pirates in popular culture: Treasure Island to Pirates of the Caribbean",
    ],
    "Olympic Games": [
        "ancient Greek Olympics and their traditions",
        "the modern Olympics revival: Pierre de Coubertin and Athens 1896",
        "iconic Olympic moments and performances",
        "Olympic host cities and their signature venues",
        "Winter Olympics history and landmark events",
        "Olympic controversies: boycotts, doping, scandals",
        "track and field records and legends",
        "Olympic gymnastics: Nadia Comaneci, Simone Biles, and perfect 10s",
        "Olympic swimming: Spitz, Phelps, Ledecky",
        "the Olympic medal table and country rivalries",
        "unusual and discontinued Olympic sports",
        "Paralympic Games history and inspiring athletes",
        "Olympic opening and closing ceremonies",
        "Team USA and dominant national programs",
        "the Olympic motto, symbols, flame relay, and traditions",
    ],
    "Candy & Chocolate": [
        "the history of chocolate from Mesoamerica to Europe",
        "iconic candy bars and their origin stories",
        "Hershey, Cadbury, and Nestlé: the big chocolate empires",
        "artisan and craft chocolate: bean-to-bar movement",
        "classic American candy: Tootsie Roll, Necco, PEZ, Skittles",
        "British sweets and confectionery traditions",
        "Japanese candy culture: Kit Kat flavors, Pocky, wagashi",
        "gummy bears, jelly beans, and gelatin-based candy history",
        "candy-making techniques: tempering, pulling, spinning sugar",
        "Halloween candy traditions and trick-or-treating history",
        "chocolate factories and Willy Wonka in popular culture",
        "licorice, marzipan, and European confectionery traditions",
        "candy packaging, marketing, and collectible wrappers",
        "sugar science: how candy is made and why it tastes good",
        "record-setting candy: largest chocolate bar, most expensive bonbon",
    ],
}

# Fallback list for any category not explicitly mapped
_DEFAULT_SUBCATEGORIES = [
    "fundamental concepts and key facts",
    "famous people and their contributions",
    "major events and milestones",
    "records and firsts",
    "cultural impact and legacy",
]

# Legacy alias kept for any code that still references it directly
ART_LIT_SUBCATEGORIES = CATEGORY_SUBCATEGORIES["Arts & Literature"]


def get_subcategories(category: str) -> list[str]:
    """Return subcategory list for a category, falling back to defaults."""
    return CATEGORY_SUBCATEGORIES.get(category, _DEFAULT_SUBCATEGORIES)


async def generate_batch(
    api_key: str,
    subcategory: str,
    difficulty: str,
    count: int = 10,
    client: httpx.AsyncClient | None = None,
    category: str = "General Knowledge",
) -> list[dict]:
    """Generate a batch of trivia questions for a specific subcategory and category."""
    difficulty_guidance = {
        "easy": "Questions should be common knowledge that most people would know",
        "medium": "Questions should require some specific knowledge but not be obscure",
        "hard": "Questions should be challenging and require specialized knowledge",
    }
    guidance = difficulty_guidance.get(difficulty, difficulty_guidance["medium"])

    prompt = (
        f"Generate {count} unique trivia questions about {subcategory} "
        f"(within the {category} category) at {difficulty} difficulty level.\n\n"
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
        f"- For {difficulty} difficulty: {guidance}\n"
        "- Do NOT repeat extremely common trivia questions\n"
        "- Return ONLY the JSON array, no other text"
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are a trivia question generator specializing in {category}. "
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
        return _parse_response(content, subcategory, difficulty, category)
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


async def verify_question(
    api_key: str,
    question: str,
    correct_answer: str,
    incorrect_answers: list[str],
    client: httpx.AsyncClient,
) -> bool:
    """Ask a second LLM call to verify the question is factually correct.

    Returns True if verified, False if incorrect or uncertain.
    Uses low temperature for a deterministic factual check.
    """
    choices_text = "\n".join(f"  - {a}" for a in incorrect_answers)
    prompt = (
        f"Question: {question}\n"
        f"Stated correct answer: {correct_answer}\n"
        f"Incorrect answers: \n{choices_text}\n\n"
        "Is the stated correct answer actually correct and factually accurate?\n"
        "Also check: are any of the 'incorrect' answers actually also correct?\n"
        "Reply with exactly one word: YES, NO, or UNCERTAIN."
    )
    payload = {
        "model": _MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict fact-checker for trivia questions. "
                    "Reply only with YES, NO, or UNCERTAIN — nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(_OPENAI_URL, json=payload, headers=headers)
        resp.raise_for_status()
        verdict = resp.json()["choices"][0]["message"]["content"].strip().upper()
        return verdict.startswith("YES")
    except Exception as exc:
        logger.warning("Verification call failed (%s) — accepting question", exc)
        return True  # fail open so a network blip doesn't discard good questions


async def verify_batch(
    api_key: str,
    questions: list[dict],
    client: httpx.AsyncClient,
    concurrency: int = 10,
) -> tuple[list[dict], int]:
    """Verify a batch of questions concurrently. Returns (accepted, rejected_count)."""
    sem = asyncio.Semaphore(concurrency)

    async def _verify_one(q: dict) -> dict | None:
        incorrect = [c["text"] for c in q["choices"] if not c["isCorrect"]]
        async with sem:
            ok = await verify_question(api_key, q["question"], q["correct_answer"], incorrect, client)
        return q if ok else None

    results = await asyncio.gather(*[_verify_one(q) for q in questions])
    accepted = [r for r in results if r is not None]
    rejected = len(questions) - len(accepted)
    return accepted, rejected


def _parse_response(content: str, subcategory: str, difficulty: str, category: str = "General Knowledge") -> list[dict]:
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
        if isinstance(correct_answer, list):
            correct_answer = correct_answer[0] if correct_answer else ""
        correct_answer = str(correct_answer)
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
            "category": category,
            "subcategory": subcategory,
            "difficulty": difficulty,
            "choices": choices,
            "correct_index": correct_index,
            "explanation": q.get("explanation", ""),
            "hint": q.get("hint", ""),
        })

    return results


# ---------------------------------------------------------------------------
# DB-level trgm dedup (requires pg_trgm extension + GIN index)
# ---------------------------------------------------------------------------

async def is_db_duplicate(pool: asyncpg.Pool, question: str, threshold: float = 0.65) -> bool:
    """Check if question has a similar match in the DB using the pg_trgm GIN index.

    O(log n) — vastly faster than the Python in-memory scan for large corpora.
    Requires: CREATE EXTENSION pg_trgm; and GIN index on cards.question.
    Falls back gracefully if the extension is not present.
    """
    try:
        async with pool.acquire(timeout=10) as conn:
            # SET threshold on this connection — it's session-local and pool
            # connections don't inherit it from ensure_trgm_threshold()
            await conn.execute(
                f"SET pg_trgm.similarity_threshold = {float(threshold):.6f}",
                timeout=5,
            )
            row = await conn.fetchrow(
                "SELECT id FROM cards WHERE question % $1 LIMIT 1",
                question,
                timeout=5,
            )
        return row is not None
    except Exception:
        return False


async def ensure_trgm_threshold(pool: asyncpg.Pool, threshold: float) -> None:
    """Set pg_trgm similarity threshold for this session."""
    try:
        # Use a single connection to set the threshold
        async with pool.acquire() as conn:
            await conn.execute(f"SET pg_trgm.similarity_threshold = {float(threshold):.6f}")
    except Exception:
        pass  # Extension may not be installed; fall back to Python check


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
    parser.add_argument("--no-verify", action="store_true", help="Skip verification pass (faster, less accurate)")
    parser.add_argument("--dedup-only", action="store_true", help="Only scan for duplicates, no generation")
    parser.add_argument("--delete-dupes", action="store_true", help="Delete found duplicates (with --dedup-only)")
    args = parser.parse_args()

    # Use same env var pattern as server/db.py
    async def _init_conn(conn):
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    # Support explicit keyword params to avoid URL-parsing issues with special chars in passwords
    db_host = os.environ.get("CE_DATABASE_HOST", "")
    if db_host:
        # ssl=False for local Fly proxy; direct connections can override via CE_DATABASE_SSL
        use_ssl = os.environ.get("CE_DATABASE_SSL", "false").lower() in ("true", "1", "yes")
        pool = await asyncpg.create_pool(
            host=db_host,
            port=int(os.environ.get("CE_DATABASE_PORT", "5432")),
            user=os.environ.get("CE_DATABASE_USER", ""),
            password=os.environ.get("CE_DATABASE_PASSWORD", ""),
            database=os.environ.get("CE_DATABASE_NAME", "card_engine"),
            ssl="require" if use_ssl else False,
            init=_init_conn,
        )
        logger.info("Connected via CE_DATABASE_HOST=%s:%s", db_host, os.environ.get("CE_DATABASE_PORT", "5432"))
    else:
        db_url = os.environ.get("CE_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
        if not db_url:
            db_url = "postgresql://billdonner@localhost:5432/card_engine"
            logger.info("No DATABASE_URL set, using default: %s", db_url)
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

    search_categories = [target_category]

    # Check whether the pg_trgm GIN index exists for DB-level dedup
    trgm_index_exists = await pool.fetchval(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_cards_question_trgm'"
    )
    use_db_dedup = bool(trgm_index_exists)

    if use_db_dedup:
        logger.info("pg_trgm GIN index found — using DB-level dedup (O(log n))")
        await ensure_trgm_threshold(pool, 0.65)
        existing = []  # Not needed for DB dedup
    else:
        # Load existing questions for Python in-memory dedup
        existing = await load_existing_questions(pool, search_categories)
        logger.info("Loaded %d existing questions from %s for dedup (Python)", len(existing), search_categories)

    # Get or create deck
    deck_row = await pool.fetchrow(
        "SELECT id FROM decks WHERE kind = 'trivia' AND title = $1", target_category
    )
    if deck_row:
        deck_id = deck_row["id"]
    else:
        deck_id = uuid.uuid4()
        category_icons = {
            "Arts & Literature": "paintbrush",
            "General Knowledge": "lightbulb",
            "History": "scroll",
            "Politics": "building.columns",
            "Pop Culture": "star",
            "Literature": "book",
            "Geography": "globe",
            "Music": "music.note",
            "Mythology": "sparkles",
            "Mathematics": "function",
            "Science & Nature": "atom",
            "Sports": "sportscourt",
        }
        icon = category_icons.get(target_category, "questionmark.circle")
        await pool.execute(
            "INSERT INTO decks (id, title, kind, properties, tier) "
            "VALUES ($1, $2, 'trivia'::deck_kind, $3, 'free'::deck_tier)",
            deck_id, target_category, {"pic": icon},
        )

    # Get source provider
    source_row = await pool.fetchrow("SELECT id FROM source_providers WHERE name = 'openai'")
    source_id = source_row["id"] if source_row else None

    # Generate in batches
    total_generated = 0
    total_inserted = 0
    total_dupes = 0
    total_rejected = 0
    batch_num = 0
    difficulties = ["easy", "medium", "hard"]
    skip_verify = getattr(args, "no_verify", False)

    async with httpx.AsyncClient(timeout=60.0) as client:
        while total_inserted < target_count:
            remaining = target_count - total_inserted
            batch_num += 1

            # Pick random subcategories for diversity
            subcategory_list = get_subcategories(target_category)
            subcats = random.sample(
                subcategory_list,
                min(args.concurrent, len(subcategory_list)),
            )

            logger.info(
                "Batch %d: generating %d questions across %d subcategories (need %d more)",
                batch_num, args.batch_size * len(subcats), len(subcats), remaining,
            )

            tasks = []
            for subcat in subcats:
                diff = random.choice(difficulties)
                tasks.append(
                    generate_batch(api_key, subcat, diff, args.batch_size, client, target_category)
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_questions = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Batch failed: %s", result)
                    continue
                batch_questions.extend(result)

            total_generated += len(batch_questions)

            # Verification pass — drop factually incorrect/uncertain questions
            if not skip_verify and batch_questions:
                batch_questions, rejected = await verify_batch(api_key, batch_questions, client)
                total_rejected += rejected
                if rejected:
                    logger.info("Verification: rejected %d/%d questions in this batch",
                                rejected, rejected + len(batch_questions))

            # Dedup and insert
            for q in batch_questions:
                if total_inserted >= target_count:
                    break

                if use_db_dedup:
                    # DB trgm check: O(log n) via GIN index
                    is_dupe = await is_db_duplicate(pool, q["question"])
                else:
                    # Python in-memory check: O(n)
                    is_dupe = bool(is_fuzzy_duplicate(
                        q["question"], q["correct_answer"], existing,
                    ))

                if is_dupe:
                    total_dupes += 1
                    continue

                if not args.dry_run:
                    card_id = await insert_card(pool, q, deck_id, source_id)
                    logger.debug("Inserted card %s", card_id)

                if not use_db_dedup:
                    # Add to existing corpus for future Python dedup
                    existing.append({
                        "id": str(uuid.uuid4()),
                        "question": q["question"],
                        "correct_answer": q["correct_answer"],
                        "category": target_category,
                    })
                total_inserted += 1

            logger.info(
                "Progress: %d/%d inserted (%d generated, %d dupes skipped, %d rejected)",
                total_inserted, target_count, total_generated, total_dupes, total_rejected,
            )

            # Stop if rejection rate (dupes + veracity rejections) hits 50% —
            # indicates the AI is exhausting unique content for this category
            total_rejections = total_dupes + total_rejected
            if total_generated >= 50 and total_rejections / total_generated >= 0.50:
                logger.warning(
                    "STOPPING: rejection rate %.0f%% >= 50%% after %d generated — "
                    "AI likely exhausted unique content for '%s'",
                    100 * total_rejections / total_generated,
                    total_generated,
                    target_category,
                )
                break

            # Small delay between batches to avoid rate limiting
            if total_inserted < target_count:
                await asyncio.sleep(2)

    print(f"\n{'='*80}")
    print(f"Generation complete!")
    print(f"  Target:     {target_count}")
    print(f"  Generated:  {total_generated}")
    print(f"  Verified:   {total_generated - total_rejected} passed, {total_rejected} rejected")
    print(f"  Inserted:   {total_inserted}")
    print(f"  Duplicates: {total_dupes}")
    print(f"  Batches:    {batch_num}")
    if args.dry_run:
        print(f"  (DRY RUN — nothing written to DB)")
    if getattr(args, "no_verify", False):
        print(f"  (VERIFICATION SKIPPED)")
    print(f"{'='*80}")


if __name__ == "__main__":
    asyncio.run(main())
