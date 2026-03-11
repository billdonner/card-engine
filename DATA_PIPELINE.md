# Data Pipeline — Ingestion, Deduplication & Access

End-to-end guide for generating trivia content, removing duplicates, and serving it to apps.

## Overview

```
                    ┌──────────────────┐
                    │   OpenAI API     │
                    │  (gpt-4o-mini)   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───────┐  ┌──▼───────┐  ┌──▼──────────┐
     │  bulk_generate  │  │  daemon  │  │  obo-gen    │
     │  (batch CLI)    │  │  (live)  │  │  (Swift)    │
     └────────┬───────┘  └──┬───────┘  └──┬──────────┘
              │    inline    │             │
              │    dedup     │             │
              ▼              ▼             ▼
     ┌──────────────────────────────────────────────┐
     │            PostgreSQL (card_engine)           │
     │  decks ◄──── cards ──── source_runs          │
     │         pg_trgm GIN index on question        │
     └───────────────────┬──────────────────────────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
     ┌────────▼──┐  ┌───▼────┐  ┌─▼──────────┐
     │ dedup_    │  │ dedup_ │  │ quality    │
     │ local.py  │  │ trgm   │  │ API routes │
     │ (post-hoc)│  │ (DB)   │  │ (server)   │
     └───────────┘  └────────┘  └────────────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
     ┌────────▼──┐  ┌───▼────┐  ┌─▼──────────┐
     │  /trivia/ │  │/flash- │  │  /metrics  │
     │  gamedata │  │ cards  │  │  /health   │
     └───────────┘  └────────┘  └────────────┘
              │          │
     ┌────────▼──┐  ┌───▼────┐
     │  Qross /  │  │ OBO    │
     │  Alities  │  │ iOS    │
     └───────────┘  └────────┘
```

## 1. Ingestion

There are three ways content enters the database:

### A. Bulk Generate (batch CLI) — primary for trivia

**Script:** `scripts/bulk_generate.py`

High-volume batch generator for building up a category. Calls OpenAI gpt-4o-mini to generate trivia questions in batches, verifies each answer with a second LLM call, deduplicates against existing DB content, and inserts.

```bash
# Generate 1000 science questions (default settings)
uv run python scripts/bulk_generate.py --category "Science & Nature" --count 1000

# Faster: skip verification, larger batches
uv run python scripts/bulk_generate.py --category "History" --count 500 \
    --batch-size 20 --concurrent 5 --no-verify

# Preview what would be generated (no DB writes)
uv run python scripts/bulk_generate.py --category "Music" --count 50 --dry-run
```

**CLI arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--category` | Arts & Literature | Target category name |
| `--count` | 1000 | Questions to insert (stops when reached) |
| `--batch-size` | 15 | Questions per OpenAI API call |
| `--concurrent` | 3 | Parallel OpenAI requests per batch |
| `--dry-run` | off | Don't write to DB |
| `--no-verify` | off | Skip answer verification (faster, less accurate) |
| `--dedup-only` | off | Scan-only mode, no generation |
| `--delete-dupes` | off | Delete found dupes (with `--dedup-only`) |

**How a batch works:**

1. Pick a random subcategory within the target category (e.g., "Impressionism" within "Arts & Literature")
2. Send prompt to gpt-4o-mini requesting `batch-size` questions as JSON array
3. Parse response → extract `question`, `correct_answer`, `incorrect_answers`, `explanation`, `hint`
4. **Verification pass** (unless `--no-verify`): for each question, ask gpt-4o-mini "Is this answer correct?" — rejects questions the model flags as wrong
5. **Inline dedup**: check each question against existing DB cards using `pg_trgm % operator` (similarity threshold 0.65); also Python-side fuzzy matching (word Jaccard >= 0.85, trigram >= 0.65)
6. Insert surviving questions into `cards` table, linked to a `deck` for the category
7. Log progress: `Progress: 402/696 inserted (548 generated, 27 dupes skipped, 119 rejected)`
8. **Auto-stop**: if rejection rate (dupes + verification failures) reaches 50% after 50+ generated, the script stops — the AI is exhausting unique content for this category

**Environment variables (required):**

```bash
export CE_OPENAI_API_KEY="sk-..."
export CE_DATABASE_HOST=localhost
export CE_DATABASE_PORT=15433
export CE_DATABASE_USER=postgres
export CE_DATABASE_PASSWORD='...'
export CE_DATABASE_NAME=card_engine
```

**Running multiple categories in sequence:**

Create a job file (one line per category: `name|count`):

```bash
cat > /tmp/gen_jobs.txt << 'EOF'
Comics|696
Technology|710
Food & Drink|734
Film & TV|751
EOF
```

Then process it:

```bash
while IFS='|' read -r cat count; do
    echo "=== Generating $cat ($count) ==="
    uv run python scripts/bulk_generate.py \
        --category "$cat" --count "$count" \
        --batch-size 15 --concurrent 2 \
        >> /tmp/gen_logs/gen_${cat// /_}.log 2>&1
done < /tmp/gen_jobs.txt
```

**Rate:** ~20 cards/minute with verification enabled, ~40/minute without.

### B. Ingestion Daemon (live background)

**File:** `server/providers/daemon.py`

Runs as an async background task inside the FastAPI process. Generates a small batch every cycle (default 60s) to continuously grow the corpus.

```bash
# Control via API
curl -X POST https://bd-cardzerver.fly.dev/api/v1/ingestion/start
curl -X POST https://bd-cardzerver.fly.dev/api/v1/ingestion/stop
curl -X POST https://bd-cardzerver.fly.dev/api/v1/ingestion/pause
curl https://bd-cardzerver.fly.dev/api/v1/ingestion/status
```

| Env Var | Default | Description |
|---------|---------|-------------|
| `CE_INGEST_CYCLE_SECONDS` | 60 | Sleep between cycles |
| `CE_INGEST_BATCH_SIZE` | 10 | Questions per cycle |
| `CE_INGEST_AUTO_START` | false | Auto-start on server boot |
| `CE_INGEST_CONCURRENT_BATCHES` | 5 | Parallel OpenAI calls |

Uses a two-stage inline dedup: exact signature hash (O(1)), then word Jaccard similarity >= 0.85 against the last 1,000 questions in memory.

### C. obo-gen (Swift CLI)

**Binary:** `~/bin/obo-gen`

Generates flashcard decks (not trivia) and writes directly to the `cards` table. Used for family flashcard content.

## 2. Deduplication

Three levels of dedup, from fastest to most thorough:

### Level 1: Inline (during generation)

Happens automatically during both bulk_generate and daemon ingestion. Prevents the most obvious duplicates from ever entering the database.

| Method | Where | Threshold | Speed |
|--------|-------|-----------|-------|
| Exact signature hash | daemon | 1.0 (exact) | O(1) |
| Word Jaccard similarity | daemon + bulk | 0.85 | O(n) vs last 1k |
| pg_trgm `%` operator | bulk_generate | 0.65 | O(log n) via GIN index |
| Python trigram + word Jaccard | bulk_generate | 0.65 / 0.85 | O(n) per question |

### Level 2: Post-hoc full scan — `dedup_local.py`

**Script:** `scripts/dedup_local.py`

Loads ALL trivia cards into memory, builds a Python trigram index, and finds every duplicate pair across the entire corpus. This catches duplicates that inline dedup missed (e.g., questions generated in different sessions or categories).

```bash
# Dry run — find dupes, print summary (no deletions)
cd ~/card-engine
uv run python scripts/dedup_local.py

# Actually delete the newer duplicate in each pair
uv run python scripts/dedup_local.py --delete

# Verbose — print every duplicate pair found
uv run python scripts/dedup_local.py --verbose

# Higher threshold (fewer matches, only very close duplicates)
uv run python scripts/dedup_local.py --threshold 0.70

# Single category only
uv run python scripts/dedup_local.py --category "History"
```

**Algorithm:**
1. Load all trivia cards ordered by `created_at`
2. Build inverted trigram index: trigram → list of card indices
3. For each card, find candidates with shared trigrams (pre-filter)
4. Compute full similarity for promising candidates
5. Keep the older card, mark the newer one for deletion
6. Delete in batches of 200

**Output:** JSON summary to stdout with `total_cards`, `duplicates_found`, `deleted`, breakdown `by_category`, and `sample_pairs`.

**Expected runtime:** 2-5 minutes for ~50k cards.

### Level 3: Post-hoc DB-side — `dedup_trgm.py`

**Script:** `scripts/dedup_trgm.py`

Worker-pool approach that runs similarity queries directly on Postgres using the `pg_trgm` GIN index. Better for very large corpora where loading everything into Python memory is impractical.

```bash
# Dry run
uv run python scripts/dedup_trgm.py

# Delete duplicates (12 concurrent workers)
uv run python scripts/dedup_trgm.py --delete --workers 12
```

### Level 4: Server-side quality API

The running cardzerver also exposes dedup via REST:

```bash
# Scan for duplicates (TF-IDF + cosine similarity)
curl -X POST https://bd-cardzerver.fly.dev/api/v1/quality/dedup/scan

# Quarantine duplicates (keeps first in cluster)
curl -X POST https://bd-cardzerver.fly.dev/api/v1/quality/dedup/purge
```

### Recommended workflow

After a batch ingestion run completes:

```bash
# 1. Run full dedup scan (dry run first to see the damage)
cd ~/card-engine
uv run python scripts/dedup_local.py --verbose

# 2. If the numbers look right, delete
uv run python scripts/dedup_local.py --delete

# 3. Verify counts
uv run python -c "
import asyncio, asyncpg
async def main():
    conn = await asyncpg.connect(host='localhost', port=15433, user='postgres',
        password='...', database='card_engine')
    rows = await conn.fetch('''
        SELECT d.title, COUNT(c.id) FROM decks d
        LEFT JOIN cards c ON c.deck_id = d.id
        GROUP BY d.title ORDER BY COUNT(c.id) DESC
    ''')
    for r in rows: print(f'{r[0]:30s} {r[1]:>6}')
    await conn.close()
asyncio.run(main())
"
```

## 3. Database Schema

PostgreSQL on port 15433 (via flyctl proxy to Fly Postgres).

### Core tables

```
decks
├── id          UUID PK
├── title       TEXT         -- category name ("Arts & Literature")
├── kind        ENUM         -- flashcard | trivia | newsquiz
├── properties  JSONB        -- pic, age_range, voice, description
├── card_count  INTEGER      -- auto-synced by trigger
├── tier        ENUM         -- free | family | premium
└── created_at  TIMESTAMPTZ

cards
├── id          UUID PK
├── deck_id     UUID FK → decks
├── position    INTEGER      -- order within deck
├── question    TEXT         -- the question text (trigram-indexed)
├── properties  JSONB        -- kind-specific payload (see below)
├── difficulty  ENUM         -- easy | medium | hard
├── source_id   UUID FK → source_providers
├── source_date TIMESTAMPTZ
├── created_at  TIMESTAMPTZ
├── quarantined BOOLEAN      -- hidden from API responses
└── quarantine_reason TEXT

source_providers
├── id          UUID PK
├── name        TEXT UNIQUE  -- 'openai', 'rss:bbc-kids', 'import:csv'
├── type        ENUM         -- api | rss | import | manual
└── config      JSONB

source_runs                   -- audit log
├── id          UUID PK
├── provider_id UUID FK → source_providers
├── started_at  TIMESTAMPTZ
├── finished_at TIMESTAMPTZ
├── items_fetched INTEGER
├── items_added   INTEGER
├── items_skipped INTEGER
└── error       TEXT
```

### cards.properties JSONB — trivia format

```json
{
  "choices": [
    {"text": "Leonardo da Vinci", "isCorrect": true},
    {"text": "Michelangelo", "isCorrect": false},
    {"text": "Raphael", "isCorrect": false},
    {"text": "Donatello", "isCorrect": false}
  ],
  "correct_index": 0,
  "explanation": "Leonardo painted the Mona Lisa circa 1503-1519",
  "hint": "Think Italian Renaissance polymath",
  "aisource": "openai",
  "subcategory": "Renaissance Art",
  "ai_difficulty": "easy"
}
```

### Key indexes

| Index | Table | Type | Purpose |
|-------|-------|------|---------|
| `idx_cards_question_trgm` | cards.question | GIN (pg_trgm) | O(log n) fuzzy duplicate search |
| `idx_cards_deck_id` | cards.deck_id | btree | List cards in a deck |
| `idx_cards_deck_position` | cards(deck_id, position) | btree | Ordered card access |
| `idx_cards_difficulty` | cards.difficulty | btree | Filter by difficulty |

### Triggers

- `trg_card_count` — auto-increment/decrement `decks.card_count` on card insert/delete
- `trg_decks_updated_at` — auto-update `decks.updated_at` on modification

## 4. API Access

FastAPI server on port **9810** (production: https://bd-cardzerver.fly.dev).

### Trivia endpoints (used by Qross and Alities apps)

**GET /api/v1/trivia/gamedata** — bulk export

```bash
# Get 50 random science questions
curl "https://bd-cardzerver.fly.dev/api/v1/trivia/gamedata?categories=Science%20%26%20Nature&count=50"

# Player-aware (excludes previously seen cards, creates session)
curl "https://bd-cardzerver.fly.dev/api/v1/trivia/gamedata?player_id=UUID&count=50"

# Filter by tier
curl "https://bd-cardzerver.fly.dev/api/v1/trivia/gamedata?tier=free&count=100"
```

Response shape:

```json
{
  "id": "uuid",
  "generated": "2026-03-11T01:30:00Z",
  "challenges": [
    {
      "id": "card-uuid",
      "topic": "Science & Nature",
      "pic": "atom",
      "question": "What is the chemical symbol for gold?",
      "answers": ["Au", "Ag", "Fe", "Cu"],
      "correct": "Au",
      "explanation": "Au comes from the Latin 'aurum'",
      "hint": "Think Latin",
      "aisource": "openai",
      "date": "2026-03-10T15:00:00Z",
      "ai_difficulty": "easy"
    }
  ],
  "session_id": "uuid",
  "share_code": "ABC123",
  "fresh_count": 4500,
  "total_available": 5000
}
```

**GET /api/v1/trivia/categories** — category list with counts

```json
{
  "categories": [
    {"name": "Science & Nature", "pic": "atom", "count": 3609, "updated_at": "..."}
  ]
}
```

### Flashcard endpoints (used by OBO iOS)

**GET /api/v1/flashcards** — all flashcard decks with cards
**GET /api/v1/flashcards/{deck_id}** — single deck

### Monitoring

**GET /health** — DB connectivity check
**GET /metrics** — deck/card/source counts (consumed by server-monitor)

## 5. Current Corpus Status

As of 2026-03-11 (51,853 total cards across 30 decks):

| Category | Cards | Status |
|----------|------:|--------|
| General Knowledge | 6,276 | complete |
| Music | 4,719 | complete |
| Politics | 4,577 | complete |
| Pop Culture | 4,416 | complete |
| Mythology | 4,387 | complete |
| Mathematics | 4,315 | complete |
| Arts & Literature | 4,293 | complete |
| History | 4,163 | complete |
| Science & Nature | 3,609 | complete |
| Sports | 3,218 | complete |
| Board/Video Games, Society, Vehicles, Geography, Literature | 1,000 each | complete |
| Comics | 730 | generating |
| Technology | 290 | queued |
| Food & Drink | 266 | queued |
| Film & TV | 249 | queued |
| *(family flashcard decks)* | ~340 | complete |

Target: fill each category to 1,000+ cards, then run a full dedup pass.

## 6. Operations Playbook

### Start a bulk generation run

```bash
cd ~/card-engine

# Set up env (or use .envrc / direnv)
export CE_OPENAI_API_KEY="sk-..."
export CE_DATABASE_HOST=localhost CE_DATABASE_PORT=15433
export CE_DATABASE_USER=postgres CE_DATABASE_PASSWORD='...'
export CE_DATABASE_NAME=card_engine

# Start proxy to Fly Postgres (if not already running)
flyctl proxy 15433:5432 -a bd-postgres &

# Single category
uv run python scripts/bulk_generate.py --category "Film & TV" --count 1000

# Multiple categories via job file
while IFS='|' read -r cat count; do
    uv run python scripts/bulk_generate.py \
        --category "$cat" --count "$count" \
        --batch-size 15 --concurrent 2 \
        >> "/tmp/gen_logs/gen_${cat// /_}.log" 2>&1
done < /tmp/gen_jobs.txt
```

### Monitor a running ingestion

```bash
# Check if running
ps aux | grep bulk_generate

# Tail the log
tail -f /tmp/gen_logs/gen_Comics.log

# See progress summaries only
grep 'Progress:' /tmp/gen_logs/gen_Comics.log
```

### Run post-hoc dedup after ingestion

```bash
cd ~/card-engine

# 1. Dry run first
uv run python scripts/dedup_local.py --verbose 2>&1 | tee /tmp/dedup_report.txt

# 2. Review the output, then delete
uv run python scripts/dedup_local.py --delete

# 3. Verify card counts haven't dropped too much
curl -s https://bd-cardzerver.fly.dev/api/v1/trivia/categories | python3 -m json.tool
```

### Check corpus health

```bash
# Category counts via API
curl -s https://bd-cardzerver.fly.dev/api/v1/trivia/categories | \
    python3 -c "import sys,json; [print(f'{c[\"name\"]:30s} {c[\"count\"]:>6}') for c in json.load(sys.stdin)['categories']]"

# Quality stats
curl -s https://bd-cardzerver.fly.dev/api/v1/quality/stats | python3 -m json.tool

# Recent ingestion runs
curl -s https://bd-cardzerver.fly.dev/api/v1/ingestion/runs | python3 -m json.tool
```

### Cost estimates

| Operation | Model | Tokens/question | Cost per 1k questions |
|-----------|-------|----------------:|----------------------:|
| Generation | gpt-4o-mini | ~300 | ~$0.05 |
| Verification | gpt-4o-mini | ~150 | ~$0.02 |
| AI Difficulty | gpt-4o-mini | ~105 | ~$0.02 |
| Total (gen + verify + score) | | ~555 | ~$0.09 |
