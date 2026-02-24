-- card-engine unified schema
-- Merges OBO (flashcards) and Alities (trivia) into one content store.
--
-- Design principles:
--   - UUID primary keys (consistent with alities, portable across databases)
--   - JSONB properties for domain-specific fields (flashcard answer, trivia choices, etc.)
--   - Explicit source tracking for multi-provider ingestion pipeline
--   - kind column on decks separates content types without separate tables
--   - Timestamptz everywhere (consistent with alities)

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE deck_kind AS ENUM ('flashcard', 'trivia', 'newsquiz');
CREATE TYPE source_type AS ENUM ('api', 'rss', 'import', 'manual');
CREATE TYPE difficulty AS ENUM ('easy', 'medium', 'hard');

-- ---------------------------------------------------------------------------
-- Source providers — tracks where content comes from
-- ---------------------------------------------------------------------------
-- Replaces: alities question_sources table
-- Adds:     feed_url for RSS providers, schedule cadence

CREATE TABLE source_providers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,           -- 'openai', 'rss:bbc-kids', 'import:csv'
    type        source_type NOT NULL,
    feed_url    TEXT,                           -- RSS/Atom URL (null for API/import)
    is_active   BOOLEAN NOT NULL DEFAULT true,
    config      JSONB NOT NULL DEFAULT '{}',   -- provider-specific settings (model, prompt template, etc.)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Decks — content collections
-- ---------------------------------------------------------------------------
-- Replaces: OBO decks table + alities categories table
--
-- OBO mapping:
--   topic      → title
--   age_range  → properties->>'age_range'
--   voice      → properties->>'voice'
--   card_count → card_count (denormalized, same as OBO)
--
-- Alities mapping:
--   categories.name        → title
--   categories.description → properties->>'description'
--   categories.choice_count → properties->>'choice_count'
--   (category pic/icon)    → properties->>'pic'

CREATE TABLE decks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL,
    kind        deck_kind NOT NULL,
    properties  JSONB NOT NULL DEFAULT '{}',
    card_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Cards — individual content items
-- ---------------------------------------------------------------------------
-- Replaces: OBO cards table + alities questions table
--
-- The question column is universal — every card has a question/prompt.
-- Everything else lives in properties JSONB, keyed by kind:
--
-- Flashcard properties:
--   { "answer": "The Sun" }
--
-- Trivia properties:
--   {
--     "choices": [
--       {"text": "Mars", "isCorrect": false},
--       {"text": "The Sun", "isCorrect": true},
--       {"text": "Jupiter", "isCorrect": false},
--       {"text": "The Moon", "isCorrect": false}
--     ],
--     "correct_index": 1,
--     "explanation": "The Sun is the center of our solar system.",
--     "hint": "It's very bright",
--     "difficulty": "easy"
--   }
--
-- Newsquiz properties:
--   {
--     "choices": [...],
--     "correct_index": 0,
--     "explanation": "...",
--     "article_title": "NASA Launches New Probe",
--     "article_url": "https://...",
--     "published_at": "2026-02-23T..."
--   }

CREATE TABLE cards (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deck_id         UUID NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    question        TEXT NOT NULL,
    properties      JSONB NOT NULL DEFAULT '{}',
    difficulty      difficulty NOT NULL DEFAULT 'medium',
    source_id       UUID REFERENCES source_providers(id),
    source_url      TEXT,                      -- attribution link (article URL, API ref)
    source_date     TIMESTAMPTZ,               -- when the source content was published
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Source runs — audit log for ingestion pipeline
-- ---------------------------------------------------------------------------

CREATE TABLE source_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     UUID NOT NULL REFERENCES source_providers(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    items_fetched   INTEGER NOT NULL DEFAULT 0,
    items_added     INTEGER NOT NULL DEFAULT 0,
    items_skipped   INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Deck lookups by kind (flashcard vs trivia vs newsquiz)
CREATE INDEX idx_decks_kind ON decks(kind);

-- Card lookups by deck (the primary access pattern)
CREATE INDEX idx_cards_deck_id ON cards(deck_id);

-- Card ordering within a deck
CREATE INDEX idx_cards_deck_position ON cards(deck_id, position);

-- Filter decks by age_range (flashcards) via JSONB
CREATE INDEX idx_decks_age_range ON decks((properties->>'age_range'))
    WHERE properties->>'age_range' IS NOT NULL;

-- Filter cards by difficulty
CREATE INDEX idx_cards_difficulty ON cards(difficulty);

-- Source run history
CREATE INDEX idx_source_runs_provider ON source_runs(provider_id);

-- Source run completion time (for monitoring recent runs)
CREATE INDEX idx_source_runs_finished ON source_runs(finished_at DESC);

-- ---------------------------------------------------------------------------
-- Helper: update card_count trigger
-- ---------------------------------------------------------------------------
-- Keeps decks.card_count in sync automatically (OBO maintained this manually).

CREATE OR REPLACE FUNCTION update_deck_card_count() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        UPDATE decks SET card_count = (
            SELECT COUNT(*) FROM cards WHERE deck_id = NEW.deck_id
        ), updated_at = now()
        WHERE id = NEW.deck_id;
    END IF;
    IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
        UPDATE decks SET card_count = (
            SELECT COUNT(*) FROM cards WHERE deck_id = OLD.deck_id
        ), updated_at = now()
        WHERE id = OLD.deck_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_card_count
    AFTER INSERT OR UPDATE OF deck_id OR DELETE ON cards
    FOR EACH ROW EXECUTE FUNCTION update_deck_card_count();

-- ---------------------------------------------------------------------------
-- Helper: update updated_at trigger
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_decks_updated_at
    BEFORE UPDATE ON decks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_providers_updated_at
    BEFORE UPDATE ON source_providers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
