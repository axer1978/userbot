"""Builds the prompt from the persona config and calls the DeepSeek API."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/chat/completions"

# Used before the persona has been filled in from the Settings tab, so the
# app still produces something sane instead of erroring out.
FALLBACK_SYSTEM_PROMPT = (
    "You are drafting a reply from a real person's Telegram account. "
    "Keep it short and natural."
)

PERSONA_HEADER = (
    "You are drafting a reply that will be sent from a real person's personal "
    "Telegram account. Write in the first person as that person. Never state or "
    "imply that you are an AI, a bot, or an assistant. Output only the message "
    "text to send — no quotation marks, no labels, no commentary, no analysis."
)

_PERSONA_SECTIONS = (
    ("purpose", "WHAT THIS ACCOUNT IS FOR"),
    ("tone", "TONE AND STYLE"),
    ("languages", "LANGUAGE"),
    ("boundaries", "HARD RULES — NEVER BREAK THESE"),
    ("signature_style", "SIGN-OFF"),
)

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 60.0


class AIResponderError(Exception):
    """Raised for any failure that should surface in the admin panel."""


def build_system_prompt(persona: dict[str, Any]) -> str:
    """Assemble the system message; falls back to a neutral one when blank."""
    sections = []
    for key, label in _PERSONA_SECTIONS:
        value = (persona.get(key) or "").strip()
        if value:
            sections.append(f"{label}:\n{value}")
    if not sections:
        return FALLBACK_SYSTEM_PROMPT
    return PERSONA_HEADER + "\n\n" + "\n\n".join(sections)


# Broad emoji blocks — enough to tell "uses emoji" from "doesn't".
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤️]"
)


def describe_style(history: list[dict[str, str]]) -> str:
    """A short brief on how the other person writes, so the reply can match it.

    Built only from their own messages — mirroring our own past replies would
    just entrench whatever the model did first.
    """
    theirs = [m["content"] for m in history if m["role"] == "user" and m["content"].strip()]
    if len(theirs) < 2:
        return ""

    lengths = [len(t) for t in theirs]
    avg_len = sum(lengths) / len(lengths)
    avg_words = sum(len(t.split()) for t in theirs) / len(theirs)
    with_emoji = sum(1 for t in theirs if _EMOJI.search(t))
    # Do they start sentences with a capital, and end with punctuation?
    capitalised = sum(1 for t in theirs if t[:1].isupper())
    punctuated = sum(1 for t in theirs if t.rstrip()[-1:] in ".!?")

    notes = []
    if avg_len < 25:
        notes.append(f"very short messages (about {avg_len:.0f} characters, "
                     f"{avg_words:.0f} words) — often just a few words")
    elif avg_len < 80:
        notes.append(f"short messages (about {avg_len:.0f} characters)")
    elif avg_len < 200:
        notes.append(f"medium-length messages (about {avg_len:.0f} characters)")
    else:
        notes.append(f"long, detailed messages (about {avg_len:.0f} characters)")

    ratio = with_emoji / len(theirs)
    if ratio > 0.5:
        notes.append("emoji in most messages")
    elif ratio > 0.15:
        notes.append("the occasional emoji")
    else:
        notes.append("no emoji")

    if capitalised / len(theirs) < 0.4:
        notes.append("mostly lower-case, not much capitalisation")
    if punctuated / len(theirs) < 0.3:
        notes.append("often no full stop at the end")

    return (
        "HOW THIS PERSON WRITES: " + "; ".join(notes) + ".\n"
        "Match them. Write about the same length — if they send one line, send one "
        "line, never a paragraph. Mirror their level of formality, their emoji use "
        "and their punctuation habits, and always reply in the language they are "
        "writing in."
    )


async def generate_opener(
    *,
    api_key: str,
    goal: str,
    recipient_name: str,
    persona: dict[str, Any],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Draft the first message of a conversation, given what it should achieve."""
    if not (goal or "").strip():
        raise AIResponderError("No goal given for the outreach message.")

    system = build_system_prompt(persona)
    system += (
        f"\n\nYou are writing the FIRST message to {recipient_name}, someone in this "
        "person's own contacts. Keep it short, personal and natural — the way you would "
        "message someone you know, not a marketing blast. Do not invent facts about "
        "them, and do not pretend a previous conversation happened."
    )

    history = [{"role": "user", "content": f"Write that message. Its purpose: {goal}"}]
    return await _complete(
        api_key=api_key,
        messages=[{"role": "system", "content": system}] + history,
        ai_config=ai_config,
        client=client,
    )


def _redact(text: str, secret: Optional[str]) -> str:
    """Belt-and-braces: never let the key reach a log line or the admin panel."""
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


def _clip(text: str, limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, min(60.0, float(header)))
        except ValueError:
            pass
    return BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))


async def generate_reply(
    *,
    api_key: str,
    history: list[dict[str, str]],
    persona: dict[str, Any],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    adaptive_style: bool = True,
) -> str:
    """Return the draft reply text, or raise AIResponderError with a safe message."""
    if not api_key:
        raise AIResponderError("DEEPSEEK_API_KEY is not set.")

    system = build_system_prompt(persona)
    if adaptive_style:
        style = describe_style(history)
        if style:
            system += "\n\n" + style

    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    if len(messages) == 1:
        raise AIResponderError("No conversation history to reply to.")

    return await _complete(
        api_key=api_key, messages=messages, ai_config=ai_config, client=client
    )


async def _complete(
    *,
    api_key: str,
    messages: list[dict[str, str]],
    ai_config: dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """One DeepSeek chat completion, with retry/backoff and safe error text."""
    if not api_key:
        raise AIResponderError("DEEPSEEK_API_KEY is not set.")

    payload = {
        "model": ai_config.get("model") or "deepseek-chat",
        "messages": messages,
        "max_tokens": ai_config.get("max_tokens", 400),
        "temperature": ai_config.get("temperature", 1.0),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        last_error = "DeepSeek request failed."
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            try:
                response = await http.post(API_URL, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = "DeepSeek API timed out."
            except httpx.HTTPError as exc:
                last_error = _redact(
                    f"Could not reach the DeepSeek API: {type(exc).__name__}.", api_key
                )
            else:
                if response.status_code == 200:
                    return _parse_reply(response, api_key)

                detail = _clip(_redact(response.text, api_key))
                if response.status_code == 429:
                    last_error = f"DeepSeek rate limit (429). {detail}"
                    delay = _retry_after(response, attempt)
                elif response.status_code in (401, 403):
                    # Not retryable — a bad key will not fix itself.
                    raise AIResponderError(
                        f"DeepSeek rejected the API key (HTTP {response.status_code}). "
                        "Check that DEEPSEEK_API_KEY is correct."
                    )
                elif response.status_code >= 500:
                    last_error = f"DeepSeek server error (HTTP {response.status_code}). {detail}"
                    delay = _retry_after(response, attempt)
                else:
                    raise AIResponderError(
                        f"DeepSeek API error (HTTP {response.status_code}). {detail}"
                    )

            if attempt < MAX_ATTEMPTS:
                log.warning(
                    "DeepSeek attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise AIResponderError(f"{last_error} Gave up after {MAX_ATTEMPTS} attempts.")
    finally:
        if owns_client:
            await http.aclose()


def _parse_reply(response: httpx.Response, api_key: str) -> str:
    try:
        data = response.json()
    except ValueError:
        raise AIResponderError("DeepSeek returned a response that was not JSON.") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIResponderError(
            "DeepSeek response was missing choices[0].message.content."
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise AIResponderError("DeepSeek returned an empty reply.")
    return content.strip()
