-- 008_players_sessions.sql
-- Anonymous players, seen-card tracking, and shareable sessions.

-- players: anonymous device-based identity
CREATE TABLE IF NOT EXISTS players (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    TEXT NOT NULL UNIQUE,
    display_name TEXT,
    properties   JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- player_card_history: which cards a player has been served
CREATE TABLE IF NOT EXISTS player_card_history (
    player_id UUID NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    card_id   UUID NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    app_id    TEXT NOT NULL DEFAULT 'qross',
    seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, card_id)
);

-- sessions: a dealt hand of cards, shareable via short code
CREATE TABLE IF NOT EXISTS sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id  UUID NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    share_code TEXT UNIQUE,
    app_id     TEXT NOT NULL DEFAULT 'qross',
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session_cards: ordered list of cards in a session
CREATE TABLE IF NOT EXISTS session_cards (
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    card_id    UUID NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    PRIMARY KEY (session_id, card_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_players_device_id ON players(device_id);
CREATE INDEX IF NOT EXISTS idx_player_card_history_player_app ON player_card_history(player_id, app_id);
CREATE INDEX IF NOT EXISTS idx_sessions_player_id ON sessions(player_id);
CREATE INDEX IF NOT EXISTS idx_sessions_share_code ON sessions(share_code);
