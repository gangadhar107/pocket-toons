"""
agent/schema.py
---------------
Single source of truth for:
  - TODAY (pinned; never use date.today() in the agent)
  - SCHEMA (tables → columns → SQLite types)
  - Row counts and window bounds (used by prompts and UI pills)

Mirrors the constants in data/generate_db.py exactly. If you change TODAY or
the row counts there, change them here too — the prompt, the validator, and
the UI all consume this module.
"""

from __future__ import annotations

from datetime import date, timedelta

# --- Pinned calendar ---------------------------------------------------------

TODAY: date = date(2026, 5, 8)
WINDOW_DAYS: int = 120
WINDOW_START: date = TODAY - timedelta(days=WINDOW_DAYS - 1)

# --- Volumes (mirrored from data/generate_db.py) ----------------------------

ROW_COUNTS: dict[str, int] = {
    "users":         10_000,
    "sessions":      50_000,
    "transactions":  15_000,
    "content_views": 50_000,
}

# --- Schema ------------------------------------------------------------------

SCHEMA: dict[str, dict[str, str]] = {
    "users": {
        "id":          "INTEGER PRIMARY KEY",
        "name":        "TEXT",
        "country":     "TEXT",      # ISO-2; distribution skewed US/IN/BR
        "plan":        "TEXT",      # 'free' | 'trial' | 'premium'
        "signup_date": "DATE",
    },
    "sessions": {
        "id":           "INTEGER PRIMARY KEY",
        "user_id":      "INTEGER REFERENCES users(id)",
        "date":         "DATE",
        "duration_sec": "INTEGER",
    },
    "transactions": {
        "id":      "INTEGER PRIMARY KEY",
        "user_id": "INTEGER REFERENCES users(id)",
        "amount":  "REAL",           # USD; refunds are negative
        "date":    "DATE",
        "type":    "TEXT",           # 'subscription' | 'ppv' | 'refund'
    },
    "content_views": {
        "id":         "INTEGER PRIMARY KEY",
        "user_id":    "INTEGER REFERENCES users(id)",
        "content_id": "INTEGER",
        "date":       "DATE",
    },
}

# Flat set of every legal column name across the whole schema. Used by the
# SQL validator for best-effort unqualified-column checks.
ALL_COLUMNS: frozenset[str] = frozenset(
    col for cols in SCHEMA.values() for col in cols
)

TABLE_NAMES: frozenset[str] = frozenset(SCHEMA.keys())


def schema_for_prompt() -> str:
    """Human-readable schema block to inline into the LLM system prompt."""
    lines = [f"Today's date is {TODAY.isoformat()}. Dialect: SQLite."]
    lines.append(
        f"Data window: {WINDOW_START.isoformat()} .. {TODAY.isoformat()} "
        f"({WINDOW_DAYS} days)."
    )
    lines.append("")
    for table, cols in SCHEMA.items():
        lines.append(f"TABLE {table} ({ROW_COUNTS[table]:,} rows)")
        for col, col_type in cols.items():
            lines.append(f"  - {col} {col_type}")
        lines.append("")
    return "\n".join(lines)
