-- Migrate existing OBO data into the unified card-engine schema.
-- Run this against a card-engine database that already has 001_initial.sql applied.
-- Assumes obo source database is accessible via dblink or foreign data wrapper,
-- OR that this is run as a one-time ETL script with values substituted.
--
-- This file documents the exact mapping from OBO → card-engine.

-- Step 1: Create the OBO source provider
INSERT INTO source_providers (name, type, config) VALUES
    ('obo-gen', 'api', '{"model": "gpt-4o-mini", "origin": "obo-gen CLI"}');

-- Step 2: Migrate decks
-- OBO: decks(id SERIAL, topic, age_range, voice, card_count, created_at)
-- card-engine: decks(id UUID, title, kind, properties JSONB, card_count, created_at)
--
-- INSERT INTO decks (title, kind, properties, card_count, created_at)
-- SELECT
--     topic,
--     'flashcard',
--     jsonb_build_object(
--         'age_range', age_range,
--         'voice', voice
--     ),
--     card_count,
--     created_at
-- FROM obo_source.decks;

-- Step 3: Migrate cards
-- OBO: cards(id SERIAL, deck_id, position, question, answer)
-- card-engine: cards(id UUID, deck_id UUID, position, question, properties JSONB, ...)
--
-- INSERT INTO cards (deck_id, position, question, properties, difficulty, source_id)
-- SELECT
--     deck_id_mapping.new_id,          -- mapped from old SERIAL to new UUID
--     c.position,
--     c.question,
--     jsonb_build_object('answer', c.answer),
--     'medium',                         -- OBO has no difficulty; default to medium
--     (SELECT id FROM source_providers WHERE name = 'obo-gen')
-- FROM obo_source.cards c
-- JOIN deck_id_mapping ON deck_id_mapping.old_id = c.deck_id;
