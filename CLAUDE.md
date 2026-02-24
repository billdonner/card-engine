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

#### Ingestion Control (Layer 2) — daemon management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/ingestion/status` | Daemon state, stats, and config |
| POST | `/api/v1/ingestion/start` | Start the ingestion daemon |
| POST | `/api/v1/ingestion/stop` | Stop the ingestion daemon |
| POST | `/api/v1/ingestion/pause` | Pause (finish current batch, then sleep) |
| POST | `/api/v1/ingestion/resume` | Resume from paused state |
| GET | `/api/v1/ingestion/runs` | Recent source_run audit log |

### Key Files

| File | Purpose |
|------|---------|
| `server/app.py` | FastAPI app, lifespan, CORS, /health, /metrics |
| `server/db.py` | asyncpg pool management, query helpers |
| `server/models.py` | All Pydantic request/response models |
| `server/adapters/generic.py` | `/api/v1/decks/*` routes |
| `server/adapters/flashcards.py` | `/api/v1/flashcards/*` routes |
| `server/adapters/trivia.py` | `/api/v1/trivia/*` routes |
| `server/providers/__init__.py` | Ingestion package init |
| `server/providers/categories.py` | 40-alias → 20-canonical category map + SF Symbols |
| `server/providers/dedup.py` | Signature + Jaccard dedup service |
| `server/providers/openai_provider.py` | GPT-4o-mini trivia generator |
| `server/providers/daemon.py` | Async background ingestion loop + DB writes |
| `server/providers/routes.py` | `/api/v1/ingestion/*` control endpoints |
