-- 006: Add tier column to decks for content segmentation (free/family/premium)
CREATE TYPE deck_tier AS ENUM ('free', 'family', 'premium');
ALTER TABLE decks ADD COLUMN tier deck_tier NOT NULL DEFAULT 'free';
UPDATE decks SET tier = 'family' WHERE properties ? 'family_id';
CREATE INDEX idx_decks_tier ON decks (tier);
