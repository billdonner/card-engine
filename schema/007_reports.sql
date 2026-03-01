-- Question reports / feedback from client apps
CREATE TABLE question_reports (
    id            BIGSERIAL PRIMARY KEY,
    app_id        TEXT NOT NULL,
    challenge_id  TEXT NOT NULL,
    topic         TEXT,
    question_text TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT 'inaccurate',
    detail        TEXT,
    reported_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reports_app ON question_reports(app_id);
CREATE INDEX idx_reports_challenge ON question_reports(challenge_id);
