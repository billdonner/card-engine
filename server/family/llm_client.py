"""LLM chat client for conversational family tree building.

Model-agnostic — supports OpenAI and Anthropic via async httpx.
The LLM returns JSON with a `reply` (human-readable text) and
`patches` (structured operations like add_person, update_person, etc.)
that the server applies to the DB.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger("card_engine.family.llm")

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)

_SYSTEM_PROMPT = """\
You are a family tree assistant. The user will describe their family members \
conversationally. Your job is to extract structured data and return JSON.

Always respond with a JSON object (no markdown fences) containing:
{
  "reply": "A friendly human-readable response acknowledging what you understood.",
  "patches": [
    {
      "op": "add_person",
      "name": "Harold",
      "nickname": null,
      "maiden_name": null,
      "born": null,
      "status": "living",
      "player": false,
      "notes": null
    },
    {
      "op": "add_relationship",
      "type": "parent_of",
      "from_name": "Harold",
      "to_name": "Billy"
    },
    {
      "op": "update_person",
      "name": "Harold",
      "fields": {"born": 1945, "nickname": "Grandpa Harold"}
    }
  ]
}

Valid patch operations:
- add_person: Add a new family member. Fields: name (required), nickname, maiden_name, born, status, player, notes.
- update_person: Update an existing person. Fields: name (to find them), fields (dict of updates).
- add_relationship: Add a relationship. Fields: type (married/parent_of/divorced), from_name, to_name, year, notes.

Relationship type semantics:
- parent_of: from_name is the parent, to_name is the child
- married: from_name and to_name are spouses
- divorced: from_name and to_name were formerly married

If the user mentions someone is a grandfather/grandmother, that implies parent_of relationships \
through the appropriate intermediate generation. Create the intermediate person as a placeholder \
if they haven't been mentioned yet.

If unsure about a detail, ask the user in the reply. Never guess birth years or names.
Return ONLY the JSON object, no other text.
"""


def _get_config() -> tuple[str, str, str]:
    """Return (provider, api_key, model)."""
    model = os.environ.get("CE_FAMILY_CHAT_MODEL", "gpt-4o-mini")
    openai_key = os.environ.get("CE_OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("CE_ANTHROPIC_API_KEY", "")

    if "claude" in model.lower() and anthropic_key:
        return "anthropic", anthropic_key, model
    if openai_key:
        return "openai", openai_key, model
    if anthropic_key:
        return "anthropic", anthropic_key, model or "claude-sonnet-4-20250514"
    return "none", "", model


def _build_context(people: list[dict], relationships: list[dict]) -> str:
    """Build a context summary of the current family tree for the LLM."""
    if not people:
        return "The family tree is currently empty."

    lines = ["Current family tree:"]
    for p in people:
        parts = [p["name"]]
        if p.get("nickname"):
            parts.append(f'(nickname: {p["nickname"]})')
        if p.get("born"):
            parts.append(f'born {p["born"]}')
        if p.get("status") == "deceased":
            parts.append("(deceased)")
        if p.get("player"):
            parts.append("[PLAYER]")
        if p.get("placeholder"):
            parts.append("[placeholder - needs more info]")
        lines.append(f"  - {' '.join(parts)}")

    if relationships:
        lines.append("\nRelationships:")
        name_map = {str(p["id"]): p["name"] for p in people}
        for r in relationships:
            from_name = name_map.get(str(r["from_id"]), "?")
            to_name = name_map.get(str(r["to_id"]), "?")
            if r["type"] == "parent_of":
                lines.append(f"  - {from_name} is parent of {to_name}")
            elif r["type"] == "married":
                lines.append(f"  - {from_name} married {to_name}")
            elif r["type"] == "divorced":
                lines.append(f"  - {from_name} divorced {to_name}")

    return "\n".join(lines)


def _parse_response(content: str) -> dict:
    """Parse LLM response into {reply, patches}."""
    cleaned = _FENCE_RE.sub("", content).strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    # Find the JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or start >= end:
        return {"reply": content, "patches": []}

    try:
        data = json.loads(cleaned[start:end + 1])
        return {
            "reply": data.get("reply", ""),
            "patches": data.get("patches", []),
        }
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON response")
        return {"reply": content, "patches": []}


async def chat(
    user_message: str,
    people: list[dict],
    relationships: list[dict],
    history: list[dict] | None = None,
) -> dict:
    """Send a chat message to the LLM and return parsed {reply, patches}.

    Args:
        user_message: The user's message.
        people: Current list of people dicts (from DB).
        relationships: Current list of relationship dicts (from DB).
        history: Previous chat messages [{role, content}, ...].

    Returns:
        dict with 'reply' (str) and 'patches' (list[dict]).
    """
    provider, api_key, model = _get_config()

    if provider == "none" or not api_key:
        return {
            "reply": "No LLM API key configured. Set CE_OPENAI_API_KEY or CE_ANTHROPIC_API_KEY.",
            "patches": [],
        }

    context = _build_context(people, relationships)

    messages = []
    if history:
        for msg in history[-20:]:  # Keep last 20 messages for context
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": f"{context}\n\nUser says: {user_message}"})

    async with httpx.AsyncClient(timeout=60.0) as client:
        if provider == "openai":
            return await _call_openai(client, api_key, model, messages)
        else:
            return await _call_anthropic(client, api_key, model, messages)


async def _call_openai(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            *messages,
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(_OPENAI_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_response(content)
    except httpx.HTTPStatusError as exc:
        logger.error("OpenAI HTTP %d: %s", exc.response.status_code, exc.response.text[:200])
        return {"reply": f"LLM error: HTTP {exc.response.status_code}", "patches": []}
    except Exception as exc:
        logger.error("OpenAI request failed: %s", exc)
        return {"reply": f"LLM error: {exc}", "patches": []}


async def _call_anthropic(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
) -> dict:
    payload = {
        "model": model,
        "max_tokens": 2000,
        "system": _SYSTEM_PROMPT,
        "messages": messages,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(_ANTHROPIC_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["content"][0]["text"]
        return _parse_response(content)
    except httpx.HTTPStatusError as exc:
        logger.error("Anthropic HTTP %d: %s", exc.response.status_code, exc.response.text[:200])
        return {"reply": f"LLM error: HTTP {exc.response.status_code}", "patches": []}
    except Exception as exc:
        logger.error("Anthropic request failed: %s", exc)
        return {"reply": f"LLM error: {exc}", "patches": []}
