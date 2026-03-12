# Trivia Generation Handoff

This document tells a new Claude Code instance how to continue trivia question generation for the Qross app.

## Current State (March 12, 2026)

**56,211 total trivia cards in production Postgres (bd-postgres).**

### Categories needing generation (4 categories, ~500 each)

These categories have subcategories defined in `scripts/bulk_generate.py` and mappings in `server/providers/categories.py`, but failed to generate due to Fly Postgres recovery mode crashes:

| Category | Current Count | Target |
|----------|--------------|--------|
| Stand-Up Comedy | 13 | 500 |
| Chess | 1 | 500 |
| Animated Films | 0 | 500 |
| Inventions | 0 | 500 |

### Categories fully generated (16 specialty + 20 original = 36 total)

All specialty categories at 488-533 questions each. Original 20 categories range from 500-5500.

## Quick Start

### 1. Prerequisites

```bash
# Clone if not already present
git clone https://github.com/billdonner/card-engine.git ~/card-engine
cd ~/card-engine

# Install Python deps
uv sync

# Install Fly CLI (if not present)
curl -L https://fly.io/install.sh | sh
flyctl auth login
```

### 2. Open Fly proxy

```bash
flyctl proxy 15433:5432 -a bd-postgres &
```

### 3. Set environment variables

```bash
export CE_DATABASE_HOST=localhost
export CE_DATABASE_PORT=15433
export CE_DATABASE_USER=card_engine_user
export CE_DATABASE_NAME=card_engine
export CE_DATABASE_PASSWORD='P$$Ba#'
export CE_OPENAI_API_KEY='<ask billdonner for the OpenAI key>'
```

### 4. Generate remaining categories

Run one at a time:

```bash
cd ~/card-engine

.venv/bin/python scripts/bulk_generate.py --category "Stand-Up Comedy" --count 500 --no-verify --concurrent 2
.venv/bin/python scripts/bulk_generate.py --category "Chess" --count 500 --no-verify --concurrent 2
.venv/bin/python scripts/bulk_generate.py --category "Animated Films" --count 500 --no-verify --concurrent 2
.venv/bin/python scripts/bulk_generate.py --category "Inventions" --count 500 --no-verify --concurrent 2
```

Each takes ~50 minutes. After each one, wait 5 minutes before starting the next (Fly Postgres recovery).

### 5. Post-generation

```bash
# Start difficulty scoring
curl -X POST https://bd-cardzerver.fly.dev/api/v1/difficulty/start

# Check stats
trivia-check --server https://bd-cardzerver.fly.dev stats

# Deploy updated categories.py to production (if not already done)
cd ~/Flyz/scripts && ./deploy.sh card-engine
```

## Key Files

| File | Purpose |
|------|---------|
| `scripts/bulk_generate.py` | Main generation script (OpenAI gpt-4o-mini) |
| `server/providers/categories.py` | Category names, aliases, SF Symbols |
| `PLAYBOOK.md` | Full operational playbook |
| `CLAUDE.md` | Project context for Claude Code |

## Known Issues

- **Fly Postgres recovery mode**: After ~500 questions, Postgres enters WAL recovery for 2-5 min. This is normal. Wait and retry.
- **Proxy drops**: If `bulk_generate.py` hangs, kill and restart: `pkill -f "flyctl proxy 15433"; flyctl proxy 15433:5432 -a bd-postgres &`
- **`trivia-check dedup`** may 502 on production for large scans. Use direct DB queries instead.
