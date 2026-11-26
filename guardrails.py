"""
guardrails.py - keeps the public app safe and cheap.

- Daily AI budget (default $1/day for ALL visitors together)
- Message length and messages-per-meeting limits
- New projects per visitor per day
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import board

DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "1.00"))
# Prices in USD per million tokens. Defaults = Claude Haiku 5.5 (check
# https://platform.claude.com/docs/en/about-claude/pricing if you change model).
PRICE_IN = float(os.environ.get("PRICE_IN_PER_MTOK", "0.10"))
PRICE_OUT = float(os.environ.get("PRICE_OUT_PER_MTOK", "0.50"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "2500"))
MAX_MESSAGES_PER_MEETING = int(os.environ.get("MAX_MESSAGES_PER_MEETING", "20"))
MAX_PROJECTS_PER_VISITOR_PER_DAY = int(os.environ.get("MAX_PROJECTS_PER_VISITOR_PER_DAY", "3"))
CONTACT_URL = os.environ.get("CONTACT_URL", "https://lvudum.com")

USAGE_FILE = board.DATA_DIR / "usage.json"
_lock = threading.Lock()


class BudgetExceeded(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read() -> dict:
    try:
        data = json.loads(Path(USAGE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if data.get("date") != _today():
        data = {"date": _today(), "usd": 0.0, "calls": 0, "visitors": {}}
    return data


def _write(data: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    Path(USAGE_FILE).write_text(json.dumps(data))


def spent_today() -> float:
    with _lock:
        return _read()["usd"]


def check_budget() -> None:
    if spent_today() >= DAILY_BUDGET_USD:
        raise BudgetExceeded()


def record_usage(input_tokens: int, output_tokens: int) -> float:
    cost = input_tokens / 1e6 * PRICE_IN + output_tokens / 1e6 * PRICE_OUT
    with _lock:
        data = _read()
        data["usd"] = round(data["usd"] + cost, 6)
        data["calls"] += 1
        _write(data)
    return cost


def allow_new_project(visitor: str) -> bool:
    with _lock:
        data = _read()
        n = data["visitors"].get(visitor, 0)
        if n >= MAX_PROJECTS_PER_VISITOR_PER_DAY:
            return False
        data["visitors"][visitor] = n + 1
        _write(data)
        return True


def clean_message(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("Type something first.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Keep it under {MAX_MESSAGE_CHARS} characters - meetings move fast!")
    return text
