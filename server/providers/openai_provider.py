"""OpenAI trivia question generator.

Ported from alities-engine AIGeneratorProvider.swift.
Uses httpx.AsyncClient to call the OpenAI chat completions API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import httpx

from server.providers.categories import CANONICAL_CATEGORIES

logger = logging.getLogger("card_engine.openai")

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_MODEL = "gpt-4o-mini"
_TEMPERATURE = 0.8
_MAX_TOKENS = 2000

_SYSTEM_PROMPT = (
    "You are a trivia question generator. Generate unique, factually accurate "
    "trivia questions. Always respond with valid JSON only."
)

_DIFFICULTY_GUIDANCE = {
    "easy": "Questions should be common knowledge that most people would know",
    "medium": "Questions should require some specific knowledge but not be obscure",
    "hard": "Questions should be challenging and require specialized knowledge",
}

# Strip markdown code fences from GPT response
_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)


def _build_prompt(count: int, category: str, difficulty: str) -> str:
    guidance = _DIFFICULTY_GUIDANCE.get(difficulty, _DIFFICULTY_GUIDANCE["medium"])
    return (
        f"Generate {count} unique trivia questions about {category} "
        f"at {difficulty} difficulty level.\n\n"
        "Return a JSON array with this exact structure:\n"
        "[\n"
        "  {\n"
        '    "question": "The question text?",\n'
        '    "correct_answer": "The correct answer",\n'
        '    "incorrect_answers": ["Wrong 1", "Wrong 2", "Wrong 3"],\n'
        '    "explanation": "Brief explanation of why the answer is correct",\n'
        '    "hint": "A subtle clue that helps without giving away the answer"\n'
        "  }\n"
        "]\n\n"
        "Requirements:\n"
        "- Questions must be factually accurate\n"
        "- Each question must have exactly 3 incorrect answers\n"
        "- Incorrect answers should be plausible but clearly wrong\n"
        f"- For {difficulty} difficulty: {guidance}\n"
        "- Return ONLY the JSON array, no other text"
    )


def _parse_response(content: str, category: str, difficulty: str) -> list[dict]:
    """Parse OpenAI response into card dicts with randomized answer position."""
    # Strip markdown fencing
    cleaned = _FENCE_RE.sub("", content).strip()
    # Extract the JSON array
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or start >= end:
        logger.warning("Could not find JSON array in response")
        return []
    json_str = cleaned[start : end + 1]

    try:
        questions = json.loads(json_str)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse OpenAI JSON: %s", exc)
        return []

    results: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question", "")
        correct_answer = q.get("correct_answer", "")
        incorrect = q.get("incorrect_answers", [])
        explanation = q.get("explanation", "")
        hint = q.get("hint", "")

        if not question_text or not correct_answer or len(incorrect) < 3:
            continue

        # Randomize correct answer position (insert at random index 0-3)
        incorrect = incorrect[:3]  # ensure exactly 3
        correct_index = random.randint(0, 3)
        all_answers = list(incorrect)
        all_answers.insert(correct_index, correct_answer)

        choices = [
            {"text": text, "isCorrect": i == correct_index}
            for i, text in enumerate(all_answers)
        ]

        results.append(
            {
                "question": question_text,
                "category": category,
                "difficulty": difficulty,
                "choices": choices,
                "correct_index": correct_index,
                "explanation": explanation,
                "hint": hint,
            }
        )

    return results


async def generate_batch(
    api_key: str,
    category: str,
    difficulty: str,
    count: int = 10,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Call OpenAI and return list of parsed question dicts."""
    prompt = _build_prompt(count, category, difficulty)

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": _TEMPERATURE,
        "max_tokens": _MAX_TOKENS,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)

    try:
        resp = await client.post(_OPENAI_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_response(content, category, difficulty)
    except httpx.HTTPStatusError as exc:
        logger.error("OpenAI HTTP %d: %s", exc.response.status_code, exc.response.text[:200])
        return []
    except Exception as exc:
        logger.error("OpenAI request failed: %s", exc)
        return []
    finally:
        if own_client:
            await client.aclose()


async def fetch_questions(
    api_key: str,
    categories: list[str] | None = None,
    batch_size: int = 10,
    concurrent_batches: int = 5,
) -> list[dict]:
    """Generate questions across shuffled categories with concurrent batches."""
    cats = list(categories or CANONICAL_CATEGORIES)
    random.shuffle(cats)

    difficulties = ["easy", "medium", "hard"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = []
        for i in range(min(concurrent_batches, len(cats))):
            cat = cats[i % len(cats)]
            diff = random.choice(difficulties)
            tasks.append(generate_batch(api_key, cat, diff, batch_size, client))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_questions: list[dict] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error("Batch failed: %s", result)
            continue
        all_questions.extend(result)

    return all_questions
