"""
Tiny LLM client (plain `requests`, no SDK needed).

Pick a provider in .env:
    LLM_PROVIDER=anthropic   + ANTHROPIC_API_KEY=...   (default model: claude-haiku-4-5-20251001)
    LLM_PROVIDER=gemini      + GEMINI_API_KEY=...      (default model: gemini-2.5-flash, has a free tier)
    LLM_PROVIDER=groq        + GROQ_API_KEY=...        (default model: llama-3.3-70b-versatile, has a free tier)
Override the model with LLM_MODEL if you like.
"""
import json
import os
import re

import requests


class LLMError(Exception):
    """API/network problem (not worth retrying automatically)."""


def provider() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


KEY_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}


def key_name() -> str:
    return KEY_NAMES.get(provider(), "ANTHROPIC_API_KEY")


def configured() -> bool:
    return bool(os.getenv(key_name(), "").strip())


def _post(url: str, **kwargs) -> dict:
    try:
        r = requests.post(url, timeout=90, **kwargs)
    except requests.RequestException:
        raise LLMError("Could not reach the LLM service — check your internet connection.") from None
    if r.status_code >= 400:
        raise LLMError(f"LLM API error {r.status_code}: {r.text[:300]}")
    return r.json()


def _anthropic(system: str, user: str, max_tokens: int) -> str:
    data = _post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
    )
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _gemini(system: str, user: str, max_tokens: int) -> str:
    model = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    data = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "content-type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max(max_tokens, 8192),
                "temperature": 0.7,
            },
        },
    )
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise LLMError("Gemini returned no text: " + str(data)[:200]) from None


def _groq(system: str, user: str, max_tokens: int) -> str:
    data = _post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + os.environ["GROQ_API_KEY"].strip(),
            "content-type": "application/json",
        },
               json={
            "model": os.getenv("LLM_MODEL") or "openai/gpt-oss-120b",
            "reasoning_effort": "low",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max(max_tokens, 8192),
            "temperature": 0.5,
        },
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise LLMError("Groq returned no text: " + str(data)[:200]) from None


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    return json.loads(t[start : end + 1])  # JSONDecodeError is a ValueError


def generate_json(system: str, user: str, max_tokens: int = 3000) -> dict:
    fn = {"gemini": _gemini, "groq": _groq}.get(provider(), _anthropic)
    return parse_json(fn(system, user, max_tokens))