"""Veracity checking via LLM — verify trivia questions are factually correct.

Supports both Claude (Anthropic) and GPT (OpenAI) models.
Checks:
  - Is the stated correct answer actually correct?
  - Are the wrong answers actually wrong?
  - Is the explanation accurate?
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

import asyncpg

logger = logging.getLogger("card_engine.quality.veracity")


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class ModelProvider(str, Enum):
    CLAUDE_HAIKU = "claude-haiku"
    CLAUDE_SONNET = "claude-sonnet"
    GPT_4O_MINI = "gpt-4o-mini"
    GPT_4O = "gpt-4o"


# Map friendly names to actual model IDs
_MODEL_IDS = {
    ModelProvider.CLAUDE_HAIKU: "claude-haiku-4-5-20251001",
    ModelProvider.CLAUDE_SONNET: "claude-sonnet-4-6",
    ModelProvider.GPT_4O_MINI: "gpt-4o-mini",
    ModelProvider.GPT_4O: "gpt-4o",
}

_SYSTEM_PROMPT = """You are a trivia fact-checker. You will be given a trivia question with multiple choice answers, one marked as correct, and an explanation.

Your job is to verify:
1. Is the stated correct answer actually factually correct?
2. Are the other answers actually wrong? (If any "wrong" answer is also correct, that's a problem.)
3. Is the explanation accurate?

Respond with a JSON object (no markdown, no code fences):
{
  "verdict": "pass" | "fail" | "uncertain",
  "confidence": 0-100,
  "issues": ["list of specific factual problems found, if any"],
  "correct_answer_valid": true | false,
  "wrong_answers_valid": true | false,
  "explanation_valid": true | false,
  "notes": "brief summary of your assessment"
}

Be strict. If the correct answer is wrong, or if any wrong answer is actually correct, verdict must be "fail".
If you cannot verify with confidence, use "uncertain".
Only use "pass" when everything checks out."""


@dataclass
class VeracityCheck:
    card_id: str
    question: str
    correct_answer: str
    wrong_answers: list[str]
    topic: str
    verdict: Verdict = Verdict.UNCERTAIN
    confidence: int = 0
    issues: list[str] = field(default_factory=list)
    correct_answer_valid: bool = True
    wrong_answers_valid: bool = True
    explanation_valid: bool = True
    notes: str = ""
    error: str | None = None


@dataclass
class VeracityResult:
    total_checked: int = 0
    passed: int = 0
    failed: int = 0
    uncertain: int = 0
    errors: int = 0
    checks: list[VeracityCheck] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    model: str = ""


def _build_user_prompt(card: dict) -> str:
    choices = card.get("choices", [])
    correct_idx = card.get("correct_index", 0)

    answers_text = []
    for i, c in enumerate(choices):
        text = c["text"] if isinstance(c, dict) else str(c)
        marker = " [CORRECT]" if i == correct_idx else ""
        answers_text.append(f"  {chr(65+i)}. {text}{marker}")

    return f"""Topic: {card.get('topic', 'Unknown')}

Question: {card['question']}

Answers:
{chr(10).join(answers_text)}

Explanation: {card.get('explanation', 'None provided')}"""


async def _check_with_claude(card: dict, model_id: str) -> dict:
    """Call Anthropic API for veracity check."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model_id,
        max_tokens=500,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(card)}],
    )

    text = response.content[0].text.strip()
    return json.loads(text)


async def _check_with_openai(card: dict, model_id: str) -> dict:
    """Call OpenAI API for veracity check."""
    import openai

    api_key = os.environ.get("CE_OPENAI_API_KEY")
    if not api_key:
        raise ValueError("CE_OPENAI_API_KEY not set")

    client = openai.AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model_id,
        max_tokens=500,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(card)},
        ],
    )

    text = response.choices[0].message.content.strip()
    return json.loads(text)


async def check_single_card(card: dict, model: ModelProvider) -> VeracityCheck:
    """Check a single card's veracity."""
    check = VeracityCheck(
        card_id=card["id"],
        question=card["question"],
        correct_answer=card.get("correct_answer", ""),
        wrong_answers=card.get("wrong_answers", []),
        topic=card.get("topic", ""),
    )

    try:
        model_id = _MODEL_IDS[model]
        if model in (ModelProvider.CLAUDE_HAIKU, ModelProvider.CLAUDE_SONNET):
            result = await _check_with_claude(card, model_id)
        else:
            result = await _check_with_openai(card, model_id)

        check.verdict = Verdict(result.get("verdict", "uncertain"))
        check.confidence = result.get("confidence", 0)
        check.issues = result.get("issues", [])
        check.correct_answer_valid = result.get("correct_answer_valid", True)
        check.wrong_answers_valid = result.get("wrong_answers_valid", True)
        check.explanation_valid = result.get("explanation_valid", True)
        check.notes = result.get("notes", "")

    except json.JSONDecodeError as e:
        check.error = f"Failed to parse LLM response: {e}"
        check.verdict = Verdict.UNCERTAIN
    except Exception as e:
        check.error = str(e)
        check.verdict = Verdict.UNCERTAIN

    return check


async def load_cards_for_veracity(
    pool: asyncpg.Pool,
    limit: int | None = None,
    category: str | None = None,
    unchecked_only: bool = True,
) -> list[dict]:
    """Load trivia cards for veracity checking."""
    sql = (
        "SELECT c.id::text, c.question, c.properties, d.title AS topic "
        "FROM cards c "
        "JOIN decks d ON d.id = c.deck_id "
        "WHERE d.kind = 'trivia' "
        "  AND c.quarantined = FALSE "
    )
    params: list = []
    idx = 1

    if unchecked_only:
        sql += "  AND NOT COALESCE((c.properties->>'veracity_checked')::boolean, FALSE) "

    if category:
        sql += f"  AND d.title = ${idx} "
        params.append(category)
        idx += 1

    sql += "ORDER BY c.created_at "

    if limit:
        sql += f"LIMIT ${idx} "
        params.append(limit)

    rows = await pool.fetch(sql, *params)

    cards = []
    for r in rows:
        props = r["properties"] or {}
        choices = props.get("choices", [])
        correct_idx = props.get("correct_index", 0)

        correct_answer = ""
        wrong_answers = []
        for i, c in enumerate(choices):
            text = c["text"] if isinstance(c, dict) else str(c)
            if i == correct_idx:
                correct_answer = text
            else:
                wrong_answers.append(text)

        cards.append({
            "id": r["id"],
            "question": r["question"],
            "correct_answer": correct_answer,
            "wrong_answers": wrong_answers,
            "topic": r["topic"],
            "choices": choices,
            "correct_index": correct_idx,
            "explanation": props.get("explanation", ""),
        })
    return cards


async def run_veracity_check(
    pool: asyncpg.Pool,
    model: ModelProvider = ModelProvider.CLAUDE_HAIKU,
    batch_size: int = 20,
    concurrency: int = 5,
    limit: int | None = None,
    category: str | None = None,
    dry_run: bool = False,
) -> VeracityResult:
    """Run veracity checks on trivia cards.

    In dry_run mode, checks are performed but no DB changes are made.
    """
    t0 = time.time()
    cards = await load_cards_for_veracity(pool, limit=limit, category=category)

    result = VeracityResult(model=model.value)
    semaphore = asyncio.Semaphore(concurrency)

    async def check_with_semaphore(card: dict) -> VeracityCheck:
        async with semaphore:
            return await check_single_card(card, model)

    # Process in batches
    for batch_start in range(0, len(cards), batch_size):
        batch = cards[batch_start:batch_start + batch_size]
        tasks = [check_with_semaphore(card) for card in batch]
        checks = await asyncio.gather(*tasks)

        for check in checks:
            result.checks.append(check)
            result.total_checked += 1

            if check.error:
                result.errors += 1
            elif check.verdict == Verdict.PASS:
                result.passed += 1
            elif check.verdict == Verdict.FAIL:
                result.failed += 1
            else:
                result.uncertain += 1

            # Apply results to DB
            if not dry_run and not check.error:
                try:
                    if check.verdict == Verdict.FAIL:
                        # Quarantine failed cards
                        issues_text = "; ".join(check.issues) if check.issues else check.notes
                        await pool.execute(
                            "UPDATE cards SET quarantined = TRUE, "
                            "quarantine_reason = $2, "
                            "properties = jsonb_set("
                            "  jsonb_set(properties, '{veracity_checked}', 'true'), "
                            "  '{veracity_issues}', $3::jsonb"
                            ") "
                            "WHERE id = $1::uuid",
                            check.card_id,
                            f"veracity_fail: {issues_text}",
                            json.dumps(check.issues),
                        )
                    else:
                        # Mark as checked
                        await pool.execute(
                            "UPDATE cards SET properties = jsonb_set("
                            "  properties, '{veracity_checked}', 'true'"
                            ") WHERE id = $1::uuid",
                            check.card_id,
                        )
                except Exception as e:
                    logger.error("Failed to update card %s: %s", check.card_id, e)

    result.elapsed_seconds = round(time.time() - t0, 2)
    return result
