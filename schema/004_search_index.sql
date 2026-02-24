-- GIN index for full-text search on cards.question
CREATE INDEX IF NOT EXISTS idx_cards_question_fts
    ON cards USING GIN (to_tsvector('english', question));
