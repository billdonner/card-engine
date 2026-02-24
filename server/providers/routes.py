"""Ingestion control endpoints — start/stop/pause/resume the daemon."""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.db import get_pool
from server.models import IngestionStatusOut, SourceRunOut

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])


def _get_daemon(request: Request):
    return request.app.state.daemon


@router.get("/status", response_model=IngestionStatusOut)
async def ingestion_status(request: Request):
    """Current daemon state and stats."""
    daemon = _get_daemon(request)
    status = daemon.get_status()
    return IngestionStatusOut(
        state=status["state"],
        stats=status["stats"],
        config=status["config"],
    )


@router.post("/start", response_model=IngestionStatusOut)
async def ingestion_start(request: Request):
    """Start the ingestion daemon."""
    daemon = _get_daemon(request)
    msg = await daemon.start()
    status = daemon.get_status()
    return IngestionStatusOut(
        state=status["state"],
        stats=status["stats"],
        config=status["config"],
        message=msg,
    )


@router.post("/stop", response_model=IngestionStatusOut)
async def ingestion_stop(request: Request):
    """Stop the ingestion daemon."""
    daemon = _get_daemon(request)
    msg = await daemon.stop()
    status = daemon.get_status()
    return IngestionStatusOut(
        state=status["state"],
        stats=status["stats"],
        config=status["config"],
        message=msg,
    )


@router.post("/pause", response_model=IngestionStatusOut)
async def ingestion_pause(request: Request):
    """Pause the daemon (finishes current batch, then sleeps)."""
    daemon = _get_daemon(request)
    msg = daemon.pause()
    status = daemon.get_status()
    return IngestionStatusOut(
        state=status["state"],
        stats=status["stats"],
        config=status["config"],
        message=msg,
    )


@router.post("/resume", response_model=IngestionStatusOut)
async def ingestion_resume(request: Request):
    """Resume the daemon from paused state."""
    daemon = _get_daemon(request)
    msg = daemon.resume()
    status = daemon.get_status()
    return IngestionStatusOut(
        state=status["state"],
        stats=status["stats"],
        config=status["config"],
        message=msg,
    )


@router.get("/runs", response_model=list[SourceRunOut])
async def ingestion_runs():
    """Recent source_runs from DB."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT sr.id, sp.name AS provider_name, sr.started_at, sr.finished_at, "
        "sr.items_fetched, sr.items_added, sr.items_skipped, sr.error "
        "FROM source_runs sr "
        "JOIN source_providers sp ON sp.id = sr.provider_id "
        "ORDER BY sr.started_at DESC LIMIT 50"
    )
    return [
        SourceRunOut(
            id=row["id"],
            provider_name=row["provider_name"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            items_fetched=row["items_fetched"],
            items_added=row["items_added"],
            items_skipped=row["items_skipped"],
            error=row["error"],
        )
        for row in rows
    ]
