"""Telegram AI support assistant — userbot + local admin panel.

Runs the Telethon client and the FastAPI admin server on one asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from contextlib import suppress
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import InputPeerUser, User

import ai_responder
import config_store
from env_file import is_placeholder, parse_env_file, recover_wrapped
from database import (
    DIR_IN,
    DIR_OUT,
    DIR_SYSTEM,
    OUT_CANCELLED,
    OUT_DRAFTED,
    OUT_FAILED,
    OUT_QUEUED,
    OUT_SENT,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    STATUS_SENT,
    Database,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# Where mutable state lives. Separate from the code so a container can keep the
# database and config on a volume that survives a rebuild.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DB_PATH = DATA_DIR / "assistant.db"

# Localhost only by default — the panel has no login, so it must not be
# reachable from the network. ADMIN_HOST exists for containers, where the
# process binds inside the container and Docker publishes it back to the
# host's loopback only. Anything other than loopback is shouted about at
# startup, because it means the panel is exposed with no authentication.
HOST = (os.getenv("ADMIN_HOST") or "127.0.0.1").strip()
PORT = int(os.getenv("ADMIN_PORT") or 8787)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

REQUIRED_ENV = ("API_ID", "API_HASH", "SESSION", "DEEPSEEK_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("assistant")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class Env:
    api_id: int
    api_hash: str
    session: str
    deepseek_key: str


def _fail(message: str) -> "SystemExit":
    print(f"\n{message}\n", file=sys.stderr)
    return SystemExit(1)


def load_env() -> Env:
    """Read credentials from the environment, or exit with a precise message."""
    env_path = BASE_DIR / ".env"
    load_dotenv(env_path)
    load_dotenv()

    # Read the file directly too, so a value that got wrapped across lines when
    # it was pasted in can be stitched back together instead of arriving cut off.
    file_values = parse_env_file(env_path)

    resolved = {
        name: recover_wrapped(name, (os.getenv(name) or "").strip(), file_values)
        for name in REQUIRED_ENV
    }

    missing = [name for name in REQUIRED_ENV if not resolved[name]]
    if missing:
        raise _fail(
            "  Missing required environment variable(s): "
            + ", ".join(missing)
            + "\n\n  Set them in your shell or in a .env file next to main.py."
            + "\n  See .env.example for the expected names."
        )

    # A value copied straight from .env.example is not a credential.
    untouched = [name for name in REQUIRED_ENV if is_placeholder(resolved[name])]
    if untouched:
        lines = [
            "  These are still the placeholder values from .env.example: "
            + ", ".join(untouched),
            "",
            "  Open .env and replace them with your own credentials.",
        ]
        if "SESSION" in untouched or "API_ID" in untouched or "API_HASH" in untouched:
            lines += [
                "",
                "  For API_ID, API_HASH and SESSION, this fills them in for you:",
                "      python setup_session.py",
            ]
        if "DEEPSEEK_API_KEY" in untouched:
            lines += ["", "  Get a DeepSeek key at https://platform.deepseek.com"]
        raise _fail("\n".join(lines))

    raw_api_id = resolved["API_ID"]
    try:
        api_id = int(raw_api_id)
    except ValueError:
        raise _fail(
            f"  API_ID must be the number from my.telegram.org (got {raw_api_id!r}).\n"
            "  It is 7-8 digits — not the API hash, and not your DeepSeek key."
        )

    # Session strings are base64: any whitespace in there came from copy-paste.
    session = "".join(resolved["SESSION"].split())
    try:
        StringSession(session)
    except ValueError:
        raise _fail(
            f"  The SESSION value in .env is not a usable Telethon session string.\n"
            f"  It is {len(session)} characters; a valid one is around 350 and starts with '1'.\n"
            "  This usually means it was truncated when pasted in.\n\n"
            "  Fix it by regenerating it — this writes the value into .env for you:\n"
            "      python setup_session.py"
        )

    env = Env()
    env.api_id = api_id
    env.api_hash = resolved["API_HASH"]
    env.session = session
    env.deepseek_key = resolved["DEEPSEEK_API_KEY"]
    return env


# ---------------------------------------------------------------------------
# Live state shared by the Telethon handlers and the HTTP handlers
# ---------------------------------------------------------------------------


class Hub:
    """Fan-out of live events to every open admin panel tab."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)


hub = Hub()
db = Database(DB_PATH)
env: Env
config: dict[str, Any] = config_store.load()
http_client: Optional[httpx.AsyncClient] = None
client: TelegramClient
me_info: dict[str, Any] = {}
telegram_state: dict[str, Any] = {"connected": False, "error": None}

# One in-flight drafting task per chat. A newer message supersedes an older
# draft, so the reply always answers the latest state of the conversation.
draft_tasks: dict[int, asyncio.Task] = {}
# Single worker draining the outreach queue, so sends stay paced.
outreach_task: Optional[asyncio.Task] = None
# Texts we are sending right now, so the outgoing-message handler doesn't
# record a duplicate of a message our own send path already logged.
in_flight_sends: dict[int, list[str]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def describe_sender(sender: Any, fallback_id: int) -> tuple[str, Optional[str], bool, Optional[int]]:
    """(display_name, username, is_bot, access_hash) for a private-chat peer."""
    username = getattr(sender, "username", None)
    access_hash = getattr(sender, "access_hash", None)
    is_bot = bool(getattr(sender, "bot", False))

    if isinstance(sender, User) or hasattr(sender, "first_name"):
        parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
        name = " ".join(p for p in parts if p).strip()
    else:
        name = (getattr(sender, "title", None) or "").strip()

    if not name:
        name = username or f"Chat {fallback_id}"
    return name, username, is_bot, access_hash


def within_active_hours(timing: dict[str, Any]) -> bool:
    if not timing.get("active_hours_enabled"):
        return True

    tz_name = timing.get("timezone") or "UTC"
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            log.warning("Unknown timezone %r in config; falling back to system time.", tz_name)
    now = datetime.now(tz).time() if tz else datetime.now().time()

    start = _parse_time(timing.get("active_hours_start"), dtime(0, 0))
    end = _parse_time(timing.get("active_hours_end"), dtime(23, 59))
    if start <= end:
        return start <= now <= end
    # Window wraps past midnight (e.g. 22:00 -> 06:00).
    return now >= start or now <= end


def _parse_time(value: Any, fallback: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


async def resolve_peer(chat_id: int):
    """Get a sendable peer, rebuilding it from the stored access_hash if needed."""
    try:
        return await client.get_input_entity(chat_id)
    except (ValueError, TypeError):
        access_hash = await db.get_access_hash(chat_id)
        if access_hash is None:
            raise RuntimeError(
                f"Cannot resolve chat {chat_id}. Receive a message from them first."
            )
        return InputPeerUser(chat_id, access_hash)


async def push_message(row: dict[str, Any]) -> None:
    conversation = await db.get_conversation(row["chat_id"])
    await hub.broadcast({"type": "message", "message": row, "conversation": conversation})


async def push_error(chat_id: Optional[int], text: str) -> None:
    """Record and surface a failure without taking the process down."""
    log.error("%s", text)
    row = None
    if chat_id is not None:
        row = await db.record_message(
            chat_id, DIR_SYSTEM, STATUS_ERROR, text, bump_preview=False
        )
    await hub.broadcast(
        {"type": "error", "chat_id": chat_id, "text": text, "message": row}
    )


def typing_seconds(text: str) -> float:
    """How long a person would plausibly take to type this."""
    human = config["human"]
    cps = max(1, int(human.get("typing_speed_cps", 12)))
    cap = int(human.get("typing_max_seconds", 25))
    return max(1.0, min(cap, len(text) / cps))


async def deliver(peer: Any, chat_id: int, text: str, typing: bool) -> Any:
    """Send the message, optionally typing first.

    The send happens inside the typing action so the indicator runs right up to
    the moment the message lands, rather than blinking off just before it.
    """
    if not (typing and config["human"].get("typing_indicator", True)):
        return await client.send_message(peer, text)

    seconds = typing_seconds(text)
    log.info("  typing for %.0fs…", seconds)

    result = None
    attempted = False
    try:
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(seconds)
            attempted = True
            result = await client.send_message(peer, text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if result is not None:
            return result  # sent fine; only tearing the indicator down failed
        if attempted:
            raise  # a real send failure — the caller reports it
        # The indicator itself is unavailable. It is cosmetic; send regardless.
        log.warning("Typing indicator unavailable (%s); sending anyway.", type(exc).__name__)
    else:
        return result

    return await client.send_message(peer, text)


async def mark_read(chat_id: int, message_id: Optional[int] = None) -> None:
    """Mark their message read, so they see the second tick."""
    if not config["human"].get("mark_read", True):
        return
    try:
        await client.send_read_acknowledge(chat_id, max_id=message_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Could not mark chat %s read: %s", chat_id, type(exc).__name__)


async def send_as_me(
    chat_id: int,
    text: str,
    draft_id: Optional[int] = None,
    typing: bool = False,
) -> dict[str, Any]:
    """Send a message through the userbot and record it as outgoing/sent."""
    peer = await resolve_peer(chat_id)
    in_flight_sends.setdefault(chat_id, []).append(text)
    try:
        sent = await deliver(peer, chat_id, text, typing)
    finally:
        pending = in_flight_sends.get(chat_id) or []
        if text in pending:
            pending.remove(text)
        if not pending:
            in_flight_sends.pop(chat_id, None)

    telegram_id = getattr(sent, "id", None)
    if draft_id is not None:
        row = await db.update_message(
            draft_id, text=text, status=STATUS_SENT, telegram_id=telegram_id
        )
        await db.set_conversation_preview(chat_id, text)
    else:
        row = await db.record_message(
            chat_id, DIR_OUT, STATUS_SENT, text, telegram_id=telegram_id
        )
        if row is None:  # the outgoing event beat us to it
            row = await db.find_by_telegram_id(chat_id, telegram_id)

    if row is not None:
        await push_message(row)
    return row or {}


# ---------------------------------------------------------------------------
# Drafting pipeline
# ---------------------------------------------------------------------------


def schedule_draft(chat_id: int) -> None:
    cancel_draft(chat_id)
    draft_tasks[chat_id] = asyncio.create_task(draft_worker(chat_id))


def cancel_draft(chat_id: int) -> None:
    task = draft_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()


async def draft_worker(chat_id: int) -> None:
    try:
        timing = config["timing"]
        low = int(timing.get("min_delay_seconds", 20))
        high = int(timing.get("max_delay_seconds", 90))
        delay = random.uniform(min(low, high), max(low, high))

        await hub.broadcast(
            {"type": "drafting", "chat_id": chat_id, "delay_seconds": round(delay, 1)}
        )
        log.info("  drafting a reply for chat %s in %.0fs…", chat_id, delay)
        await asyncio.sleep(delay)

        # Re-read state after the delay — I may have paused the chat meanwhile.
        if config["behavior"].get("global_pause"):
            return
        conversation = await db.get_conversation(chat_id)
        if conversation is None or conversation["automation_paused"]:
            return
        if not within_active_hours(config["timing"]):
            log.info("Outside active hours; skipping draft for chat %s.", chat_id)
            return

        history = await db.get_history_for_ai(chat_id, limit=30)
        if not history:
            log.info("No usable history for chat %s; skipping draft.", chat_id)
            return

        # Read it before writing back, the way a person would: the delay above
        # is the time before opening the chat, this is opening it.
        await mark_read(chat_id)

        text = await ai_responder.generate_reply(
            api_key=env.deepseek_key,
            history=history,
            persona=config["persona"],
            ai_config=config["ai"],
            client=http_client,
            adaptive_style=config["human"].get("adaptive_style", True),
        )

        if config["behavior"].get("auto_send"):
            await send_as_me(chat_id, text, typing=True)
            log.info("Auto-sent AI reply to chat %s.", chat_id)
        else:
            row = await db.record_message(
                chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False
            )
            if row is not None:
                await push_message(row)
            log.info("Draft awaiting approval for chat %s.", chat_id)

    except asyncio.CancelledError:
        raise
    except ai_responder.AIResponderError as exc:
        await push_error(chat_id, str(exc))
    except Exception as exc:  # never let one chat take down the bot
        log.exception("Unexpected failure while drafting for chat %s", chat_id)
        await push_error(chat_id, f"Drafting failed: {type(exc).__name__}: {exc}")
    finally:
        if draft_tasks.get(chat_id) is asyncio.current_task():
            draft_tasks.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Outreach — messages we start, to people already in the account's contacts
# ---------------------------------------------------------------------------


def start_of_day_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def ensure_outreach_worker() -> None:
    global outreach_task
    if outreach_task is None or outreach_task.done():
        outreach_task = asyncio.create_task(outreach_worker())


async def outreach_worker() -> None:
    """Drain the queue one at a time, spaced out and capped per day."""
    try:
        while True:
            item = await db.next_queued_outreach()
            if item is None:
                return

            settings = config["outreach"]
            if config["behavior"].get("global_pause"):
                log.info("Outreach paused (global pause); leaving %s queued.", item["id"])
                return

            sent_today = await db.outreach_sent_since(start_of_day_utc())
            limit = int(settings.get("daily_limit", 20))
            if sent_today >= limit:
                log.info(
                    "Outreach daily limit reached (%s/%s); the rest stays queued for tomorrow.",
                    sent_today, limit,
                )
                await hub.broadcast({
                    "type": "outreach_paused",
                    "reason": f"Daily limit of {limit} reached. Remaining messages stay queued.",
                })
                return

            await process_outreach(item)

            if await db.next_queued_outreach() is not None:
                low = int(settings.get("min_gap_seconds", 90))
                high = int(settings.get("max_gap_seconds", 300))
                gap = random.uniform(min(low, high), max(low, high))
                log.info("Next outreach message in %.0fs.", gap)
                await asyncio.sleep(gap)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Outreach worker stopped unexpectedly")


async def process_outreach(item: dict[str, Any]) -> None:
    """Draft one outreach message and either send it or queue it for approval."""
    outreach_id, chat_id = item["id"], item["chat_id"]
    try:
        text = await ai_responder.generate_opener(
            api_key=env.deepseek_key,
            goal=item["goal"],
            recipient_name=item["display_name"] or "them",
            persona=config["persona"],
            ai_config=config["ai"],
            client=http_client,
        )
    except ai_responder.AIResponderError as exc:
        await db.update_outreach(outreach_id, status=OUT_FAILED, error=str(exc))
        await push_error(chat_id, f"Outreach draft failed: {exc}")
        await broadcast_outreach()
        return

    if not config["outreach"].get("auto_send"):
        row = await db.record_message(
            chat_id, DIR_OUT, STATUS_PENDING, text, bump_preview=False
        )
        await db.update_outreach(
            outreach_id,
            status=OUT_DRAFTED,
            message=text,
            draft_id=row["id"] if row else None,
        )
        if row is not None:
            await push_message(row)
        log.info("Outreach draft for %s awaiting approval.", item["display_name"])
        await broadcast_outreach()
        return

    try:
        await send_as_me(chat_id, text, typing=True)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        await db.update_outreach(outreach_id, status=OUT_FAILED, error=detail)
        await push_error(chat_id, f"Could not send outreach message: {detail}")
    else:
        await db.update_outreach(
            outreach_id, status=OUT_SENT, message=text, mark_sent=True
        )
        log.info("Outreach message sent to %s.", item["display_name"])
    await broadcast_outreach()


async def settle_outreach_draft(
    draft_id: int, status: str, text: Optional[str] = None
) -> None:
    """Close out the queue row behind an approved or rejected outreach draft."""
    item = await db.outreach_for_draft(draft_id)
    if item is None or item["status"] != OUT_DRAFTED:
        return
    await db.update_outreach(
        item["id"], status=status, message=text, mark_sent=(status == OUT_SENT)
    )
    await broadcast_outreach()


async def broadcast_outreach() -> None:
    await hub.broadcast({"type": "outreach", "items": await db.list_outreach()})


async def list_contacts() -> list[dict[str, Any]]:
    """The account's own Telegram contacts — the only people outreach can target."""
    result = await client(GetContactsRequest(hash=0))
    contacts = []
    for user in getattr(result, "users", []):
        if getattr(user, "deleted", False) or getattr(user, "is_self", False):
            continue
        name, username, is_bot, access_hash = describe_sender(user, user.id)
        await db.upsert_conversation(user.id, name, username, is_bot, access_hash)
        contacts.append({
            "chat_id": user.id,
            "display_name": name,
            "username": username,
            "is_bot": is_bot,
        })
    contacts.sort(key=lambda c: c["display_name"].lower())
    return contacts


# ---------------------------------------------------------------------------
# Telethon handlers
# ---------------------------------------------------------------------------


async def on_incoming(event: events.NewMessage.Event) -> None:
    # Private chats only. This is true for bot accounts too — conversations
    # that run through a bot's interface are handled just like any other DM.
    if not event.is_private:
        return

    chat_id = event.chat_id
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None
    name, username, is_bot, access_hash = describe_sender(sender, chat_id)
    await db.upsert_conversation(chat_id, name, username, is_bot, access_hash)

    text = (event.raw_text or "").strip()
    has_text = bool(text)
    stored_text = text if has_text else "[non-text message]"

    if config["behavior"].get("log_all_messages", True):
        row = await db.record_message(
            chat_id,
            DIR_IN,
            STATUS_RECEIVED,
            stored_text,
            telegram_id=event.message.id,
            mark_unread=True,
        )
    else:
        row = {
            "id": None,
            "chat_id": chat_id,
            "telegram_id": event.message.id,
            "direction": DIR_IN,
            "status": STATUS_RECEIVED,
            "text": stored_text,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    if row is not None:
        await push_message(row)

    log.info("DM from %s%s (chat %s): %s", name, " [bot]" if is_bot else "", chat_id,
             f"{len(text)} chars" if has_text else "non-text message")

    # Each early return is logged: the terminal should always explain why a
    # message did not get a reply.
    if not has_text:
        log.info("  no text to reply to — skipping.")
        return
    if config["behavior"].get("global_pause"):
        log.info("  automation is globally paused — skipping.")
        return

    conversation = await db.get_conversation(chat_id)
    if conversation and conversation["automation_paused"]:
        log.info("  this conversation is paused — skipping.")
        return
    if not within_active_hours(config["timing"]):
        timing = config["timing"]
        log.info("  outside active hours (%s-%s %s) — skipping.",
                 timing.get("active_hours_start"), timing.get("active_hours_end"),
                 timing.get("timezone"))
        return

    schedule_draft(chat_id)


async def on_outgoing(event: events.NewMessage.Event) -> None:
    """Mirror messages I send from my phone/desktop so the thread stays whole."""
    if not event.is_private:
        return

    text = (event.raw_text or "").strip()
    chat_id = event.chat_id
    if text and text in (in_flight_sends.get(chat_id) or []):
        return  # already recorded by send_as_me
    if not config["behavior"].get("log_all_messages", True):
        return

    try:
        chat = await event.get_chat()
    except Exception:
        chat = None
    name, username, is_bot, access_hash = describe_sender(chat, chat_id)
    await db.upsert_conversation(chat_id, name, username, is_bot, access_hash)

    row = await db.record_message(
        chat_id,
        DIR_OUT,
        STATUS_SENT,
        text or "[non-text message]",
        telegram_id=event.message.id,
    )
    if row is not None:
        await push_message(row)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

app = FastAPI(title="Telegram AI Assistant — Admin")


class SendBody(BaseModel):
    text: str = Field(min_length=1)


class PauseBody(BaseModel):
    paused: bool


class GlobalPauseBody(BaseModel):
    global_pause: bool


class ApproveBody(BaseModel):
    text: Optional[str] = None


class OutreachBody(BaseModel):
    chat_ids: list[int] = Field(default_factory=list)
    goal: str = ""


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "telegram_connected": telegram_state["connected"],
        "telegram_error": telegram_state["error"],
        "me": me_info,
        "global_pause": config["behavior"].get("global_pause", False),
        "auto_send": config["behavior"].get("auto_send", False),
        "persona_configured": any(
            (config["persona"].get(k) or "").strip() for k in config_store.DEFAULTS["persona"]
        ),
    }


@app.get("/api/config")
async def api_get_config() -> dict[str, Any]:
    return config


@app.put("/api/config")
async def api_put_config(payload: dict[str, Any]) -> dict[str, Any]:
    global config
    try:
        config = config_store.save(payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not save config: {exc}") from exc
    await hub.broadcast({"type": "config", "config": config})
    log.info("Config updated from the admin panel.")
    return config


@app.get("/api/conversations")
async def api_conversations() -> list[dict[str, Any]]:
    return await db.list_conversations()


@app.get("/api/conversations/{chat_id}/messages")
async def api_messages(chat_id: int) -> dict[str, Any]:
    conversation = await db.get_conversation(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    return {"conversation": conversation, "messages": await db.get_messages(chat_id)}


@app.post("/api/conversations/{chat_id}/read")
async def api_mark_read(chat_id: int) -> dict[str, Any]:
    conversation = await db.mark_read(chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    await hub.broadcast({"type": "conversation", "conversation": conversation})
    return conversation


@app.post("/api/conversations/{chat_id}/pause")
async def api_pause(chat_id: int, body: PauseBody) -> dict[str, Any]:
    conversation = await db.set_paused(chat_id, body.paused)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")
    if body.paused:
        cancel_draft(chat_id)
    await hub.broadcast({"type": "conversation", "conversation": conversation})
    return conversation


@app.post("/api/global-pause")
async def api_global_pause(body: GlobalPauseBody) -> dict[str, Any]:
    global config
    updated = {**config, "behavior": {**config["behavior"], "global_pause": body.global_pause}}
    config = config_store.save(updated)
    if body.global_pause:
        for chat_id in list(draft_tasks):
            cancel_draft(chat_id)
    await hub.broadcast({"type": "config", "config": config})
    return {"global_pause": config["behavior"]["global_pause"]}


@app.post("/api/conversations/{chat_id}/send")
async def api_send(chat_id: int, body: SendBody) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if await db.get_conversation(chat_id) is None:
        raise HTTPException(status_code=404, detail="Unknown conversation")

    # I answered by hand, so any AI draft in flight for this chat is stale.
    cancel_draft(chat_id)
    try:
        return await send_as_me(chat_id, text)
    except Exception as exc:
        detail = f"Could not send message: {type(exc).__name__}: {exc}"
        await push_error(chat_id, detail)
        raise HTTPException(status_code=502, detail=detail) from exc


@app.post("/api/drafts/{draft_id}/approve")
async def api_approve(draft_id: int, body: ApproveBody) -> dict[str, Any]:
    draft = await db.get_message(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Unknown draft")
    if draft["status"] != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"Draft is already {draft['status']}")

    text = (body.text if body.text is not None else draft["text"]).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")

    try:
        sent = await send_as_me(draft["chat_id"], text, draft_id=draft_id)
    except Exception as exc:
        detail = f"Could not send draft: {type(exc).__name__}: {exc}"
        await push_error(draft["chat_id"], detail)
        raise HTTPException(status_code=502, detail=detail) from exc

    await settle_outreach_draft(draft_id, OUT_SENT, text=text)
    return sent


@app.post("/api/drafts/{draft_id}/reject")
async def api_reject(draft_id: int) -> dict[str, Any]:
    draft = await db.get_message(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Unknown draft")
    row = await db.update_message(draft_id, status=STATUS_REJECTED)
    if row is not None:
        await push_message(row)
    await settle_outreach_draft(draft_id, OUT_CANCELLED)
    return row or {}


@app.get("/api/contacts")
async def api_contacts() -> list[dict[str, Any]]:
    if not telegram_state["connected"]:
        raise HTTPException(status_code=503, detail="Telegram is not connected yet")
    try:
        return await list_contacts()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not read contacts: {type(exc).__name__}: {exc}"
        ) from exc


@app.get("/api/outreach")
async def api_outreach_list() -> list[dict[str, Any]]:
    return await db.list_outreach()


@app.post("/api/outreach")
async def api_outreach_queue(body: OutreachBody) -> dict[str, Any]:
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Say what the message should achieve")
    if not body.chat_ids:
        raise HTTPException(status_code=400, detail="Pick at least one contact")

    # Only people already in the account's contacts may be targeted.
    try:
        allowed = {c["chat_id"]: c["display_name"] for c in await list_contacts()}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not verify contacts: {type(exc).__name__}"
        ) from exc

    unknown = [cid for cid in body.chat_ids if cid not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(unknown)} of those are not in your Telegram contacts. "
                "Outreach only goes to people you already have as contacts."
            ),
        )

    recipients = [(cid, allowed[cid]) for cid in body.chat_ids]
    queued = await db.queue_outreach(recipients, goal)
    ensure_outreach_worker()
    await broadcast_outreach()
    return {"queued": len(queued), "skipped": len(recipients) - len(queued)}


@app.post("/api/outreach/cancel")
async def api_outreach_cancel() -> dict[str, Any]:
    cancelled = await db.cancel_queued_outreach()
    global outreach_task
    task, outreach_task = outreach_task, None
    if task is not None and not task.done():
        task.cancel()
        # Wait for it to actually stop. Otherwise queueing again straight after
        # sees a task that is cancelling-but-not-done and starts no replacement,
        # leaving the new items sitting in the queue forever.
        with suppress(asyncio.CancelledError):
            await task
    await broadcast_outreach()
    return {"cancelled": cancelled}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        await ws.send_json(
            {
                "type": "hello",
                "conversations": await db.list_conversations(),
                "config": config,
                "status": await api_status(),
            }
        )
        while True:
            await ws.receive_text()  # client sends keepalives; nothing to parse
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.disconnect(ws)


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error in admin API")
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


async def run_telegram() -> None:
    """Keep the userbot connected; reconnect on failure without killing the API."""
    global me_info
    backoff = 5
    while True:
        try:
            await client.connect()
            if not await client.is_user_authorized():
                telegram_state["connected"] = False
                telegram_state["error"] = (
                    "The SESSION string is not authorized. Generate a new Telethon "
                    "StringSession for this account and set SESSION to it."
                )
                log.error("%s", telegram_state["error"])
                await hub.broadcast({"type": "status", "status": await api_status()})
                return

            me = await client.get_me()
            me_info = {
                "id": getattr(me, "id", None),
                "name": describe_sender(me, getattr(me, "id", 0))[0],
                "username": getattr(me, "username", None),
            }
            telegram_state["connected"] = True
            telegram_state["error"] = None
            backoff = 5
            log.info("Telegram connected as %s. Listening for private messages "
                     "(send a DM from another account to test).", me_info["name"])
            await hub.broadcast({"type": "status", "status": await api_status()})

            await client.run_until_disconnected()
            telegram_state["connected"] = False
            log.warning("Telegram disconnected; reconnecting…")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            telegram_state["connected"] = False
            telegram_state["error"] = f"{type(exc).__name__}: {exc}"
            log.error("Telegram client error: %s", telegram_state["error"])
            await hub.broadcast({"type": "status", "status": await api_status()})

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def run_web() -> None:
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    )
    if HOST not in LOOPBACK:
        log.warning(
            "Admin panel bound to %s, not loopback. It has NO LOGIN — anyone who "
            "can reach this port controls the account. Only do this inside a "
            "container whose port is published to 127.0.0.1, or behind a firewall.",
            HOST,
        )
    log.info("Admin panel: http://%s:%s", HOST, PORT)
    await server.serve()


async def main() -> None:
    global env, client, http_client

    env = load_env()
    await db.connect()
    http_client = httpx.AsyncClient(timeout=ai_responder.REQUEST_TIMEOUT_SECONDS)

    client = TelegramClient(StringSession(env.session), env.api_id, env.api_hash)
    client.add_event_handler(on_incoming, events.NewMessage(incoming=True))
    client.add_event_handler(on_outgoing, events.NewMessage(outgoing=True))

    try:
        await asyncio.gather(run_web(), run_telegram())
    finally:
        if client.is_connected():
            await client.disconnect()
        if http_client is not None:
            await http_client.aclose()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
