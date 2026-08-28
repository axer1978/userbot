"""Interactive helper: log in once and write SESSION straight into .env.

Copying a ~350-character session string out of a terminal by hand is easy to
get wrong (line wrapping, truncation), so this writes it to the file for you.
Nothing secret is ever printed.

    python setup_session.py            log in and store SESSION in .env
    python setup_session.py --check    validate .env without revealing values
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

from telethon import errors
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from env_file import parse_env_file

ENV_PATH = Path(__file__).with_name(".env")
EXAMPLE_PATH = Path(__file__).with_name(".env.example")
REQUIRED = ("API_ID", "API_HASH", "SESSION", "DEEPSEEK_API_KEY")
# Literal values shipped in .env.example, so "filled in" can be told from "untouched".
EXAMPLE_VALUES = {"1234567"}


def read_env() -> dict[str, str]:
    """Parse .env, rejoining any value that got wrapped across lines."""
    return parse_env_file(ENV_PATH)


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


# Telegram reports how it delivered the code. Saying so out loud saves people
# watching for an SMS that was never going to arrive.
_DELIVERY = {
    "SentCodeTypeApp":
        "the Telegram app itself.\n"
        "     Open Telegram on your phone or another logged-in device and look for\n"
        "     the chat named 'Telegram' (blue checkmark). The code is there.\n"
        "     It was NOT sent by SMS.",
    "SentCodeTypeSms": "SMS to your phone.",
    "SentCodeTypeCall": "a phone call that reads the code aloud.",
    "SentCodeTypeFlashCall": "a flash call — the code is part of the calling number.",
    "SentCodeTypeMissedCall": "a missed call — the code is the last digits of the calling number.",
    "SentCodeTypeEmailCode": "email.",
}


def describe_delivery(sent_code) -> str:
    name = type(sent_code.type).__name__
    return _DELIVERY.get(name, name)


def do_login(api_id: int, api_hash: str) -> tuple[str, object]:
    """Interactive login, returning (session_string, me). Raises SystemExit on failure."""
    client = TelegramClient(StringSession(), api_id, api_hash)
    client.connect()

    phone = input("  Phone number (international, e.g. +34600123456): ").strip()
    if not phone.startswith("+"):
        print("  Note: no '+' — assuming you meant +" + phone)
        phone = "+" + phone

    try:
        sent = client.send_code_request(phone)
    except errors.FloodWaitError as exc:
        minutes = exc.seconds / 60
        raise SystemExit(
            f"\n  Telegram is rate-limiting login attempts on this number.\n"
            f"  Wait {exc.seconds} seconds (~{minutes:.0f} min) and try again.\n"
            "  Requesting codes repeatedly makes this longer, so don't retry in the meantime.\n"
        )
    except errors.PhoneNumberInvalidError:
        raise SystemExit(
            f"\n  Telegram does not recognise {phone} as a valid number.\n"
            "  Use international format: + then country code then the number,\n"
            "  with no spaces or dashes.\n"
        )
    except errors.PhoneNumberBannedError:
        raise SystemExit(f"\n  {phone} is banned from Telegram.\n")
    except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError):
        raise SystemExit(
            "\n  API_ID and API_HASH are not a matching pair.\n"
            "  Recheck both at https://my.telegram.org -> API development tools.\n"
        )

    print(f"\n  Telegram sent the code via {describe_delivery(sent)}\n")

    for attempt in range(3):
        code = input("  Login code (or type 'sms' to have it resent by SMS): ").strip()

        if code.lower() == "sms":
            try:
                sent = client.send_code_request(phone, force_sms=True)
                print(f"\n  Resent via {describe_delivery(sent)}\n")
            except errors.FloodWaitError as exc:
                raise SystemExit(f"\n  Rate-limited; wait {exc.seconds} seconds.\n")
            except Exception as exc:
                print(f"  Could not resend by SMS: {type(exc).__name__}")
            continue

        try:
            client.sign_in(phone, code)
            break
        except errors.SessionPasswordNeededError:
            password = getpass("  Two-step verification password: ")
            client.sign_in(password=password)
            break
        except errors.PhoneCodeInvalidError:
            print(f"  That code is not right. {2 - attempt} attempt(s) left.")
        except errors.PhoneCodeExpiredError:
            raise SystemExit(
                "\n  That code has expired. Run setup_session.py again for a fresh one.\n"
            )
    else:
        raise SystemExit("\n  Too many incorrect codes. Run setup_session.py again.\n")

    session_string = client.session.save()
    me = client.get_me()
    client.disconnect()
    return session_string, me


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
            try:
                StringSession("".join(value.split()))
                valid = True
            except ValueError:
                valid = False
            print(f"  {key:18} {len(value)} chars — {'parses correctly' if valid else 'NOT A VALID SESSION'}")
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

    print()
    session_string, me = do_login(int(api_id_raw), api_hash)

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
