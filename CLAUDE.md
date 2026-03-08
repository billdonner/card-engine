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

PostgreSQL — incremental migrations in `schema/`.

### Content tables (001)
- `decks` — content collections (kind: flashcard, trivia, newsquiz)
- `cards` — content items with JSONB properties
- `source_providers` — tracks ingestion sources
- `source_runs` — audit log for pipeline runs

### Player & session tables (008)
- `players` — anonymous device-based identity (`device_id TEXT UNIQUE`)
- `player_card_history` — tracks which cards each player has been served
- `sessions` — dealt hand of cards with shareable 6-char code
- `session_cards` — ordered card list within a session

### Family tree tables (005, 009, 010)
- `families` — top-level family record
- `family_people` — people in the tree (name, born, gender, status, player flag)
- `family_relationships` — typed edges: `married`, `parent_of`, `divorced`
- `family_chat_sessions` — JSONB chat history per family (LLM builder)
- `family_members` — links `players` → `families` with role (`owner`/`member`) (009)
- `family_invites` — 6-char invite codes for joining a family (009)
- `family_card_exclusions` — questions a family has removed from generated decks (010)

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

#### Quality Control (Layer 2) — dedup, veracity, quarantine

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/quality/dedup/scan` | Scan full corpus for exact + near duplicates |
| POST | `/api/v1/quality/dedup/purge` | Quarantine duplicates (keeps first in cluster) |
| POST | `/api/v1/quality/veracity/check` | Verify trivia facts via LLM (Claude or GPT) |
| POST | `/api/v1/quality/answer-in-question/scan` | Find/delete answer-in-question cards |
| GET | `/api/v1/quality/quarantine` | List quarantined cards (paginated) |
| POST | `/api/v1/quality/quarantine/{id}/restore` | Un-quarantine a card |
| DELETE | `/api/v1/quality/quarantine/{id}` | Permanently delete a quarantined card |
| GET | `/api/v1/quality/quarantine/review` | Lightweight web UI for human review |
| GET | `/api/v1/quality/stats` | Quality control statistics |

CLI: `trivia-check` (installed to `~/bin/`). Commands: `dedup`, `veracity`, `aiq`, `scan`, `quarantine`, `stats`. Global `--dry-run` flag for read-only mode.

#### AI Difficulty Scoring (Layer 2) — batch scoring

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/difficulty/status` | Scorer state, stats, scored/unscored counts |
| POST | `/api/v1/difficulty/start` | Start batch scoring job |
| POST | `/api/v1/difficulty/stop` | Stop scoring job |

#### Family Tree (Layer 2) — device-based access control

All family endpoints require `?player_id=<uuid>` (device identity). Families are private — only members see and edit them.

**Family CRUD**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/family` | open | Create family; body `{name, player_id}` — creator becomes owner |
| GET | `/api/v1/family` | open | List families `?player_id=` — returns only player's families |
| GET | `/api/v1/family/{id}` | member | Full tree (people + relationships) |
| DELETE | `/api/v1/family/{id}` | owner | Delete family and all data |

**People & Relationships**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/family/{id}/people` | member | Add person |
| PATCH | `/api/v1/family/{id}/people/{pid}` | member | Update person |
| DELETE | `/api/v1/family/{id}/people/{pid}` | member | Delete person |
| POST | `/api/v1/family/{id}/relationships` | member | Add relationship (married/parent_of/divorced) |
| DELETE | `/api/v1/family/{id}/relationships/{rid}` | member | Delete relationship |

**Membership**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/family/{id}/members` | member | List members with roles |
| DELETE | `/api/v1/family/{id}/members/{pid}` | owner | Remove a member |
| POST | `/api/v1/family/join` | open | Join via invite code; body `{player_id, invite_code}` |

**Invites**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/family/{id}/invite` | owner | Create 6-char invite code |
| GET | `/api/v1/family/{id}/invite` | owner | List active invite codes |
| DELETE | `/api/v1/family/{id}/invite/{iid}` | owner | Revoke invite code |

**Tree Views**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/family/{id}/tree` | member | Full tree (same as GET family/{id}) |
| GET | `/api/v1/family/{id}/players` | member | People marked as players |
| GET | `/api/v1/family/{id}/open_items` | member | Placeholders and missing-field report |

**Chat Builder (LLM)**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/family/{id}/chat` | member | Send message to Claude; applies patches to tree |
| GET | `/api/v1/family/{id}/chat/history` | member | Chat message history |

**Deck Generation & Editing**

| Method | Path | Access | Description |
|--------|------|--------|-------------|
| POST | `/api/v1/family/{id}/generate/{player_id}` | member | Generate flashcard + trivia decks for a player |
| GET | `/api/v1/family/{id}/deck/{player_id}` | member | List generated decks for a player |
| GET | `/api/v1/family/{id}/decks/{deck_id}` | member | List all cards in a generated deck |
| DELETE | `/api/v1/family/{id}/decks/{deck_id}/cards/{cid}` | member | Remove card + add to exclusion list |
| GET | `/api/v1/family/{id}/exclusions` | member | List excluded questions |
| DELETE | `/api/v1/family/{id}/exclusions/{eid}` | member | Restore excluded question |

**Family tree files:**

| File | Purpose |
|------|---------|
| `server/family/db.py` | All family DB helpers (tree, membership, invites, deck editing, exclusions) |
| `server/family/models.py` | Pydantic models for all family features |
| `server/family/routes.py` | All `/api/v1/family/*` routes with `require_member`/`require_owner` guards |
| `server/family/engine.py` | Relationship graph engine: `FamilyGraph.compute_relations()` |
| `server/family/generator.py` | Deterministic flashcard + trivia deck generator (skips excluded questions) |
| `server/family/llm_client.py` | Claude API chat builder — parses patches from LLM response |

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
| `server/adapters/quality.py` | `/api/v1/quality/*` routes + quarantine review HTML |
| `server/providers/quality/dedup.py` | TF-IDF + cosine similarity corpus-wide dedup |
| `server/providers/quality/veracity.py` | LLM-based fact checking (Claude + GPT) |
| `server/providers/quality/answer_in_question.py` | Answer-in-question detector |
| `scripts/trivia_check.py` | CLI tool (installed as `~/bin/trivia-check`) |

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
