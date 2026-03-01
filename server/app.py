"""card-engine — Unified content backend for Flasherz and Alities apps."""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from threading import Lock

import psutil
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.db import close_pool, get_pool, get_report_count, get_stats, init_pool

logger = logging.getLogger("card_engine")

PORT = int(os.environ.get("CE_PORT", "9810"))


# ---------------------------------------------------------------------------
# RateCounter — thread-safe sliding-window request counter
# ---------------------------------------------------------------------------

SPARKLINE_BUCKETS = 60


class RateCounter:
    """Count events in a sliding window and expose per-second rate + history."""

    def __init__(self, window: float = 60.0) -> None:
        self._window = window
        self._lock = Lock()
        self._timestamps: deque[float] = deque()
        self._sparkline: deque[float] = deque(maxlen=SPARKLINE_BUCKETS)
        self._last_snapshot = time.monotonic()

    def record(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._timestamps.append(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            count = len(self._timestamps)
        return count / self._window if self._window else 0.0

    def snapshot_sparkline(self) -> None:
        self._sparkline.append(round(self.rate(), 2))

    def sparkline_history(self) -> list[float]:
        return list(self._sparkline)


request_counter = RateCounter(window=60.0)
_start_time: float = 0.0


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time
    _start_time = time.time()

    await init_pool()
    logger.info("Database pool initialized")

    # Initialize ingestion daemon
    from server.providers.daemon import IngestionConfig, IngestionDaemon

    config = IngestionConfig()
    daemon = IngestionDaemon(pool=get_pool(), config=config)
    app.state.daemon = daemon
    if config.auto_start:
        await daemon.start()
        logger.info("Ingestion daemon auto-started")

    yield

    await daemon.stop()
    logger.info("Ingestion daemon stopped")
    await close_pool()
    logger.info("Database pool closed")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="card-engine",
    version="0.1.0",
    description="Unified content backend for Flasherz (flashcards) and Alities (trivia)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request counting middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def count_requests(request: Request, call_next):
    request_counter.record()
    return await call_next(request)


# ---------------------------------------------------------------------------
# Include adapter routers
# ---------------------------------------------------------------------------

from server.adapters.generic import router as generic_router  # noqa: E402
from server.adapters.flashcards import router as flashcards_router  # noqa: E402
from server.adapters.trivia import router as trivia_router  # noqa: E402
from server.adapters.studio import router as studio_router  # noqa: E402
from server.adapters.reports import router as reports_router  # noqa: E402
from server.providers.routes import router as ingestion_router  # noqa: E402
from server.family.routes import router as family_router  # noqa: E402

app.include_router(generic_router)
app.include_router(flashcards_router)
app.include_router(trivia_router)
app.include_router(studio_router)
app.include_router(reports_router)
app.include_router(ingestion_router)
app.include_router(family_router)


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check — returns DB connectivity status."""
    result: dict = {"status": "ok"}
    try:
        p = get_pool()
        db_ok = await p.fetchval("SELECT 1")
        result["database"] = "connected" if db_ok == 1 else "unexpected"
    except RuntimeError:
        result["database"] = "pool_not_initialized"
    except Exception as exc:
        result["status"] = "degraded"
        result["database"] = f"error: {exc}"
    return result


@app.get("/metrics")
async def metrics():
    """Stats endpoint for server-monitor dashboard."""
    try:
        p = get_pool()
        now = time.time()
        process = psutil.Process(os.getpid())
        mem = process.memory_info()

        request_counter.snapshot_sparkline()

        # -- System metrics ---------------------------------------------------

        uptime = now - _start_time if _start_time else 0.0
        rps = request_counter.rate()

        result: list[dict] = [
            {
                "key": "uptime",
                "label": "Uptime",
                "value": round(uptime),
                "unit": "seconds",
            },
            {
                "key": "rps",
                "label": "Requests / sec",
                "value": round(rps, 2),
                "unit": "req/s",
                "warn_above": 200,
                "sparkline_history": request_counter.sparkline_history(),
            },
            {
                "key": "memory_rss",
                "label": "Memory (RSS)",
                "value": round(mem.rss / 1_048_576, 1),
                "unit": "MB",
                "warn_above": 512,
            },
            {
                "key": "memory_vms",
                "label": "Memory (VMS)",
                "value": round(mem.vms / 1_048_576, 1),
                "unit": "MB",
            },
            {
                "key": "cpu_percent",
                "label": "CPU usage",
                "value": process.cpu_percent(interval=0),
                "unit": "%",
                "warn_above": 90,
            },
        ]

        # -- Content metrics --------------------------------------------------

        stats = await get_stats()

        result.extend([
            {"key": "total_decks", "label": "Total decks", "value": stats["total_decks"], "unit": "decks"},
            {"key": "total_cards", "label": "Total cards", "value": stats["total_cards"], "unit": "cards"},
            {"key": "total_sources", "label": "Source providers", "value": stats["total_sources"], "unit": "sources"},
        ])

        # Deck breakdown by kind
        for kind, count in stats["decks_by_kind"].items():
            result.append({
                "key": f"decks_{kind}",
                "label": f"Decks ({kind})",
                "value": count,
                "unit": "decks",
            })

        # Published vs draft
        published = await p.fetchval(
            "SELECT COUNT(*) FROM decks WHERE COALESCE(properties->>'status', 'published') = 'published'"
        )
        draft = await p.fetchval(
            "SELECT COUNT(*) FROM decks WHERE properties->>'status' = 'draft'"
        )
        result.extend([
            {"key": "decks_published", "label": "Published decks", "value": published, "unit": "decks"},
            {"key": "decks_draft", "label": "Draft decks", "value": draft, "unit": "decks"},
        ])

        # -- Ingestion metrics ------------------------------------------------

        daemon = app.state.daemon
        daemon_state = daemon.state if daemon else "unknown"
        daemon_stats = daemon.stats if daemon else {}

        result.extend([
            {"key": "ingest_state", "label": "Ingestion daemon", "value": daemon_state, "unit": ""},
            {
                "key": "ingest_cycles",
                "label": "Ingestion cycles",
                "value": daemon_stats.get("cycles_completed", 0),
                "unit": "cycles",
            },
            {
                "key": "ingest_added",
                "label": "Cards ingested",
                "value": daemon_stats.get("items_added", 0),
                "unit": "cards",
            },
            {
                "key": "ingest_dupes",
                "label": "Duplicates skipped",
                "value": daemon_stats.get("duplicates_skipped", 0),
                "unit": "cards",
            },
            {
                "key": "ingest_errors",
                "label": "Ingestion errors",
                "value": daemon_stats.get("errors", 0),
                "unit": "errors",
                "warn_above": 10,
            },
        ])

        # Last source run
        last_run = await p.fetchrow(
            "SELECT finished_at, items_added, items_skipped, error "
            "FROM source_runs ORDER BY started_at DESC LIMIT 1"
        )
        if last_run:
            if last_run["error"]:
                result.append({
                    "key": "last_run_status",
                    "label": "Last run",
                    "value": "error",
                    "unit": "",
                    "color": "red",
                })
            elif last_run["finished_at"]:
                result.append({
                    "key": "last_run_added",
                    "label": "Last run added",
                    "value": last_run["items_added"],
                    "unit": "cards",
                })

        # Total source runs
        total_runs = await p.fetchval("SELECT COUNT(*) FROM source_runs")
        result.append({
            "key": "total_runs",
            "label": "Total ingestion runs",
            "value": total_runs,
            "unit": "runs",
        })

        # -- Qross / Trivia metrics -------------------------------------------

        trivia_categories = await p.fetchval(
            "SELECT COUNT(*) FROM decks WHERE kind = 'trivia' "
            "AND COALESCE(properties->>'status', 'published') = 'published'"
        )
        trivia_questions = await p.fetchval(
            "SELECT COUNT(*) FROM cards c JOIN decks d ON d.id = c.deck_id "
            "WHERE d.kind = 'trivia' "
            "AND COALESCE(d.properties->>'status', 'published') = 'published'"
        )
        avg_per_cat = round(trivia_questions / trivia_categories, 1) if trivia_categories else 0
        smallest_cat = await p.fetchrow(
            "SELECT d.title, d.card_count FROM decks d "
            "WHERE d.kind = 'trivia' "
            "AND COALESCE(d.properties->>'status', 'published') = 'published' "
            "ORDER BY d.card_count ASC LIMIT 1"
        )

        result.extend([
            {"key": "trivia_categories", "label": "Trivia categories", "value": trivia_categories, "unit": "topics"},
            {"key": "trivia_questions", "label": "Trivia questions", "value": trivia_questions, "unit": "questions"},
            {"key": "trivia_avg_per_cat", "label": "Avg questions/topic", "value": avg_per_cat, "unit": "avg"},
        ])
        if smallest_cat:
            result.append({
                "key": "trivia_smallest",
                "label": f"Smallest: {smallest_cat['title']}",
                "value": smallest_cat["card_count"],
                "unit": "questions",
                "warn_below": 50,
            })

        # -- Question reports -------------------------------------------------

        report_count = await get_report_count()
        result.append({
            "key": "question_reports",
            "label": "Question reports",
            "value": report_count,
            "unit": "reports",
        })

        # -- Database health --------------------------------------------------

        db_size = await p.fetchval(
            "SELECT pg_database_size(current_database())"
        )
        result.append({
            "key": "db_size",
            "label": "Database size",
            "value": round(db_size / 1_048_576, 1) if db_size else 0,
            "unit": "MB",
        })

        active_conns = await p.fetchval(
            "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        result.append({
            "key": "db_connections",
            "label": "DB connections",
            "value": active_conns or 0,
            "unit": "conns",
            "warn_above": 50,
        })

        return {"metrics": result}

    except Exception as exc:
        logger.exception("Error fetching metrics")
        return JSONResponse(
            status_code=500,
            content={"metrics": [], "error": f"Database error: {exc}"},
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run() -> None:
    uvicorn.run("server.app:app", host="127.0.0.1", port=PORT, reload=False)


if __name__ == "__main__":
    run()
