"""Load/save config.json, with defaults and validation.

Persona fields intentionally ship blank — they are filled in from the admin
panel's Settings tab, not pre-seeded with example content.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("config.json")

DEFAULTS: dict[str, Any] = {
    "persona": {
        "purpose": "",
        "tone": "",
        "languages": "",
        "boundaries": "",
        "signature_style": "",
    },
    "timing": {
        "min_delay_seconds": 20,
        "max_delay_seconds": 90,
        "active_hours_enabled": False,
        "active_hours_start": "09:00",
        "active_hours_end": "21:00",
        "timezone": "UTC",
    },
    "behavior": {
        "auto_send": False,
        "log_all_messages": True,
        "global_pause": False,
    },
    "ai": {
        "model": "deepseek-chat",
        "max_tokens": 400,
        "temperature": 1.0,
    },
    # Messages the assistant starts, to people already in your contacts.
    "outreach": {
        # Telegram rate-limits and penalises bursts of new conversations, so
        # sends are spaced out and capped per day.
        "min_gap_seconds": 90,
        "max_gap_seconds": 300,
        "daily_limit": 20,
        "auto_send": False,
    },
}


def _merge(defaults: dict[str, Any], loaded: Any) -> dict[str, Any]:
    """Defaults, overlaid with whatever the file actually has. Unknown keys drop."""
    out = copy.deepcopy(defaults)
    if not isinstance(loaded, dict):
        return out
    for key, default_value in defaults.items():
        if key not in loaded:
            continue
        value = loaded[key]
        if isinstance(default_value, dict):
            out[key] = _merge(default_value, value)
        else:
            out[key] = value
    return out


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _as_int(value: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _as_float(value: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_time(value: Any, fallback: str) -> str:
    """Accept 'H:MM' / 'HH:MM', normalise to 'HH:MM'."""
    if not isinstance(value, str):
        return fallback
    parts = value.strip().split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def normalize(raw: Any) -> dict[str, Any]:
    cfg = _merge(DEFAULTS, raw)

    persona = cfg["persona"]
    for field in DEFAULTS["persona"]:
        persona[field] = _as_text(persona.get(field))

    timing = cfg["timing"]
    d = DEFAULTS["timing"]
    timing["min_delay_seconds"] = _as_int(timing.get("min_delay_seconds"), d["min_delay_seconds"], 0, 86_400)
    timing["max_delay_seconds"] = _as_int(timing.get("max_delay_seconds"), d["max_delay_seconds"], 0, 86_400)
    if timing["max_delay_seconds"] < timing["min_delay_seconds"]:
        timing["max_delay_seconds"] = timing["min_delay_seconds"]
    timing["active_hours_enabled"] = _as_bool(timing.get("active_hours_enabled"), d["active_hours_enabled"])
    timing["active_hours_start"] = _as_time(timing.get("active_hours_start"), d["active_hours_start"])
    timing["active_hours_end"] = _as_time(timing.get("active_hours_end"), d["active_hours_end"])
    tz = _as_text(timing.get("timezone")) or d["timezone"]
    timing["timezone"] = tz

    behavior = cfg["behavior"]
    for field, default_value in DEFAULTS["behavior"].items():
        behavior[field] = _as_bool(behavior.get(field), default_value)

    out = cfg["outreach"]
    d = DEFAULTS["outreach"]
    out["min_gap_seconds"] = _as_int(out.get("min_gap_seconds"), d["min_gap_seconds"], 5, 86_400)
    out["max_gap_seconds"] = _as_int(out.get("max_gap_seconds"), d["max_gap_seconds"], 5, 86_400)
    if out["max_gap_seconds"] < out["min_gap_seconds"]:
        out["max_gap_seconds"] = out["min_gap_seconds"]
    out["daily_limit"] = _as_int(out.get("daily_limit"), d["daily_limit"], 1, 1000)
    out["auto_send"] = _as_bool(out.get("auto_send"), d["auto_send"])

    ai = cfg["ai"]
    ai["model"] = _as_text(ai.get("model")) or DEFAULTS["ai"]["model"]
    ai["max_tokens"] = _as_int(ai.get("max_tokens"), DEFAULTS["ai"]["max_tokens"], 1, 8192)
    ai["temperature"] = _as_float(ai.get("temperature"), DEFAULTS["ai"]["temperature"], 0.0, 2.0)

    return cfg


def load(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    return normalize(raw)


def save(cfg: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Validate then write atomically, so a crash can't leave a half-written file."""
    clean = normalize(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return clean
