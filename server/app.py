"""card-engine — Unified content backend for Flasherz and Alities apps."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.db import close_pool, get_pool, get_stats, init_pool

logger = logging.getLogger("card_engine")

PORT = int(os.environ.get("CE_PORT", "9810"))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
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
# Include adapter routers
# ---------------------------------------------------------------------------

from server.adapters.generic import router as generic_router  # noqa: E402
from server.adapters.flashcards import router as flashcards_router  # noqa: E402
from server.adapters.trivia import router as trivia_router  # noqa: E402
from server.adapters.studio import router as studio_router  # noqa: E402
from server.providers.routes import router as ingestion_router  # noqa: E402
from server.family.routes import router as family_router  # noqa: E402

app.include_router(generic_router)
app.include_router(flashcards_router)
app.include_router(trivia_router)
app.include_router(studio_router)
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
        stats = await get_stats()
        return {
            "metrics": [
                {"key": "total_decks", "label": "Total Decks", "value": stats["total_decks"], "unit": "count"},
                {"key": "total_cards", "label": "Total Cards", "value": stats["total_cards"], "unit": "count"},
                {"key": "total_sources", "label": "Source Providers", "value": stats["total_sources"], "unit": "count"},
            ],
            "decks_by_kind": stats["decks_by_kind"],
        }
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
