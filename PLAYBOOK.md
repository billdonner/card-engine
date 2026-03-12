# Trivia Content Playbook

Instructions for Claude Code instances managing trivia question generation and ingestion.

## When to Use What

| Method | Use Case | Speed | Cost |
|--------|----------|-------|------|
| **Daemon** | Steady background growth across all 20 categories | ~500 Q/hour | Low (gpt-4o-mini) |
| **Bulk script** | Targeted fill for a specific category (1000+ questions) | ~200 Q/min | Low (gpt-4o-mini) |
| **obo-gen CLI** | Small batches, testing, or on-device generation | ~10-50 Q/run | Free (onboard) or low |

## Production Bulk Generation (most common task)

### 1. Open Fly proxy

```bash
flyctl proxy 15433:5432 -a bd-postgres &
```

Keep this running in the background for the entire session.

### 2. Set environment variables

**Use individual vars, NOT `CE_DATABASE_URL`** (the `#` in passwords breaks URL parsing):

```bash
export CE_DATABASE_HOST=localhost
export CE_DATABASE_PORT=15433
export CE_DATABASE_USER=card_engine
export CE_DATABASE_PASSWORD=<password from fly secrets>
export CE_DATABASE_NAME=card_engine
export CE_OPENAI_API_KEY=<key>
```

### 3. Run bulk generation with rolling worker pool

For a single category:
```bash
cd ~/card-engine
python scripts/bulk_generate.py --category "Arts & Literature" --count 1000
```

For multiple categories, run **one at a time** (sequential). Fly Postgres enters recovery mode after each ~500-question bulk run, so parallel/pooled approaches cause cascading connection failures:

```bash
#!/bin/bash
CATEGORIES=("Science & Nature" "History" "Geography")

for cat in "${CATEGORIES[@]}"; do
    echo "Starting: $cat"
    .venv/bin/python scripts/bulk_generate.py --category "$cat" --count 500 \
        --no-verify --concurrent 2 \
        > "/tmp/gen_$(echo $cat | tr ' &-' '___').log" 2>&1
    echo "Done: $cat"
done
echo "All done"
```

**Important:** After each category completes, the Fly Postgres instance may enter WAL recovery for 2-5 minutes. Sequential execution handles this naturally — the next category's connection attempt waits and retries. If a category fails to connect, just re-run it after a few minutes.

### 4. Post-generation quality checks

Run these after any bulk generation:

```bash
# Scan for duplicates (dry run first)
trivia-check dedup --dry-run

# Purge duplicates if found
trivia-check dedup

# Check for answer-in-question leaks
trivia-check aiq --dry-run

# Score difficulty on new questions
curl -X POST https://bd-cardzerver.fly.dev/api/v1/difficulty/start

# Verify counts
trivia-check stats
```

### 5. Monitor via dashboard

The server-monitor dashboard at https://bd-server-monitor.fly.dev shows live `cat_*` metrics from cardzerver — question counts per category update in real time during generation.

## The 36 Canonical Categories

### Original 20
```
Science & Nature    Technology      Mathematics     History
Geography           Politics        Sports          Music
Literature          Arts & Lit.     Film & TV       Video Games
Board Games         Comics          Food & Drink    Pop Culture
Mythology           Society & Culture  General Knowledge  Vehicles
```

### Specialty 16 (added March 2026)
```
Romance Novels      Silent Movies       Broadway Musicals   Cocktails & Spirits
Space Exploration   True Crime          Fashion & Design    Roller Coasters
National Parks      Horror Movies       The Beatles         Reality TV
Volcanoes & Earthquakes  Pirates & Smugglers  Olympic Games  Candy & Chocolate
```

**Not yet generated (need bulk_generate):** Stand-Up Comedy, Chess, Animated Films, Inventions

Category names must match exactly (case-sensitive). The server normalizes ~80 aliases (e.g., "science" → "Science & Nature", "beatles" → "The Beatles") but bulk_generate.py uses exact names.

## Daemon Control (for steady-state growth)

```bash
# Check status
curl https://bd-cardzerver.fly.dev/api/v1/ingestion/status

# Start daemon (generates across all 20 categories)
curl -X POST https://bd-cardzerver.fly.dev/api/v1/ingestion/start

# Stop daemon
curl -X POST https://bd-cardzerver.fly.dev/api/v1/ingestion/stop
```

The daemon auto-starts on deploy if `CE_INGEST_AUTO_START=true` (currently false in production).

## obo-gen CLI (interactive/small batches)

```bash
# Generate 20 trivia questions via on-device AI (free, no API key)
obo-gen "Volcanoes" -n 20 --kind trivia --model onboard

# Batch from file
echo "Solar System\nUS Presidents\nAncient Rome" > topics.txt
obo-gen batch topics.txt --kind trivia --model onboard -n 15

# Check current stats
obo-gen stats
```

obo-gen writes to cardzerver via REST API (no direct DB access). Set `CARDZERVER_URL` if not using production.

## Adding a New Category

1. Add to `CATEGORY_SUBCATEGORIES` dict in `scripts/bulk_generate.py` (30 subcategories for diversity)
2. Add canonical name + aliases to `server/providers/categories.py`
3. Add SF Symbol mapping in `CANONICAL_TO_SYMBOL`
4. Generate initial batch: `python scripts/bulk_generate.py --category "New Category" --count 500`
5. Run difficulty scorer: `curl -X POST .../api/v1/difficulty/start`
6. Verify in Qross: category appears in Categories view automatically

## Common Gotchas

- **Never use `CE_DATABASE_URL` with Fly proxy** — passwords containing `#` break URL parsing. Use individual `CE_DATABASE_*` vars.
- **Fly proxy dies silently** — if bulk_generate hangs on DB connection, restart the proxy.
- **Fly Postgres enters recovery mode after bulk writes** — after ~500 questions, Postgres checkpoints and rejects connections for 2-5 min. Run categories sequentially and expect some to fail. Re-run failed categories after the DB recovers.
- **Use `.venv/bin/python` not `python`** — macOS doesn't have a system `python`. Always use the card-engine venv directly.
- **Use `--concurrent 2`** — higher concurrency overwhelms the Fly proxy. Two workers is the sweet spot.
- **Dedup thresholds**: Jaccard word similarity at 0.85, trigram at 0.65. These are tuned to catch paraphrasing without false positives. Don't lower them.
- **Deck creation is automatic** — `bulk_generate.py` creates the deck if it doesn't exist. No manual setup needed.
- **Card count is trigger-maintained** — `trg_card_count` on the `cards` table keeps `decks.card_count` accurate. Never update it manually.
- **Player dedup is server-side** — Qross sends `player_id` with each game request; the server excludes previously seen cards. No client-side dedup needed.
