"""Player and session adapter routes — device-based identity and session sharing."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query

from server.db import (
    clear_player_history,
    get_player,
    get_player_stats,
    get_session_by_share_code,
    update_session_properties,
    upsert_player,
)
from server.models import (
    ChallengeOut,
    GameDataOut,
    PlayerIn,
    PlayerOut,
    PlayerStatsOut,
    ResetOut,
    SessionUpdateIn,
    SessionUpdateOut,
)

router = APIRouter(tags=["players"])


# ---------------------------------------------------------------------------
# Player endpoints
# ---------------------------------------------------------------------------

@router.post("/api/v1/players", response_model=PlayerOut)
async def register_player(body: PlayerIn):
    """Register or upsert a player by device_id."""
    row, is_new = await upsert_player(
        device_id=body.device_id,
        display_name=body.display_name,
        properties=body.properties,
    )
    return PlayerOut(
        id=row["id"],
        device_id=row["device_id"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        is_new=is_new,
    )


@router.get("/api/v1/players/{player_id}/stats", response_model=PlayerStatsOut)
async def player_stats(
    player_id: UUID,
    app_id: str = Query("qross", description="App identifier"),
):
    """Get seen-card statistics for a player."""
    player = await get_player(player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    stats = await get_player_stats(player_id, app_id)
    return PlayerStatsOut(
        player_id=player_id,
        total_seen=stats["total_seen"],
        by_category=stats["by_category"],
    )


@router.post("/api/v1/players/{player_id}/reset", response_model=ResetOut)
async def reset_player(
    player_id: UUID,
    app_id: str = Query("qross", description="App identifier (omit to clear all apps)"),
):
    """Clear a player's seen-card history."""
    player = await get_player(player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    cleared = await clear_player_history(player_id, app_id)
    return ResetOut(player_id=player_id, cleared=cleared)


# ---------------------------------------------------------------------------
# Session replay endpoint
# ---------------------------------------------------------------------------

@router.get("/api/v1/sessions/{share_code}")
async def replay_session(share_code: str):
    """Replay a shared session — returns the same challenges in order,
    plus any challenge metadata stored in properties."""
    session, rows = await get_session_by_share_code(share_code)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    challenges: list[ChallengeOut] = []
    for r in rows:
        if r["card_id"] is None:
            continue

        raw_props = r["card_props"]
        props = raw_props if isinstance(raw_props, dict) else {}
        raw_deck_props = r["deck_props"]
        deck_props = raw_deck_props if isinstance(raw_deck_props, dict) else {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)

        answer_texts = [
            c["text"] if isinstance(c, dict) else str(c) for c in choices
        ]
        correct_answer = answer_texts[correct_idx] if correct_idx < len(answer_texts) else ""

        challenges.append(
            ChallengeOut(
                id=str(r["card_id"]),
                topic=r["title"],
                pic=deck_props.get("pic", "questionmark.circle"),
                question=r["question"],
                answers=answer_texts,
                correct=correct_answer,
                explanation=props.get("explanation", ""),
                hint=props.get("hint", ""),
                aisource=props.get("aisource", "card-engine"),
                date=r["source_date"].isoformat() if r["source_date"] else "",
                ai_difficulty=props.get("ai_difficulty"),
            )
        )

    result = GameDataOut(
        id=str(session["id"]),
        generated=session["created_at"].isoformat(),
        challenges=challenges,
    ).model_dump()

    # Include challenge metadata from session properties
    session_props = session["properties"] if isinstance(session["properties"], dict) else {}
    result["challenge"] = session_props.get("challenge")

    return result


@router.patch("/api/v1/sessions/{session_id}", response_model=SessionUpdateOut)
async def patch_session(session_id: UUID, body: SessionUpdateIn):
    """Update session properties — used to save challenge metadata after a game."""
    row = await update_session_properties(session_id, body.properties)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionUpdateOut(id=str(row["id"]), properties=row["properties"])
