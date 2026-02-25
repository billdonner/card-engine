-- Family tree tables for personalized flashcard/trivia generation.
--
-- Allows users to describe family members, build a relationship graph,
-- and generate decks per child player.  Generated decks land in the
-- existing decks + cards tables so obo-ios and alities-mobile consume
-- them through existing adapters with no changes.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE relationship_type AS ENUM ('married', 'parent_of', 'divorced');
CREATE TYPE person_status AS ENUM ('living', 'deceased');

-- ---------------------------------------------------------------------------
-- Families
-- ---------------------------------------------------------------------------

CREATE TABLE families (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_families_updated_at
    BEFORE UPDATE ON families
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Family people
-- ---------------------------------------------------------------------------

CREATE TABLE family_people (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    nickname    TEXT,
    maiden_name TEXT,
    born        INTEGER,                          -- birth year
    status      person_status NOT NULL DEFAULT 'living',
    notes       TEXT,
    player      BOOLEAN NOT NULL DEFAULT false,   -- is this person a child player?
    placeholder BOOLEAN NOT NULL DEFAULT false,   -- incomplete record needing more info
    photo_url   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_family_people_family ON family_people(family_id);

CREATE TRIGGER trg_family_people_updated_at
    BEFORE UPDATE ON family_people
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Family relationships
-- ---------------------------------------------------------------------------

CREATE TABLE family_relationships (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    type        relationship_type NOT NULL,
    from_id     UUID NOT NULL REFERENCES family_people(id) ON DELETE CASCADE,
    to_id       UUID NOT NULL REFERENCES family_people(id) ON DELETE CASCADE,
    year        INTEGER,                          -- year relationship started
    ended       BOOLEAN NOT NULL DEFAULT false,
    end_reason  TEXT,                             -- 'divorce', 'death', etc.
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_family_relationships_family ON family_relationships(family_id);
CREATE INDEX idx_family_relationships_from   ON family_relationships(from_id);
CREATE INDEX idx_family_relationships_to     ON family_relationships(to_id);

-- ---------------------------------------------------------------------------
-- Family chat sessions — conversational tree builder
-- ---------------------------------------------------------------------------

CREATE TABLE family_chat_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    messages    JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_family_chat_sessions_family ON family_chat_sessions(family_id);

CREATE TRIGGER trg_family_chat_sessions_updated_at
    BEFORE UPDATE ON family_chat_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
