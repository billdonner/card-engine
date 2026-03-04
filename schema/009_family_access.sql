-- Migration 009: Family access control via device-based player membership
-- Adds family_members (ownership/membership) and family_invites (invite codes)

CREATE TABLE family_members (
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    player_id   UUID NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member',  -- 'owner' or 'member'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (family_id, player_id)
);
CREATE INDEX idx_family_members_player ON family_members(player_id);

CREATE TABLE family_invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    invite_code TEXT NOT NULL UNIQUE,
    created_by  UUID NOT NULL REFERENCES players(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_family_invites_code ON family_invites(invite_code);
