"""Quality control API endpoints — dedup, veracity, answer-in-question, quarantine."""

from __future__ import annotations

import logging
import time
import uuid
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from server.db import get_pool

logger = logging.getLogger("card_engine.quality")

router = APIRouter(prefix="/api/v1/quality", tags=["quality"])

# ---------------------------------------------------------------------------
# In-memory job registry for background operations
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}  # job_id -> {status, result, started_at, ...}


# ---------------------------------------------------------------------------
# Dedup endpoints
# ---------------------------------------------------------------------------

_BATCH = 3000  # cards processed per LATERAL query


async def _run_dedup_trgm(
    job_id: str,
    threshold: float,
    category: str | None,
    delete: bool,
    concurrency: int,
) -> None:
    """Background task: batched LATERAL-join dedup using pg_trgm GIN index.

    Processes cards in batches of _BATCH to avoid long-running connections.
    Each batch: for each card, use GIN to find most similar older card O(log n).
    """
    pool = get_pool()
    job = _jobs[job_id]
    job["status"] = "running"

    try:
        t_start = time.monotonic()

        # Fetch ordered card IDs + timestamps once
        cat_where = "AND d.title = $1" if category else ""
        id_params = [category] if category else []
        id_rows = await pool.fetch(
            f"SELECT c.id, c.created_at FROM cards c "
            f"JOIN decks d ON d.id = c.deck_id AND d.kind = 'trivia' {cat_where} "
            f"ORDER BY c.created_at",
            *id_params,
            timeout=60,
        )
        all_cards = list(id_rows)
        total = len(all_cards)
        job["total"] = total

        pairs: list[dict] = []
        seen_newer: set[str] = set()

        cat_filter_o = "AND od.kind = 'trivia'" + (f" AND od.title = $3" if category else "")
        lateral_sql = f"""
            SELECT
                c.id::text           AS newer_id,
                c.question           AS newer_q,
                match.id::text       AS older_id,
                match.question       AS older_q,
                match.sim            AS sim,
                d.title              AS category
            FROM cards c
            JOIN decks d ON d.id = c.deck_id AND d.kind = 'trivia'
            CROSS JOIN LATERAL (
                SELECT o.id, o.question,
                       similarity(c.question, o.question) AS sim
                FROM cards o
                JOIN decks od ON od.id = o.deck_id {cat_filter_o}
                WHERE o.id <> c.id
                  AND o.created_at <= c.created_at
                  AND c.question % o.question
                ORDER BY similarity(c.question, o.question) DESC
                LIMIT 1
            ) match
            WHERE c.id = ANY($2::uuid[])
              AND match.sim >= $1
            ORDER BY match.sim DESC
        """

        for offset in range(0, total, _BATCH):
            batch_ids = [r["id"] for r in all_cards[offset : offset + _BATCH]]
            params = [threshold, batch_ids]
            if category:
                params.append(category)

            async with pool.acquire() as conn:
                await conn.execute(
                    f"SET pg_trgm.similarity_threshold = {threshold}"
                )
                rows = await conn.fetch(lateral_sql, *params, timeout=300)

            for r in rows:
                nid = r["newer_id"]
                if nid not in seen_newer:
                    seen_newer.add(nid)
                    pairs.append({
                        "newer_id": nid,
                        "newer_q":  r["newer_q"],
                        "older_id": r["older_id"],
                        "older_q":  r["older_q"],
                        "sim":      float(r["sim"]),
                        "category": r["category"],
                    })

            job["checked"] = min(offset + _BATCH, total)
            job["found"] = len(pairs)
            logger.info(
                "dedup/trgm %s batch %d/%d: %d dupes so far",
                job_id, offset + _BATCH, total, len(pairs),
            )

        elapsed = time.monotonic() - t_start
        deleted = 0

        if delete and pairs:
            to_delete = [p["newer_id"] for p in pairs]
            for i in range(0, len(to_delete), 200):
                batch = to_delete[i : i + 200]
                result = await pool.execute(
                    "DELETE FROM cards WHERE id::text = ANY($1::text[])", batch
                )
                deleted += int(result.split()[-1])

        cat_summary: dict[str, int] = {}
        for p in pairs:
            cat_summary[p["category"]] = cat_summary.get(p["category"], 0) + 1

        job.update({
            "status": "done",
            "total_questions": total,
            "duplicates_found": len(pairs),
            "deleted": deleted,
            "dry_run": not delete,
            "elapsed_seconds": round(elapsed, 1),
            "by_category": cat_summary,
            "pairs": pairs[:500],
        })
        logger.info("dedup/trgm job %s done: %d dupes in %.1fs", job_id, len(pairs), elapsed)

    except Exception as exc:
        logger.exception("dedup/trgm job %s failed: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


@router.post("/dedup/trgm")
async def dedup_trgm(
    background_tasks: BackgroundTasks,
    threshold: float = Query(0.65, ge=0.5, le=1.0),
    category: str | None = Query(None),
    delete: bool = Query(False),
    concurrency: int = Query(16, ge=1, le=32),
):
    """Start a background trgm dedup job. Returns job_id immediately.

    Poll GET /api/v1/quality/dedup/trgm/{job_id} for status and results.
    """
    pool = get_pool()
    has_index = await pool.fetchval(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_cards_question_trgm'"
    )
    if not has_index:
        return JSONResponse(status_code=503, content={
            "error": "pg_trgm GIN index not found. Run schema/012_trgm_index.sql first."
        })

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "queued",
        "job_id": job_id,
        "threshold": threshold,
        "category": category,
        "delete": delete,
        "concurrency": concurrency,
        "started_at": time.time(),
        "checked": 0,
        "found": 0,
        "total": 0,
    }
    background_tasks.add_task(
        _run_dedup_trgm, job_id, threshold, category, delete, concurrency
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "poll": f"/api/v1/quality/dedup/trgm/{job_id}",
        "note": "SQL LATERAL join — runs server-side, poll for completion",
    }


@router.get("/dedup/trgm/{job_id}")
async def dedup_trgm_status(job_id: str):
    """Poll the status of a dedup/trgm job."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "job not found"})
    return job


@router.post("/dedup/scan")
async def dedup_scan(
    threshold: float = Query(0.85, ge=0.5, le=1.0, description="Cosine similarity threshold"),
):
    """Scan the full trivia corpus for exact and near duplicates."""
    from server.providers.quality.dedup import scan_duplicates

    pool = get_pool()
    result = await scan_duplicates(pool, threshold=threshold)

    return {
        "total_cards": result.total_cards,
        "exact_duplicate_clusters": len(result.exact_clusters),
        "near_duplicate_clusters": len(result.near_clusters),
        "total_duplicates": result.total_duplicates,
        "elapsed_seconds": result.elapsed_seconds,
        "exact_clusters": [
            {
                "card_ids": c.card_ids,
                "questions": c.questions,
                "correct_answers": c.correct_answers,
                "similarity": c.similarity,
                "match_type": c.match_type,
            }
            for c in result.exact_clusters
        ],
        "near_clusters": [
            {
                "card_ids": c.card_ids,
                "questions": c.questions,
                "correct_answers": c.correct_answers,
                "similarity": c.similarity,
                "match_type": c.match_type,
            }
            for c in result.near_clusters
        ],
    }


@router.post("/dedup/purge")
async def dedup_purge(
    threshold: float = Query(0.85, ge=0.5, le=1.0),
    dry_run: bool = Query(False, description="Report what would happen without making changes"),
):
    """Quarantine duplicate cards (keeps the first in each cluster)."""
    from server.providers.quality.dedup import purge_duplicates, scan_duplicates

    pool = get_pool()
    result = await scan_duplicates(pool, threshold=threshold)
    summary = await purge_duplicates(pool, result, dry_run=dry_run)

    return {
        "total_cards": result.total_cards,
        "exact_clusters": len(result.exact_clusters),
        "near_clusters": len(result.near_clusters),
        **summary,
    }


# ---------------------------------------------------------------------------
# Veracity endpoints
# ---------------------------------------------------------------------------

@router.post("/veracity/check")
async def veracity_check(
    model: str = Query("claude-haiku", description="LLM model: claude-haiku, claude-sonnet, gpt-4o-mini, gpt-4o"),
    batch_size: int = Query(20, ge=1, le=100),
    concurrency: int = Query(5, ge=1, le=20),
    limit: int | None = Query(None, ge=1, description="Max cards to check"),
    category: str | None = Query(None, description="Filter by category/topic"),
    dry_run: bool = Query(False, description="Check but don't quarantine failures"),
):
    """Run veracity checks on trivia cards using an LLM."""
    from server.providers.quality.veracity import ModelProvider, run_veracity_check

    pool = get_pool()
    model_enum = ModelProvider(model)
    result = await run_veracity_check(
        pool,
        model=model_enum,
        batch_size=batch_size,
        concurrency=concurrency,
        limit=limit,
        category=category,
        dry_run=dry_run,
    )

    return {
        "model": result.model,
        "total_checked": result.total_checked,
        "passed": result.passed,
        "failed": result.failed,
        "uncertain": result.uncertain,
        "errors": result.errors,
        "elapsed_seconds": result.elapsed_seconds,
        "dry_run": dry_run,
        "checks": [
            {
                "card_id": c.card_id,
                "question": c.question,
                "topic": c.topic,
                "verdict": c.verdict.value,
                "confidence": c.confidence,
                "issues": c.issues,
                "correct_answer_valid": c.correct_answer_valid,
                "wrong_answers_valid": c.wrong_answers_valid,
                "explanation_valid": c.explanation_valid,
                "notes": c.notes,
                "error": c.error,
            }
            for c in result.checks
        ],
    }


# ---------------------------------------------------------------------------
# Answer-in-question endpoints
# ---------------------------------------------------------------------------

@router.post("/answer-in-question/scan")
async def aiq_scan(
    dry_run: bool = Query(False, description="Report matches without deleting"),
):
    """Find and auto-delete trivia questions where the answer appears in the question."""
    from server.providers.quality.answer_in_question import scan_answer_in_question

    pool = get_pool()
    result = await scan_answer_in_question(pool, dry_run=dry_run)

    return {
        "total_scanned": result.total_scanned,
        "matches_found": len(result.matches),
        "deleted": result.deleted,
        "dry_run": result.dry_run,
        "elapsed_seconds": result.elapsed_seconds,
        "matches": [
            {
                "card_id": m.card_id,
                "question": m.question,
                "correct_answer": m.correct_answer,
                "topic": m.topic,
            }
            for m in result.matches
        ],
    }


# ---------------------------------------------------------------------------
# Quarantine management
# ---------------------------------------------------------------------------

@router.get("/quarantine")
async def quarantine_list(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    reason: str | None = Query(None, description="Filter by quarantine reason prefix"),
):
    """List quarantined cards with pagination."""
    pool = get_pool()

    where = "WHERE c.quarantined = TRUE "
    params: list = []
    idx = 1

    if reason:
        where += f"AND c.quarantine_reason LIKE ${idx} "
        params.append(f"{reason}%")
        idx += 1

    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM cards c {where}", *params
    )

    params.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT c.id::text, c.question, c.properties, c.quarantine_reason, "
        f"       c.created_at, d.title AS topic "
        f"FROM cards c "
        f"JOIN decks d ON d.id = c.deck_id "
        f"{where}"
        f"ORDER BY c.created_at DESC "
        f"LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )

    items = []
    for r in rows:
        raw_props = r["properties"]
        props = raw_props if isinstance(raw_props, dict) else {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)
        answers = [c["text"] if isinstance(c, dict) else str(c) for c in choices]
        correct = answers[correct_idx] if correct_idx < len(answers) else ""

        items.append({
            "id": r["id"],
            "question": r["question"],
            "answers": answers,
            "correct_answer": correct,
            "topic": r["topic"],
            "quarantine_reason": r["quarantine_reason"],
            "created_at": r["created_at"].isoformat(),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/quarantine/{card_id}/restore")
async def quarantine_restore(card_id: UUID):
    """Un-quarantine a card, making it visible in gamedata again."""
    pool = get_pool()
    result = await pool.execute(
        "UPDATE cards SET quarantined = FALSE, quarantine_reason = NULL "
        "WHERE id = $1 AND quarantined = TRUE",
        card_id,
    )
    if result == "UPDATE 1":
        return {"status": "restored", "card_id": str(card_id)}
    return JSONResponse(status_code=404, content={"error": "Card not found or not quarantined"})


@router.delete("/quarantine/{card_id}")
async def quarantine_delete(card_id: UUID):
    """Permanently delete a quarantined card."""
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM cards WHERE id = $1 AND quarantined = TRUE",
        card_id,
    )
    if result == "DELETE 1":
        return {"status": "deleted", "card_id": str(card_id)}
    return JSONResponse(status_code=404, content={"error": "Card not found or not quarantined"})


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats")
async def quality_stats():
    """Quality control statistics overview."""
    pool = get_pool()

    total_trivia = await pool.fetchval(
        "SELECT COUNT(*) FROM cards c JOIN decks d ON d.id = c.deck_id "
        "WHERE d.kind = 'trivia'"
    )
    quarantined = await pool.fetchval(
        "SELECT COUNT(*) FROM cards WHERE quarantined = TRUE"
    )
    active = total_trivia - quarantined

    # Breakdown by quarantine reason
    reason_rows = await pool.fetch(
        "SELECT quarantine_reason, COUNT(*) AS cnt "
        "FROM cards WHERE quarantined = TRUE "
        "GROUP BY quarantine_reason ORDER BY cnt DESC"
    )

    veracity_checked = await pool.fetchval(
        "SELECT COUNT(*) FROM cards c JOIN decks d ON d.id = c.deck_id "
        "WHERE d.kind = 'trivia' AND (c.properties->>'veracity_checked')::boolean = TRUE"
    )

    return {
        "total_trivia_cards": total_trivia,
        "active_cards": active,
        "quarantined_cards": quarantined,
        "veracity_checked": veracity_checked or 0,
        "veracity_unchecked": active - (veracity_checked or 0),
        "quarantine_breakdown": [
            {"reason": r["quarantine_reason"] or "unknown", "count": r["cnt"]}
            for r in reason_rows
        ],
    }


# ---------------------------------------------------------------------------
# Web review page (lightweight HTML)
# ---------------------------------------------------------------------------

_REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quarantine Review — card-engine</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
  h1 { font-size: 1.5rem; margin-bottom: 16px; color: #f8fafc; }
  .stats { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat { background: #1e293b; padding: 12px 20px; border-radius: 8px; }
  .stat-value { font-size: 1.8rem; font-weight: 700; color: #38bdf8; }
  .stat-label { font-size: 0.8rem; color: #94a3b8; }
  .filters { margin-bottom: 16px; display: flex; gap: 8px; align-items: center; }
  .filters select, .filters button { padding: 6px 12px; border-radius: 6px; border: 1px solid #334155;
    background: #1e293b; color: #e2e8f0; cursor: pointer; }
  .filters button:hover { background: #334155; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  th { text-align: left; padding: 8px 12px; background: #1e293b; color: #94a3b8; font-size: 0.75rem;
       text-transform: uppercase; letter-spacing: 0.05em; }
  td { padding: 10px 12px; border-bottom: 1px solid #1e293b; font-size: 0.9rem; vertical-align: top; }
  tr:hover { background: #1e293b; }
  .q-text { max-width: 400px; }
  .reason { color: #f97316; font-size: 0.8rem; }
  .topic { color: #a78bfa; font-size: 0.8rem; }
  .answers { font-size: 0.8rem; color: #94a3b8; }
  .correct { color: #4ade80; font-weight: 600; }
  .btn { padding: 4px 10px; border-radius: 4px; border: none; cursor: pointer; font-size: 0.8rem; }
  .btn-restore { background: #059669; color: white; }
  .btn-restore:hover { background: #047857; }
  .btn-delete { background: #dc2626; color: white; }
  .btn-delete:hover { background: #b91c1c; }
  .actions { display: flex; gap: 6px; }
  .pagination { display: flex; gap: 8px; justify-content: center; margin-top: 16px; }
  .pagination button { padding: 6px 14px; border-radius: 6px; border: 1px solid #334155;
    background: #1e293b; color: #e2e8f0; cursor: pointer; }
  .pagination button:disabled { opacity: 0.4; cursor: default; }
  .pagination button:not(:disabled):hover { background: #334155; }
  .toast { position: fixed; top: 20px; right: 20px; padding: 10px 20px; border-radius: 8px;
           background: #059669; color: white; font-size: 0.9rem; display: none; z-index: 100; }
  .toast.error { background: #dc2626; }
</style>
</head>
<body>
<h1>Quarantine Review</h1>
<div class="stats" id="stats"></div>
<div class="filters">
  <select id="reason-filter">
    <option value="">All reasons</option>
    <option value="duplicate">Duplicates</option>
    <option value="veracity_fail">Veracity failures</option>
  </select>
  <button onclick="loadCards()">Filter</button>
  <span style="margin-left: auto; font-size: 0.8rem; color: #64748b;" id="page-info"></span>
</div>
<table>
  <thead>
    <tr>
      <th>Topic</th>
      <th>Question</th>
      <th>Answers</th>
      <th>Reason</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody id="card-body"></tbody>
</table>
<div class="pagination">
  <button id="btn-prev" onclick="prevPage()" disabled>&larr; Prev</button>
  <button id="btn-next" onclick="nextPage()">Next &rarr;</button>
</div>
<div class="toast" id="toast"></div>

<script>
const PAGE_SIZE = 25;
let offset = 0;
let total = 0;

async function loadStats() {
  const r = await fetch('/api/v1/quality/stats');
  const d = await r.json();
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-value">${d.active_cards}</div><div class="stat-label">Active</div></div>
    <div class="stat"><div class="stat-value">${d.quarantined_cards}</div><div class="stat-label">Quarantined</div></div>
    <div class="stat"><div class="stat-value">${d.veracity_checked}</div><div class="stat-label">Veracity Checked</div></div>
    <div class="stat"><div class="stat-value">${d.veracity_unchecked}</div><div class="stat-label">Unchecked</div></div>
  `;
}

async function loadCards() {
  const reason = document.getElementById('reason-filter').value;
  let url = `/api/v1/quality/quarantine?limit=${PAGE_SIZE}&offset=${offset}`;
  if (reason) url += `&reason=${reason}`;
  const r = await fetch(url);
  const d = await r.json();
  total = d.total;

  document.getElementById('page-info').textContent =
    `${offset + 1}\u2013${Math.min(offset + PAGE_SIZE, total)} of ${total}`;
  document.getElementById('btn-prev').disabled = offset === 0;
  document.getElementById('btn-next').disabled = offset + PAGE_SIZE >= total;

  const tbody = document.getElementById('card-body');
  tbody.innerHTML = d.items.map(c => `<tr id="row-${c.id}">
    <td class="topic">${esc(c.topic)}</td>
    <td class="q-text">${esc(c.question)}</td>
    <td class="answers">${c.answers.map((a, i) =>
      a === c.correct_answer ? `<span class="correct">${esc(a)}</span>` : esc(a)
    ).join('<br>')}</td>
    <td class="reason">${esc(c.quarantine_reason || 'unknown')}</td>
    <td class="actions">
      <button class="btn btn-restore" onclick="restore('${c.id}')">Restore</button>
      <button class="btn btn-delete" onclick="del('${c.id}')">Delete</button>
    </td>
  </tr>`).join('');
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function nextPage() { offset += PAGE_SIZE; loadCards(); }
function prevPage() { offset = Math.max(0, offset - PAGE_SIZE); loadCards(); }

function toast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2500);
}

async function restore(id) {
  const r = await fetch(`/api/v1/quality/quarantine/${id}/restore`, { method: 'POST' });
  if (r.ok) {
    document.getElementById('row-' + id)?.remove();
    toast('Card restored');
    loadStats();
  } else toast('Restore failed', true);
}

async function del(id) {
  if (!confirm('Permanently delete this card?')) return;
  const r = await fetch(`/api/v1/quality/quarantine/${id}`, { method: 'DELETE' });
  if (r.ok) {
    document.getElementById('row-' + id)?.remove();
    toast('Card deleted');
    loadStats();
  } else toast('Delete failed', true);
}

loadStats();
loadCards();
</script>
</body>
</html>"""


@router.get("/quarantine/review", response_class=HTMLResponse)
async def quarantine_review():
    """Lightweight web UI for reviewing quarantined cards."""
    return _REVIEW_HTML
