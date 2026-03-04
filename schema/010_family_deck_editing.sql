-- Migration 010: Family deck editing — track questions a family wants excluded
-- When a parent removes a card from a generated deck, the question is stored
-- here so it is skipped if the deck is regenerated.

CREATE TABLE family_card_exclusions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    excluded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (family_id, question)
);
CREATE INDEX idx_family_card_exclusions_family ON family_card_exclusions(family_id);
