-- 011_quarantine.sql — Add quarantine support for quality control
-- Cards can be quarantined (hidden from gamedata) with a reason.

ALTER TABLE cards ADD COLUMN IF NOT EXISTS quarantined BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_cards_quarantined ON cards (quarantined) WHERE quarantined = TRUE;
