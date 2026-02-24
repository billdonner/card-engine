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

Multi-source provider system:

| Provider | Type | Status |
|----------|------|--------|
| OpenAI (gpt-4o-mini) | API | Planned — port from alities-engine |
| RSS/Atom feeds | RSS | Planned |
| CSV/JSON import | Import | Planned |
| obo-gen CLI | API | Planned — port from obo-gen |

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
| obo-server | `~/obo-server` | Being replaced by this |
| obo-gen | `~/obo-gen` | Swift CLI tool, will write to card-engine DB |
| obo-ios | `~/obo-ios` | Current flashcard app |
| flasherz-ios | `~/flasherz-ios` | Future flashcard app (will consume card-engine API) |
| alities-engine | `~/alities-engine` | Being replaced by this |
| alities-mobile | `~/alities-mobile` | Will consume card-engine API |

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

### Key Files

| File | Purpose |
|------|---------|
| `server/app.py` | FastAPI app, lifespan, CORS, /health, /metrics |
| `server/db.py` | asyncpg pool management, query helpers |
| `server/models.py` | All Pydantic request/response models |
| `server/adapters/generic.py` | `/api/v1/decks/*` routes |
| `server/adapters/flashcards.py` | `/api/v1/flashcards/*` routes |
| `server/adapters/trivia.py` | `/api/v1/trivia/*` routes |
