# Telegram AI Support Assistant

A Telethon **userbot** that runs on your own Telegram account, drafts replies to
incoming DMs with the DeepSeek API, and gives you a local web admin panel to
supervise everything.

Nothing is sent without your approval unless you explicitly turn on auto-send.

## What it does

- Listens for **private messages only** — group and channel traffic is ignored.
  Messages from **bot accounts are included**, so conversations that run through
  a bot's interface are handled like any other DM.
- Logs every message to SQLite and pushes it to the admin panel live over a
  WebSocket.
- For each incoming DM it checks, in order: the per-chat pause flag, the global
  pause, and the configured active hours. If any of them says stop, no draft is
  made.
- Otherwise it builds the last ~30 messages of that chat as OpenAI-style
  `{"role": "user" | "assistant"}` turns, prepends a system message built from
  your persona config, waits a randomised human-looking delay, and calls
  DeepSeek.
- **Auto-send off (the default):** the draft is saved as `pending_approval` and
  appears in the panel with *Approve & Send*, *Edit then Send* and *Reject*.
  Nothing reaches Telegram until you approve it.
- **Auto-send on:** the reply goes out directly and is logged as sent.

If a newer message arrives in a chat while a draft is still being prepared, the
in-flight draft is cancelled and restarted, so the reply always answers the
latest state of the conversation.

## Requirements

- Python 3.11+
- A Telegram `API_ID` / `API_HASH` from https://my.telegram.org
- A Telethon `StringSession` for the account the assistant runs on
- A DeepSeek API key from https://platform.deepseek.com

## Install

```bash
cd telegram_admin_bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure credentials

Credentials come from the environment and are never written to `config.json`,
never logged, and never included in errors shown in the panel.

```bash
cp .env.example .env
```

Then edit `.env`:

```
API_ID=1234567
API_HASH=your_api_hash
SESSION=your_telethon_string_session
DEEPSEEK_API_KEY=your_deepseek_key
```

If you don't have a `StringSession` yet, generate one with the bundled helper.
It logs you in and writes the value into `.env` for you — the string is ~350
characters, and copying it out of a terminal by hand tends to wrap or truncate
it:

```bash
python setup_session.py
```

It asks for your `API_ID`, `API_HASH`, phone number (international format) and
the login code Telegram sends to your app. Nothing secret is printed.

To confirm `.env` is complete without revealing any values:

```bash
python setup_session.py --check
```

Treat the session string like a password — it grants full access to your
account. If it is ever exposed, revoke it in Telegram under
Settings → Devices, then run `setup_session.py` again.

## Run

```bash
python main.py
```

Then open **http://127.0.0.1:8787**.

The server binds to `127.0.0.1` only, so it is not reachable from your network.
There is no login, which is exactly why it must stay on localhost.

Startup fails with a clear message naming any missing environment variable
rather than crashing silently.

## Fill in your persona first

**`config.json` ships with every persona field blank, and a blank persona
produces generic replies.** Open **Settings** in the panel and fill in:

| Field | What to put there |
|---|---|
| `purpose` | What this account is for and what you want the assistant to do |
| `tone` | How it should sound |
| `languages` | e.g. "always reply in the language the person wrote in" |
| `boundaries` | Hard rules — what it must never say, promise, or do |
| `signature_style` | Whether and how to sign off |

Until at least one field is filled in, the app falls back to a minimal neutral
system prompt so it still works, and the panel shows *"persona not configured"*.

Other settings:

| Setting | Default | Meaning |
|---|---|---|
| `min_delay_seconds` / `max_delay_seconds` | 20 / 90 | Random wait before drafting, so replies don't look instant |
| `active_hours_enabled` | `false` | When on, drafting only happens inside the window below |
| `active_hours_start` / `end` / `timezone` | 09:00 / 21:00 / UTC | Window (may cross midnight), in the given IANA timezone |
| `auto_send` | `false` | Send AI replies without approval |
| `log_all_messages` | `true` | Persist messages to SQLite. Turn off and messages still appear live, but no history is kept — which also means the AI gets less context |
| `model` / `max_tokens` / `temperature` | `deepseek-chat` / 400 / 1.0 | DeepSeek request parameters |

Settings save straight back to `config.json` and take effect immediately — no
restart.

## Using the panel

- **Left sidebar** — conversations with last message, timestamp, unread count, a
  `BOT` badge for bot accounts, and a per-chat Pause/Resume button.
- **Main panel** — the full thread. Incoming, outgoing, pending drafts, rejected
  drafts and errors are colour-coded and labelled.
- **Pause automation** (per chat) — stops AI drafting for that conversation when
  you take it over by hand. Any draft already in flight is cancelled.
- **Pause all** — global kill switch for every chat.
- **Message box** — send as yourself at any time, whatever the automation state.
  Doing so also cancels a pending draft for that chat.

Sending a message from your phone or Telegram Desktop shows up in the panel too,
so the thread stays complete.

## Errors

DeepSeek failures (network, timeout, HTTP 429, malformed response) are retried
with exponential backoff that honours `Retry-After`, then surfaced as a red
error in the conversation and a toast in the panel. The bot keeps running.
Authentication failures (401/403) fail fast without burning retries.

Telegram disconnects are reconnected automatically with backoff; the admin
server stays up throughout, and the header shows the live connection state.

## Files

```
main.py           entrypoint — Telethon client + FastAPI/uvicorn via asyncio.gather
database.py       SQLite helpers (aiosqlite; conversations.is_bot, messages.direction/status)
ai_responder.py   builds the system prompt + message list, calls DeepSeek, returns the draft
config_store.py   loads/validates/atomically saves config.json
env_file.py       tolerant .env reader; rejoins values that got wrapped when pasted
setup_session.py  one-time login helper; writes SESSION into .env (--check validates)
config.json       your settings (persona fields blank until you fill them in)
static/index.html the admin panel — plain HTML/CSS/JS, no build step
assistant.db      created on first run
```

## A note on userbots

Automating a personal account is against Telegram's Terms of Service and can get
the account limited or banned. Keep the delays human, and prefer approval mode
over auto-send.
