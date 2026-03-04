# card-engine — Unified Content Backend

Shared backend for Flasherz (flashcards) and Alities (trivia) apps.

## Architecture

Three-layer design:

| Layer | Purpose | Location |
|-------|---------|----------|
| **Layer 1 — Card Store** | Generic decks + cards with JSONB properties | `schema/`, `server/db.py` |
| **Layer 2 — Domain Adapters** | Flashcard and trivia API endpoints | `server/adapters/` |
| **Layer 3 — Game Apps** | Flasherz iOS, Alities iOS/web | External repos |

## Ingestion Pipeline

Trivia question generation daemon ported from alities-engine. Runs as an async background task inside the FastAPI process.

| Provider | Type | Status |
|----------|------|--------|
| OpenAI (gpt-4o-mini) | API | **Active** — generates trivia via chat completions |
| RSS/Atom feeds | RSS | Planned |
| CSV/JSON import | Import | Planned |
| obo-gen CLI | API | Planned — port from obo-gen |

### How It Works

1. Daemon cycles through 20 canonical trivia categories (shuffled)
2. Generates questions via GPT-4o-mini in concurrent batches
3. Deduplicates: signature cache (O(1)) then Jaccard similarity (threshold 0.85)
4. Inserts into `cards` table with `deck.kind='trivia'`, creating decks per category
5. Logs each cycle as a `source_runs` row for audit
6. Sleeps `CE_INGEST_CYCLE_SECONDS` then repeats

### Ingestion Env Vars

| Variable | Default | Description |
|----------|---------|-------------|
| `CE_OPENAI_API_KEY` | (required) | OpenAI API key for question generation |
| `CE_INGEST_CYCLE_SECONDS` | `60` | Sleep between ingestion cycles |
| `CE_INGEST_BATCH_SIZE` | `10` | Questions per category per batch |
| `CE_INGEST_AUTO_START` | `false` | Auto-start daemon on server boot |
| `CE_INGEST_CONCURRENT_BATCHES` | `5` | Parallel OpenAI requests per cycle |

## Database

PostgreSQL with the unified schema in `schema/001_initial.sql`.

- `decks` — content collections (kind: flashcard, trivia, newsquiz)
- `cards` — content items with JSONB properties
- `source_providers` — tracks ingestion sources
- `source_runs` — audit log for pipeline runs
- `players` — anonymous device-based player identity (schema 008)
- `player_card_history` — tracks which cards each player has been served (schema 008)
- `sessions` — dealt hand of cards with shareable 6-char code (schema 008)
- `session_cards` — ordered card list within a session (schema 008)

## Related Repos

| Repo | Path | Relationship |
|------|------|-------------|
| obo (hub) | `~/obo` | Planning docs |
| obo-server | `~/obo-server` | Retired — replaced by this |
| obo-gen | `~/obo-gen` | Swift CLI tool, writes decks to card-engine DB |
| obo-ios | `~/obo-ios` | Flashcard app (consumes `/api/v1/flashcards`) |
| flasherz-ios | `~/flasherz-ios` | Future flashcard app (will consume card-engine API) |
| alities-engine | `~/alities-engine` | Retired — ingestion pipeline ported here |
| alities-mobile | `~/alities-mobile` | Trivia app (consumes `/api/v1/trivia` + `/api/v1/ingestion`) |

## Server

FastAPI app in `server/` — single-process, asyncpg connection pool, Pydantic models.

### Commands

```bash
# Apply schema
psql -d card_engine -f schema/001_initial.sql

# Run server (dev with reload)
cd ~/card-engine && python3.11 -m uvicorn server.app:app --port 9810 --reload

# Run server (production)
cd ~/card-engine && python3.11 -m uvicorn server.app:app --port 9810

# Install dependencies
cd ~/card-engine && pip install -e ".[dev]"
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CE_DATABASE_URL` | built from parts | Full Postgres DSN (overrides individual vars) |
| `CE_DB_HOST` | `localhost` | Database host |
| `CE_DB_PORT` | `5432` | Database port |
| `CE_DB_USER` | `postgres` | Database user |
| `CE_DB_PASSWORD` | `postgres` | Database password |
| `CE_DB_NAME` | `card_engine` | Database name |
| `CE_PORT` | `9810` | Server listen port |

### Embedded cardz-studio

The cardz-studio React SPA is built in a multi-stage Docker build and served at `/studio`. In production: `https://bd-cardzerver.fly.dev/studio`. The SPA uses relative API paths (`/api/v1/...`) so it works without any proxy config.

Deploy builds both Python backend and React frontend: `~/Flyz/scripts/deploy.sh card-engine`

### Port

**9810** (inherits from obo-server slot in the port registry).

### API Endpoints

#### Core

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | DB connectivity check |
| GET | `/metrics` | Deck/card/source counts for server-monitor |

#### Generic (Layer 1)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/decks` | List decks — filters: `kind`, `age`, `limit`, `offset` |
| GET | `/api/v1/decks/{id}` | Single deck with all cards |

#### Flashcard Adapter (Layer 2) — backward-compatible with obo-ios

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/flashcards` | All flashcard decks with cards in one bulk call |
| GET | `/api/v1/flashcards/{id}` | Single flashcard deck with cards |

Response fields map to obo-ios expectations: `topic`, `age_range`, `voice`, `answer`.

#### Trivia Adapter (Layer 2) — backward-compatible with alities-mobile

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/trivia/gamedata` | Bulk export in alities Challenge format |
| GET | `/api/v1/trivia/categories` | Categories with counts + SF Symbol pics |

Response fields map to alities-mobile expectations: `answers`, `correct`, `explanation`, `hint`, `pic`.

When `player_id` query param is provided to `/api/v1/trivia/gamedata`, response adds `session_id`, `share_code`, `fresh_count`, `total_available`. Previously seen cards are excluded.

#### Players & Sessions (Layer 2) — device identity and session sharing

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/players` | Register/upsert player by `device_id` |
| GET | `/api/v1/players/{id}/stats` | Seen count, per-category breakdown |
| POST | `/api/v1/players/{id}/reset` | Clear seen-card history |
| GET | `/api/v1/sessions/{share_code}` | Replay a shared session (same `GameDataOut` shape) |

#### Question Reports (Layer 2) — cross-app feedback

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/reports` | Submit a question report (any client app) |
| GET | `/api/v1/reports` | List reports — optional `?app_id=` filter (admin) |

Request body for POST: `{ "app_id": "qross", "challenge_id": "...", "question_text": "...", "reason": "inaccurate", "topic": "...", "detail": "..." }`

#### Ingestion Control (Layer 2) — daemon management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/ingestion/status` | Daemon state, stats, and config |
| POST | `/api/v1/ingestion/start` | Start the ingestion daemon |
| POST | `/api/v1/ingestion/stop` | Stop the ingestion daemon |
| POST | `/api/v1/ingestion/pause` | Pause (finish current batch, then sleep) |
| POST | `/api/v1/ingestion/resume` | Resume from paused state |
| GET | `/api/v1/ingestion/runs` | Recent source_run audit log |

#### AI Difficulty Scoring (Layer 2) — batch scoring

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/difficulty/status` | Scorer state, stats, scored/unscored counts |
| POST | `/api/v1/difficulty/start` | Start batch scoring job |
| POST | `/api/v1/difficulty/stop` | Stop scoring job |

### Key Files

| File | Purpose |
|------|---------|
| `server/app.py` | FastAPI app, lifespan, CORS, /health, /metrics |
| `server/db.py` | asyncpg pool management, query helpers |
| `server/models.py` | All Pydantic request/response models |
| `server/adapters/generic.py` | `/api/v1/decks/*` routes |
| `server/adapters/flashcards.py` | `/api/v1/flashcards/*` routes |
| `server/adapters/trivia.py` | `/api/v1/trivia/*` routes (with player-aware exclusion) |
| `server/adapters/players.py` | `/api/v1/players/*` + `/api/v1/sessions/*` routes |
| `server/adapters/reports.py` | `/api/v1/reports` question feedback |
| `server/providers/__init__.py` | Ingestion package init |
| `server/providers/categories.py` | 40-alias → 20-canonical category map + SF Symbols |
| `server/providers/dedup.py` | Signature + Jaccard dedup service |
| `server/providers/openai_provider.py` | GPT-4o-mini trivia generator |
| `server/providers/daemon.py` | Async background ingestion loop + DB writes |
| `server/providers/routes.py` | `/api/v1/ingestion/*` control endpoints |
| `server/providers/difficulty.py` | Claude Haiku batch difficulty scorer |
| `server/providers/difficulty_routes.py` | `/api/v1/difficulty/*` control endpoints |

## AI Difficulty Scoring

Batch job that scores every trivia question's difficulty using GPT-4o-mini.

### How It Works

1. Fetches trivia cards without `ai_difficulty` in their JSONB properties (only dict-type properties)
2. Sends each question + choices to GPT-4o-mini with a rubric (subject obscurity, answer similarity, specialized knowledge)
3. Model responds with a single word: `easy`, `medium`, or `hard`
4. Stores result via `jsonb_set()` as `ai_difficulty` in the card's JSONB properties (no schema migration needed)
5. Exposed in `/api/v1/trivia/gamedata` response as `ai_difficulty` field
6. Qross and alities-mobile can use `ai_difficulty` for consistent difficulty badges and filtering

### Env Vars

| Variable | Default | Description |
|----------|---------|-------------|
| `CE_OPENAI_API_KEY` | (required) | OpenAI API key (reuses ingestion key) |
| `CE_DIFFICULTY_BATCH_SIZE` | `20` | Cards per batch |
| `CE_DIFFICULTY_CONCURRENCY` | `10` | Parallel requests per batch |

### Usage

```bash
# Check status
curl https://bd-cardzerver.fly.dev/api/v1/difficulty/status

# Start scoring all unscored questions
curl -X POST https://bd-cardzerver.fly.dev/api/v1/difficulty/start

# Stop scoring
curl -X POST https://bd-cardzerver.fly.dev/api/v1/difficulty/stop
```

### Cost Estimate

~9k questions × ~105 tokens each ≈ 945k tokens. GPT-4o-mini: ~$0.15 total.
