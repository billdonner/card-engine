"""Question reports adapter — generic feedback endpoint for all client apps."""

from __future__ import annotations

from fastapi import APIRouter, Query

from server.db import insert_question_report, list_question_reports
from server.models import QuestionReportIn, QuestionReportOut, ReportsListOut

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.post("", response_model=QuestionReportOut, status_code=201)
async def create_report(body: QuestionReportIn):
    """Submit a question report from any client app."""
    row = await insert_question_report(body.model_dump())
    return QuestionReportOut(
        id=row["id"],
        app_id=row["app_id"],
        challenge_id=row["challenge_id"],
        reported_at=row["reported_at"],
    )


@router.get("", response_model=ReportsListOut)
async def list_reports(
    app_id: str | None = Query(None, description="Filter by app_id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List question reports (admin use). Optional app_id filter."""
    rows, total = await list_question_reports(app_id=app_id, limit=limit, offset=offset)
    reports = [
        QuestionReportOut(
            id=r["id"],
            app_id=r["app_id"],
            challenge_id=r["challenge_id"],
            reported_at=r["reported_at"],
        )
        for r in rows
    ]
    return ReportsListOut(reports=reports, total=total)
