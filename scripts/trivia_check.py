#!/usr/bin/env python3
"""trivia-check — CLI tool for trivia quality control.

Commands:
  trivia-check dedup         Scan and quarantine duplicate questions
  trivia-check veracity      Verify trivia facts via LLM
  trivia-check aiq           Find/delete answer-in-question cards
  trivia-check scan          Run all quality checks
  trivia-check quarantine    Manage quarantined cards
  trivia-check stats         Quality control statistics

Global flags:
  --dry-run    Report what would happen without making changes
  --server     Base URL of card-engine (default: http://localhost:9810)
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


DEFAULT_SERVER = "http://localhost:9810"


def _api(method: str, path: str, server: str, params: dict | None = None) -> dict:
    """Make an API call to card-engine."""
    url = f"{server}{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})

    req = Request(url, method=method)
    req.add_header("Accept", "application/json")

    try:
        with urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()
        print(f"Error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        print(f"Is card-engine running at {server}?", file=sys.stderr)
        sys.exit(1)


def _print_table(headers: list[str], rows: list[list[str]], max_widths: dict[int, int] | None = None) -> None:
    """Print a formatted table to stdout."""
    if not rows:
        print("  (no results)")
        return

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))

    # Apply max widths
    if max_widths:
        for i, mw in max_widths.items():
            if i < len(widths):
                widths[i] = min(widths[i], mw)

    # Format
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            s = str(cell)
            if max_widths and i in max_widths and len(s) > max_widths[i]:
                s = s[:max_widths[i] - 3] + "..."
            cells.append(s)
        print(fmt.format(*cells))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_dedup(args: argparse.Namespace) -> None:
    """Scan for and handle duplicate questions."""
    print(f"Scanning for duplicates (threshold={args.threshold})...")

    if args.dry_run:
        # Scan only
        data = _api("POST", "/api/v1/quality/dedup/scan", args.server,
                     {"threshold": args.threshold})
    else:
        data = _api("POST", "/api/v1/quality/dedup/purge", args.server,
                     {"threshold": args.threshold, "dry_run": args.dry_run})

    print(f"\nTotal cards scanned: {data['total_cards']}")
    print(f"Exact duplicate clusters: {data.get('exact_duplicate_clusters', data.get('exact_clusters', 0))}")
    print(f"Near-duplicate clusters: {data.get('near_duplicate_clusters', data.get('near_clusters', 0))}")

    if args.dry_run:
        total_dupes = data.get("total_duplicates", 0)
        print(f"Total duplicates found: {total_dupes}")
        print("\n[DRY RUN] No changes made.\n")

        # Show clusters
        for label, key in [("EXACT", "exact_clusters"), ("NEAR", "near_clusters")]:
            clusters = data.get(key, [])
            if clusters:
                print(f"\n{label} DUPLICATE CLUSTERS:")
                for i, c in enumerate(clusters, 1):
                    print(f"\n  Cluster {i} (similarity={c['similarity']}):")
                    for q, a in zip(c["questions"], c["correct_answers"]):
                        print(f"    Q: {q[:80]}")
                        print(f"    A: {a}")
                        print()
    else:
        quarantined = data.get("quarantined", data.get("would_quarantine", 0))
        print(f"Cards quarantined: {quarantined}")


def cmd_veracity(args: argparse.Namespace) -> None:
    """Check trivia facts via LLM."""
    params = {
        "model": args.model,
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "dry_run": args.dry_run,
    }
    if args.limit:
        params["limit"] = args.limit
    if args.category:
        params["category"] = args.category

    print(f"Running veracity check (model={args.model}, limit={args.limit or 'all'})...")
    data = _api("POST", "/api/v1/quality/veracity/check", args.server, params)

    print(f"\nModel: {data['model']}")
    print(f"Total checked: {data['total_checked']}")
    print(f"Passed: {data['passed']}")
    print(f"Failed: {data['failed']}")
    print(f"Uncertain: {data['uncertain']}")
    print(f"Errors: {data['errors']}")
    print(f"Elapsed: {data['elapsed_seconds']}s")

    if args.dry_run:
        print("\n[DRY RUN] No cards were quarantined.\n")

    # Show failures
    failures = [c for c in data.get("checks", []) if c["verdict"] == "fail"]
    if failures:
        print(f"\nFAILED CARDS ({len(failures)}):")
        headers = ["Topic", "Question", "Confidence", "Issues"]
        rows = []
        for c in failures:
            issues = "; ".join(c["issues"]) if c["issues"] else c["notes"]
            rows.append([c["topic"], c["question"], str(c["confidence"]), issues])
        _print_table(headers, rows, max_widths={1: 60, 3: 60})


def cmd_aiq(args: argparse.Namespace) -> None:
    """Find and delete answer-in-question cards."""
    print("Scanning for answer-in-question cards...")
    data = _api("POST", "/api/v1/quality/answer-in-question/scan", args.server,
                {"dry_run": args.dry_run})

    print(f"\nTotal scanned: {data['total_scanned']}")
    print(f"Matches found: {data['matches_found']}")

    if args.dry_run:
        print("\n[DRY RUN] No cards deleted.\n")
    else:
        print(f"Deleted: {data['deleted']}")

    if data["matches"]:
        print(f"\nMATCHES:")
        headers = ["Topic", "Question", "Answer"]
        rows = [[m["topic"], m["question"], m["correct_answer"]] for m in data["matches"]]
        _print_table(headers, rows, max_widths={1: 60, 2: 30})


def cmd_scan(args: argparse.Namespace) -> None:
    """Run all quality checks."""
    print("=" * 60)
    print("FULL QUALITY SCAN")
    print("=" * 60)

    print("\n--- Answer-in-Question ---")
    cmd_aiq(args)

    print("\n--- Deduplication ---")
    cmd_dedup(args)

    if not args.skip_veracity:
        print("\n--- Veracity Check ---")
        cmd_veracity(args)
    else:
        print("\n--- Veracity Check (skipped) ---")

    print("\n--- Stats ---")
    cmd_stats(args)


def cmd_quarantine(args: argparse.Namespace) -> None:
    """Manage quarantined cards."""
    if args.qaction == "list":
        params = {"limit": args.limit, "offset": args.offset}
        if args.reason:
            params["reason"] = args.reason
        data = _api("GET", "/api/v1/quality/quarantine", args.server, params)

        print(f"Quarantined cards: {data['total']} (showing {data['offset']+1}-{data['offset']+len(data['items'])})\n")
        headers = ["ID", "Topic", "Question", "Reason"]
        rows = [
            [c["id"][:8], c["topic"], c["question"], c["quarantine_reason"] or "unknown"]
            for c in data["items"]
        ]
        _print_table(headers, rows, max_widths={2: 60, 3: 30})

    elif args.qaction == "restore":
        if not args.card_id:
            print("Error: card_id required", file=sys.stderr)
            sys.exit(1)
        data = _api("POST", f"/api/v1/quality/quarantine/{args.card_id}/restore", args.server)
        print(f"Restored: {data['card_id']}")

    elif args.qaction == "delete":
        if not args.card_id:
            print("Error: card_id required", file=sys.stderr)
            sys.exit(1)
        data = _api("DELETE", f"/api/v1/quality/quarantine/{args.card_id}", args.server)
        print(f"Deleted: {data['card_id']}")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show quality control statistics."""
    data = _api("GET", "/api/v1/quality/stats", args.server)

    print(f"\nQuality Statistics:")
    headers = ["Metric", "Value"]
    rows = [
        ["Total trivia cards", str(data["total_trivia_cards"])],
        ["Active cards", str(data["active_cards"])],
        ["Quarantined cards", str(data["quarantined_cards"])],
        ["Veracity checked", str(data["veracity_checked"])],
        ["Veracity unchecked", str(data["veracity_unchecked"])],
    ]
    _print_table(headers, rows)

    breakdown = data.get("quarantine_breakdown", [])
    if breakdown:
        print(f"\nQuarantine Breakdown:")
        headers = ["Reason", "Count"]
        rows = [[b["reason"], str(b["count"])] for b in breakdown]
        _print_table(headers, rows)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="trivia-check",
        description="Trivia quality control CLI for card-engine",
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="card-engine base URL")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no changes")

    sub = parser.add_subparsers(dest="command", required=True)

    # dedup
    p_dedup = sub.add_parser("dedup", help="Scan and quarantine duplicates")
    p_dedup.add_argument("--threshold", type=float, default=0.85, help="Similarity threshold (0.5-1.0)")

    # veracity
    p_ver = sub.add_parser("veracity", help="Verify trivia facts via LLM")
    p_ver.add_argument("--model", default="claude-haiku",
                       choices=["claude-haiku", "claude-sonnet", "gpt-4o-mini", "gpt-4o"])
    p_ver.add_argument("--batch-size", type=int, default=20)
    p_ver.add_argument("--concurrency", type=int, default=5)
    p_ver.add_argument("--limit", type=int, default=None, help="Max cards to check")
    p_ver.add_argument("--category", default=None, help="Filter by category")

    # aiq
    sub.add_parser("aiq", help="Find/delete answer-in-question cards")

    # scan
    p_scan = sub.add_parser("scan", help="Run all quality checks")
    p_scan.add_argument("--threshold", type=float, default=0.85)
    p_scan.add_argument("--model", default="claude-haiku",
                        choices=["claude-haiku", "claude-sonnet", "gpt-4o-mini", "gpt-4o"])
    p_scan.add_argument("--batch-size", type=int, default=20)
    p_scan.add_argument("--concurrency", type=int, default=5)
    p_scan.add_argument("--limit", type=int, default=None)
    p_scan.add_argument("--category", default=None)
    p_scan.add_argument("--skip-veracity", action="store_true", help="Skip veracity check")

    # quarantine
    p_q = sub.add_parser("quarantine", help="Manage quarantined cards")
    q_sub = p_q.add_subparsers(dest="qaction", required=True)

    p_ql = q_sub.add_parser("list", help="List quarantined cards")
    p_ql.add_argument("--limit", type=int, default=50)
    p_ql.add_argument("--offset", type=int, default=0)
    p_ql.add_argument("--reason", default=None)

    p_qr = q_sub.add_parser("restore", help="Restore a quarantined card")
    p_qr.add_argument("card_id", help="Card UUID to restore")

    p_qd = q_sub.add_parser("delete", help="Permanently delete a quarantined card")
    p_qd.add_argument("card_id", help="Card UUID to delete")

    # stats
    sub.add_parser("stats", help="Quality control statistics")

    args = parser.parse_args()

    commands = {
        "dedup": cmd_dedup,
        "veracity": cmd_veracity,
        "aiq": cmd_aiq,
        "scan": cmd_scan,
        "quarantine": cmd_quarantine,
        "stats": cmd_stats,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
