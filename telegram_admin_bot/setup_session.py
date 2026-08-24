"""Interactive helper: log in once and write SESSION straight into .env.

Copying a ~350-character session string out of a terminal by hand is easy to
get wrong (line wrapping, truncation), so this writes it to the file for you.
Nothing secret is ever printed.

    python setup_session.py            log in and store SESSION in .env
    python setup_session.py --check    validate .env without revealing values
"""

from __future__ import annotations

import sys
from pathlib import Path

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

ENV_PATH = Path(__file__).with_name(".env")
EXAMPLE_PATH = Path(__file__).with_name(".env.example")
REQUIRED = ("API_ID", "API_HASH", "SESSION", "DEEPSEEK_API_KEY")
# Literal values shipped in .env.example, so "filled in" can be told from "untouched".
EXAMPLE_VALUES = {"1234567"}


def read_env() -> dict[str, str]:
    """Parse .env into a dict, ignoring comments and blank lines."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_key(key: str, value: str) -> None:
    """Set one key in .env, replacing any existing line, preserving the rest."""
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    elif EXAMPLE_PATH.exists():
        lines = EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check() -> int:
    """Report which variables look usable, without printing their values."""
    values = read_env()
    if not ENV_PATH.exists():
        print(f"  {ENV_PATH.name} does not exist yet.")
        return 1

    ok = True
    for key in REQUIRED:
        value = values.get(key, "")
        if not value or value.startswith("your_") or value in EXAMPLE_VALUES:
            print(f"  {key:18} MISSING (still blank or the example value)")
            ok = False
        elif key == "SESSION":
            valid = len(value) > 300 and value.startswith("1")
            print(f"  {key:18} {len(value)} chars — {'looks valid' if valid else 'LOOKS WRONG'}")
            ok = ok and valid
        elif key == "API_ID":
            valid = value.isdigit()
            print(f"  {key:18} {'looks valid' if valid else 'NOT A NUMBER'}")
            ok = ok and valid
        else:
            print(f"  {key:18} set ({len(value)} chars)")

    print("\n  All good — run: python main.py" if ok else "\n  Fix the items above first.")
    return 0 if ok else 1


def main() -> int:
    if "--check" in sys.argv:
        return check()

    values = read_env()

    api_id_raw = values.get("API_ID", "")
    if api_id_raw in EXAMPLE_VALUES:
        api_id_raw = ""
    if not api_id_raw.isdigit():
        api_id_raw = input("API_ID (the number from my.telegram.org): ").strip()
    if not api_id_raw.isdigit():
        print("  API_ID must be a number.")
        return 1

    api_hash = values.get("API_HASH", "")
    if not api_hash or api_hash.startswith("your_"):
        api_hash = input("API_HASH (32-character hex string): ").strip()
    if not api_hash:
        print("  API_HASH is required.")
        return 1

    print("\n  Logging in. Enter your phone in international form (e.g. +34600123456),")
    print("  then the code Telegram sends to your app.\n")

    with TelegramClient(StringSession(), int(api_id_raw), api_hash) as client:
        session_string = client.session.save()
        me = client.get_me()

    write_key("API_ID", api_id_raw)
    write_key("API_HASH", api_hash)
    write_key("SESSION", session_string)

    name = " ".join(p for p in [me.first_name, me.last_name] if p) or str(me.id)
    print(f"\n  Signed in as {name}.")
    print(f"  SESSION ({len(session_string)} chars) written to {ENV_PATH.name}.\n")

    if not read_env().get("DEEPSEEK_API_KEY", "").strip() or \
       read_env().get("DEEPSEEK_API_KEY", "").startswith("your_"):
        print("  Still needed: DEEPSEEK_API_KEY in .env (from platform.deepseek.com).")
    else:
        print("  Next: python main.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        raise SystemExit(1)
