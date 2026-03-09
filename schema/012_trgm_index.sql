-- Migration 012: pg_trgm GIN index for fast fuzzy dedup
-- Enables trigram similarity queries on cards.question
-- Makes per-question dedup O(log n) instead of O(n) during generation
-- Makes full-corpus dedup O(n log n) instead of O(n²)

-- Enable pg_trgm extension (requires superuser; run as postgres role)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN index for fast trigram similarity on question text
-- Accelerates the % operator (similarity >= pg_trgm.similarity_threshold)
-- Used by: bulk_generate.py, dedup_trgm.py
CREATE INDEX IF NOT EXISTS idx_cards_question_trgm
    ON cards USING GIN (question gin_trgm_ops);
