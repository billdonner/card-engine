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

## Port

card-engine server will use port **9810** (inherits from obo-server slot).

## Commands

```bash
# Apply schema to local postgres
psql -d card_engine -f schema/001_initial.sql

# Run server (future)
cd server && uvicorn app:app --port 9810
```
